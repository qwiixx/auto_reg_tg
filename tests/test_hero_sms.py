import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_workflow import client_stub, sent_code

import registration
from models import CodeChallenge, RegistrationStatus
from number_provider import (
    Activation,
    ProviderConfigurationError,
    load_provider_factory,
    register_automatic,
)
from providers import hero_sms

ACTIVATION = Activation("+12025550101", "123")
CHALLENGE = CodeChallenge(ACTIVATION.phone, "SentCodeTypeSms", 5, None, 1, 1)


class HeroSMSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.responses = deque()

        async def respond(request):
            self.requests.append(dict(request.query))
            response = self.responses.popleft()
            if response is None:
                request.transport.abort()
                return web.Response()
            return response

        app = web.Application()
        app.router.add_get("/handler_api.php", respond)
        server = await self.enterAsyncContext(TestServer(app))
        session = await self.enterAsyncContext(aiohttp.ClientSession())
        self.provider = hero_sms.HeroSMS(
            session, "fixture-key", 16, base_url=str(server.make_url("/handler_api.php"))
        )

    async def test_registration_polls_preserves_leading_zero_and_completes_activation(self):
        self.responses.extend(
            [
                web.json_response({"activationId": 123, "phoneNumber": 12025550101}),
                web.Response(text="STATUS_WAIT_CODE"),
                web.Response(text="STATUS_OK:01234"),
                web.Response(text="ACCESS_ACTIVATION"),
            ]
        )
        client = client_stub()
        with (
            patch.object(hero_sms, "POLL_INTERVAL", 0),
            patch.object(registration, "build_client", return_value=client),
            patch.object(registration, "send_code", new=AsyncMock(return_value=sent_code())),
        ):
            result = await register_automatic(
                registration.RegistrationService(""), self.provider, first_name="Test"
            )
        self.assertEqual(result.status, RegistrationStatus.SUCCEEDED)
        self.assertFalse(result.provider_cleanup_failed)
        client.sign_in.assert_awaited_once_with(
            phone=ACTIVATION.phone, code="01234", phone_code_hash="hash"
        )
        self.assertEqual(
            self.requests,
            [
                {
                    "api_key": "fixture-key",
                    "action": "getNumberV2",
                    "service": "tg",
                    "country": "16",
                },
                {"api_key": "fixture-key", "action": "getStatus", "id": "123"},
                {"api_key": "fixture-key", "action": "getStatus", "id": "123"},
                {"api_key": "fixture-key", "action": "setStatus", "id": "123", "status": "6"},
            ],
        )

    async def test_invalid_number_with_known_activation_is_cancelled(self):
        self.responses.extend(
            [
                web.json_response({"activationId": "123"}),
                web.Response(text="ACCESS_CANCEL"),
            ]
        )
        result = await register_automatic(
            registration.RegistrationService(""), self.provider, first_name="Test"
        )
        self.assertEqual(result.reason, "invalid_phone")
        self.assertFalse(result.provider_cleanup_failed)
        self.assertEqual(self.requests[-1]["status"], "8")

    async def test_json_code_and_cancelled_status(self):
        self.responses.extend(
            [web.json_response({"smsCode": "00987"}), web.Response(text="STATUS_CANCEL")]
        )
        self.assertEqual(await self.provider.wait_for_code(ACTIVATION, CHALLENGE), "00987")
        self.assertIsNone(await self.provider.wait_for_code(ACTIVATION, CHALLENGE))

    async def test_hero_sms_errors_do_not_echo_sensitive_response_details(self):
        self.responses.extend(
            [
                web.Response(text="NO_NUMBERS"),
                web.json_response({"title": "NO_BALANCE", "details": "fixture-key"}, status=402),
                web.json_response({"title": "EARLY_CANCEL_DENIED"}, status=409),
            ]
        )
        with self.assertRaises(hero_sms.NoNumbersError):
            await self.provider.acquire_number()
        with self.assertRaisesRegex(hero_sms.NoBalanceError, "^NO_BALANCE$"):
            await self.provider.acquire_number()
        with self.assertRaises(hero_sms.EarlyCancelError):
            await self.provider.finalize(ACTIVATION, used=False)

    async def test_acquisition_is_not_retried_after_a_disconnect(self):
        self.responses.extend([None, web.json_response({"activationId": "second"})])
        with self.assertRaises(hero_sms.HeroSMSError):
            await self.provider.acquire_number()
        self.assertEqual(len(self.requests), 1)

    async def test_factory_reports_missing_key_before_opening_session(self):
        factory = load_provider_factory("providers.hero_sms:open_provider")
        with (
            patch.dict("os.environ", {"HEROSMS_API_KEY": ""}),
            self.assertRaisesRegex(ProviderConfigurationError, "HEROSMS_API_KEY"),
        ):
            async with factory():
                self.fail("A provider without a key must not open")
