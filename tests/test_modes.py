import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors
from test_workflow import client_stub, input_stub, sent_code

import main as cli
import registration as reg
from models import RegistrationStatus
from number_provider import Activation, register_automatic
from phone_numbers import parse_phone_list

PHONE = "+12345678"


class ManualModeTests(unittest.IsolatedAsyncioTestCase):
    def test_file_normalizes_deduplicates_and_preserves_order(self):
        self.assertEqual(
            parse_phone_list("\ufeff\r\n+1 (234) 5678\r\n12345678\r\n+87654321\r\n"),
            [PHONE, "+87654321"],
        )

    async def test_invalid_file_stops_before_first_network_call(self):
        with TemporaryDirectory() as directory:
            file = Path(directory, "phones.txt")
            file.write_text(f"{PHONE}\ninvalid +87654321\n", encoding="utf-8")
            with (
                patch.object(reg, "build_client") as build,
                patch("builtins.input") as prompt,
                patch("sys.stderr") as stderr,
                self.assertRaises(SystemExit),
            ):
                await cli.main(["manual", "--file", str(file)])
            self.assertIn("Строка 2", "".join(call.args[0] for call in stderr.write.call_args_list))
            build.assert_not_called()
            prompt.assert_not_called()

    async def test_file_finishes_each_phone_before_starting_next(self):
        with TemporaryDirectory() as directory:
            file = Path(directory, "phones.txt")
            file.write_text(f"{PHONE}\n+87654321\n", encoding="utf-8")
            first, second = client_stub(), client_stub()

            def build_client(phone, *args, **kwargs):
                if phone == PHONE:
                    return first
                first.disconnect.assert_awaited_once()
                return second

            with (
                patch.object(reg, "build_client", side_effect=build_client) as build,
                patch.object(reg, "send_code", new=AsyncMock(return_value=sent_code())),
                patch("builtins.input", side_effect=["", "-", "01234", "09876"]),
                patch("builtins.print"),
            ):
                await cli.main(["manual", "--file", str(file)])
            self.assertEqual([call.args[0] for call in build.call_args_list], [PHONE, "+87654321"])
            first.sign_in.assert_awaited_once_with(
                phone=PHONE, code="01234", phone_code_hash="hash"
            )
            second.sign_in.assert_awaited_once_with(
                phone="+87654321", code="09876", phone_code_hash="hash"
            )

    async def test_single_phone_exits_after_attempt(self):
        client = client_stub()
        with (
            patch.object(reg, "build_client", return_value=client) as build,
            patch.object(reg, "send_code", new=AsyncMock(return_value=sent_code())),
            patch("builtins.input", side_effect=["", "-", "01234"]),
            patch("builtins.print"),
        ):
            await cli.main(["manual", "--phone", PHONE])
        build.assert_called_once()
        client.sign_in.assert_awaited_once_with(phone=PHONE, code="01234", phone_code_hash="hash")

    async def test_same_phone_cannot_open_two_sessions(self):
        service = reg.RegistrationService("")
        client, inputs = client_stub(), input_stub()
        entered, release = asyncio.Event(), asyncio.Event()

        async def read_code(challenge):
            entered.set()
            await release.wait()
            return "12345"

        inputs.read_code.side_effect = read_code
        with (
            patch.object(reg, "build_client", return_value=client) as build,
            patch.object(reg, "send_code", new=AsyncMock(return_value=sent_code())),
        ):
            task = asyncio.create_task(service.register(PHONE, inputs))
            await entered.wait()
            try:
                duplicate = await service.register("+1 (234) 5678", inputs)
                self.assertEqual(duplicate.reason, "already_running")
                build.assert_called_once()
            finally:
                release.set()
                self.assertEqual((await task).status, RegistrationStatus.SUCCEEDED)


class AutomaticModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = reg.RegistrationService("")
        self.activation = Activation(PHONE, "private-reference")
        self.provider = SimpleNamespace(
            acquire_number=AsyncMock(return_value=self.activation),
            wait_for_code=AsyncMock(return_value="01234"),
            finalize=AsyncMock(),
        )
        self.client = client_stub()
        self.enterContext(patch.object(reg, "build_client", return_value=self.client))
        self.send = self.enterContext(
            patch.object(reg, "send_code", new=AsyncMock(return_value=sent_code()))
        )

    async def test_auto_cli_opens_provider_and_routes_code_by_activation(self):
        @asynccontextmanager
        async def factory():
            yield self.provider
            self.provider.finalize.assert_awaited_once_with(self.activation, used=True)

        with (
            patch.object(cli, "load_provider_factory", return_value=factory),
            patch("builtins.input", side_effect=AssertionError("Automatic mode prompted")),
            patch("builtins.print"),
        ):
            await cli.main(["auto", "--provider", "example:open_provider"])

        activation, challenge = self.provider.wait_for_code.await_args.args
        self.assertIs(activation, self.activation)
        self.assertEqual((challenge.phone, challenge.delivery), (PHONE, "SentCodeTypeSms"))
        self.client.sign_in.assert_awaited_once_with(
            phone=PHONE, code="01234", phone_code_hash="hash"
        )
        self.client.disconnect.assert_awaited_once()

    async def test_rejected_code_is_not_retried_and_releases_activation(self):
        self.client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
        result = await register_automatic(self.service, self.provider, first_name="Test")
        self.assertEqual(result.reason, "code_invalid")
        self.provider.wait_for_code.assert_awaited_once()
        self.provider.finalize.assert_awaited_once_with(self.activation, used=False)

    async def test_timeout_releases_activation(self):
        async def wait_for_code(*args):
            await asyncio.Future()

        self.provider.wait_for_code.side_effect = wait_for_code
        result = await register_automatic(
            self.service, self.provider, first_name="Test", code_timeout=0.01
        )
        self.assertEqual(result.reason, "provider_code_timeout")
        self.provider.finalize.assert_awaited_once_with(self.activation, used=False)
        self.client.disconnect.assert_awaited_once()

    async def test_cancel_disconnects_and_releases_activation(self):
        entered = asyncio.Event()

        async def wait_for_code(*args):
            entered.set()
            await asyncio.Future()

        self.provider.wait_for_code.side_effect = wait_for_code
        task = asyncio.create_task(
            register_automatic(self.service, self.provider, first_name="Test")
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.disconnect.assert_awaited_once()
        self.provider.finalize.assert_awaited_once_with(self.activation, used=False)

    async def test_repeated_cancel_during_finalization_still_settles_used_activation(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def finalize(activation, *, used):
            entered.set()
            await release.wait()
            finished.set()

        self.provider.finalize.side_effect = finalize
        task = asyncio.create_task(
            register_automatic(self.service, self.provider, first_name="Test")
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.provider.finalize.assert_awaited_once_with(self.activation, used=True)

    async def test_repeated_cancel_during_disconnect_keeps_confirmed_outcome(self):
        entered, release = asyncio.Event(), asyncio.Event()
        finished = asyncio.Event()

        async def disconnect():
            entered.set()
            await release.wait()
            finished.set()

        self.client.disconnect.side_effect = disconnect
        task = asyncio.create_task(
            register_automatic(self.service, self.provider, first_name="Test")
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        active_during_cleanup = self.service.is_active(PHONE)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(active_during_cleanup)
        self.assertTrue(finished.is_set())
        self.assertFalse(self.service.is_active(PHONE))
        self.provider.finalize.assert_awaited_once_with(self.activation, used=True)

    async def test_existing_session_releases_unused_activation(self):
        self.client.is_user_authorized.return_value = True
        result = await register_automatic(self.service, self.provider, first_name="Test")
        self.assertEqual(result.status, RegistrationStatus.ALREADY_AUTHORIZED)
        self.send.assert_not_awaited()
        self.provider.wait_for_code.assert_not_awaited()
        self.provider.finalize.assert_awaited_once_with(self.activation, used=False)

    async def test_cleanup_failure_preserves_success(self):
        self.provider.finalize.side_effect = OSError("offline")
        with self.assertLogs("number_provider", level="WARNING"):
            result = await register_automatic(self.service, self.provider, first_name="Test")
        self.assertEqual(result.status, RegistrationStatus.SUCCEEDED)
        self.assertTrue(result.provider_cleanup_failed)
