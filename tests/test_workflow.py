import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors
from telethon.tl import types as t

import registration as reg
from models import LoginAttempt


def client_stub():
    return SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        get_me=AsyncMock(return_value=SimpleNamespace(id=1)),
        sign_in=AsyncMock(return_value=SimpleNamespace(id=1)),
    )


def sent_code():
    return t.auth.SentCode(t.auth.SentCodeTypeSms(5), "hash")


def input_stub():
    return SimpleNamespace(
        read_code=AsyncMock(return_value="12345"),
        read_password=AsyncMock(return_value=" secret "),
        read_first_name=AsyncMock(return_value="Test"),
    )


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_client_stays_open_until_complete(self):
        client = client_stub()
        client.is_user_authorized.return_value = True
        with (
            patch.object(reg, "build_client", return_value=client),
            patch.object(reg, "send_code", new_callable=AsyncMock) as send,
        ):
            async with reg.RegistrationService("").attempt("+12345678", input_stub()) as result:
                send.assert_not_awaited()
                client.disconnect.assert_not_awaited()
                self.assertEqual(result.status, reg.RegistrationStatus.ALREADY_AUTHORIZED)
            client.disconnect.assert_awaited_once()

    async def test_pending_login_preserves_response(self):
        client, sent = client_stub(), sent_code()
        with (
            patch.object(reg, "build_client", return_value=client),
            patch.object(reg, "send_code", new=AsyncMock(return_value=sent)),
        ):
            attempt = await reg.arm("+12345678", "")
        self.assertIs(attempt.sent_code, sent)
        inputs = input_stub()
        with patch.object(reg, "finish_login", new_callable=AsyncMock) as finish:
            await reg.complete(attempt, inputs)
            finish.assert_awaited_once_with(
                client, "+12345678", sent, inputs, None, max_code_attempts=3
            )

    async def test_connect_error_closes_client(self):
        client = client_stub()
        client.connect.side_effect = OSError("offline")
        with (
            patch.object(reg, "build_client", return_value=client),
            self.assertRaises(OSError),
        ):
            await reg.arm("+12345678", "")
        client.disconnect.assert_awaited_once()

    async def test_cancellation_closes_client(self):
        client = client_stub()
        client.connect.side_effect = asyncio.CancelledError
        with (
            patch.object(reg, "build_client", return_value=client),
            self.assertRaises(asyncio.CancelledError),
        ):
            await reg.arm("+12345678", "")
        client.disconnect.assert_awaited_once()

    async def test_cleanup_failure_does_not_override_success(self):
        client = client_stub()
        client.disconnect.side_effect = OSError("offline")
        client.is_user_authorized.return_value = True
        with (
            patch.object(reg, "build_client", return_value=client),
            self.assertLogs("registration", level="WARNING"),
        ):
            result = await reg.RegistrationService("").register("+12345678", input_stub())
        self.assertEqual(result.status, reg.RegistrationStatus.ALREADY_AUTHORIZED)

    async def test_app_without_fallback_skips(self):
        client = client_stub()
        sent = t.auth.SentCode(t.auth.SentCodeTypeApp(5), "hash")
        with patch.object(reg, "finish_login", new_callable=AsyncMock) as finish:
            with self.assertRaisesRegex(reg.RegistrationRejected, "app_code_without_fallback"):
                await reg.complete(LoginAttempt(client, "+12345678", sent), input_stub())
            finish.assert_not_awaited()

    async def test_manual_code_has_three_attempts(self):
        client = client_stub()
        client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
        inputs = input_stub()
        with self.assertRaisesRegex(reg.RegistrationRejected, "code_invalid"):
            await reg.finish_login(client, "+12345678", sent_code(), inputs)
        self.assertEqual(client.sign_in.await_count, 3)
        self.assertEqual(
            [call.args[0].attempt for call in inputs.read_code.await_args_list], [1, 2, 3]
        )

    async def test_automatic_code_is_not_retried(self):
        client = client_stub()
        client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
        inputs = input_stub()
        with self.assertRaisesRegex(reg.RegistrationRejected, "code_invalid"):
            await reg.finish_login(client, "+12345678", sent_code(), inputs, "12345")
        client.sign_in.assert_awaited_once()
        inputs.read_code.assert_not_awaited()

    async def test_2fa_preserves_password_spaces(self):
        client = client_stub()
        client.sign_in.side_effect = [
            errors.SessionPasswordNeededError(None),
            SimpleNamespace(id=1),
        ]
        await reg.finish_login(client, "+12345678", sent_code(), input_stub())
        self.assertEqual(client.sign_in.await_args.kwargs, {"password": " secret "})

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            LoginAttempt(client_stub(), "+12345678")

    async def test_email_setup_passes_new_code_and_hash(self):
        client = client_stub()
        initial = t.auth.SentCode(t.auth.SentCodeTypeSetUpEmailRequired(), "old-hash")
        updated = sent_code()
        inputs = input_stub()
        with (
            patch.object(
                reg, "setup_email", new=AsyncMock(return_value=(updated, "12345"))
            ) as setup,
            patch.object(reg, "finish_login", new_callable=AsyncMock) as finish,
        ):
            await reg.complete(
                LoginAttempt(client, "+12345678", initial),
                inputs,
                mail_proxy_url="http://localhost:8080",
            )
        setup.assert_awaited_once_with(
            client, "+12345678", "old-hash", mail_proxy_url="http://localhost:8080"
        )
        finish.assert_awaited_once_with(
            client, "+12345678", updated, inputs, "12345", max_code_attempts=3
        )

    async def test_app_resend_uses_new_hash(self):
        client = AsyncMock()
        updated = sent_code()
        client.return_value = updated
        initial = t.auth.SentCode(
            t.auth.SentCodeTypeApp(5), "old-hash", next_type=t.auth.CodeTypeSms()
        )
        inputs = input_stub()
        with patch.object(reg, "finish_login", new_callable=AsyncMock) as finish:
            await reg.complete(LoginAttempt(client, "+12345678", initial), inputs)
        self.assertEqual(client.await_args.args[0].phone_code_hash, "old-hash")
        finish.assert_awaited_once_with(
            client, "+12345678", updated, inputs, None, max_code_attempts=3
        )

    async def test_free_number_starts_signup(self):
        client = client_stub()
        client.sign_in.side_effect = errors.PhoneNumberUnoccupiedError(None)
        inputs = input_stub()
        with patch.object(reg, "sign_up", new_callable=AsyncMock) as signup:
            await reg.finish_login(client, "+12345678", sent_code(), inputs)
        signup.assert_awaited_once_with(client, "+12345678", "hash", inputs)

    async def test_immediate_success_accepts_authorization(self):
        client = client_stub()
        user = t.User(id=1)
        response = t.auth.SentCodeSuccess(t.auth.Authorization(user=user))
        with (
            patch.object(reg, "build_client", return_value=client),
            patch.object(reg, "send_code", new=AsyncMock(return_value=response)),
            patch.object(reg, "accept_authorization", new=AsyncMock(return_value=user)) as accept,
        ):
            attempt = await reg.arm("+12345678", "")
        self.assertTrue(attempt.authorized)
        accept.assert_awaited_once_with(client, user)
        await reg.complete(attempt, input_stub())
