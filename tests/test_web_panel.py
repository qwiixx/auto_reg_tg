import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from test_workflow import client_stub, sent_code

import registration
from models import RegistrationResult, RegistrationStatus
from number_provider import Activation
from panel_auth import COOKIE_NAME, LOGIN_COOLDOWN, MAX_LOGIN_FAILURES, SESSION_TTL, PanelAuth
from panel_jobs import PanelJobs
from proxy_config import parse_proxy
from web_panel import create_app

PHONE = "+12345678"
SECOND_PHONE = "+87654321"
SESSION = b"test fixture, no account credentials"
LOGIN = {"username": "operator", "password": "Пароль + test 123"}


class WebPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        (self.directory / "reg_12345678.session").write_bytes(SESSION)
        (self.directory / "reg_12345678.session-journal").write_bytes(b"journal")
        (self.directory / "other.session").write_bytes(b"other")
        self.jobs = PanelJobs(registration.RegistrationService(""))
        auth = PanelAuth(**LOGIN)
        app = create_app(self.jobs, auth=auth, sessions_dir=self.directory)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.origin = {"Origin": str(self.http.make_url("/").origin())}

    async def login(self):
        response = await self.http.post("/api/auth/login", headers=self.origin, json=LOGIN)
        self.assertEqual(response.status, 204)
        return response

    async def state(self):
        response = await self.http.get("/api/state")
        self.assertEqual(response.status, 200)
        return await response.json()

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while True:
                state = await self.state()
                if predicate(state):
                    return state
                await asyncio.sleep(0.005)

    async def test_catalog_and_download_require_login_and_local_origin(self):
        for path in ("/api/state", "/api/sessions/12345678"):
            response = await self.http.get(path)
            self.assertEqual(response.status, 401)
            response = await self.http.get(path, headers={"Origin": "https://example.com"})
            self.assertEqual(response.status, 403)
        response = await self.http.get("/api/state", headers={"Host": "example.com"})
        self.assertEqual(response.status, 403)

        response = await self.login()
        cookie = response.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        state = await self.state()
        self.assertEqual(
            [(file["phone"], file["name"]) for file in state["sessions"]],
            [(PHONE, "reg_12345678.session")],
        )
        response = await self.http.get("/api/sessions/12345678")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["Content-Disposition"], 'attachment; filename="reg_12345678.session"'
        )
        self.assertEqual(await response.read(), SESSION)
        response = await self.http.get("/api/sessions/other.session")
        self.assertEqual(response.status, 404)

    async def test_login_rejects_wrong_credentials_and_throttles_retries(self):
        response = await self.http.post("/api/auth/login", json=LOGIN)
        self.assertEqual(response.status, 403)
        wrong_login = {**LOGIN, "password": "incorrect-password"}
        with patch("panel_auth.monotonic", return_value=100) as now:
            for _ in range(MAX_LOGIN_FAILURES - 1):
                response = await self.http.post(
                    "/api/auth/login", headers=self.origin, json=wrong_login
                )
                self.assertEqual(response.status, 401)
                self.assertNotIn(COOKIE_NAME, response.cookies)
            response = await self.http.post(
                "/api/auth/login", headers=self.origin, json=wrong_login
            )
            self.assertEqual(response.status, 429)
            self.assertEqual(response.headers["Retry-After"], str(LOGIN_COOLDOWN))
            response = await self.http.post("/api/auth/login", headers=self.origin, json=LOGIN)
            self.assertEqual(response.status, 429)
            now.return_value += LOGIN_COOLDOWN
            await self.login()

    async def test_logout_revokes_cookie_and_requires_same_origin(self):
        response = await self.login()
        cookie = response.cookies[COOKIE_NAME].value
        response = await self.http.post("/api/auth/logout")
        self.assertEqual(response.status, 403)
        await self.state()
        response = await self.http.post("/api/auth/logout", headers=self.origin)
        self.assertEqual(response.status, 204)
        response = await self.http.get("/api/state", cookies={COOKIE_NAME: cookie})
        self.assertEqual(response.status, 401)

    async def test_browser_session_expires(self):
        with patch("panel_auth.monotonic", return_value=100) as now:
            await self.login()
            await self.state()
            now.return_value += SESSION_TTL
            response = await self.http.get("/api/state")
            self.assertEqual(response.status, 401)

    async def test_invalid_file_starts_no_work(self):
        await self.login()
        response = await self.http.post(
            "/api/jobs", headers=self.origin, json={"mode": "file", "numbers": f"{PHONE}\ninvalid"}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("Строка 2", (await response.json())["error"])
        self.assertFalse((await self.state())["running"])

    async def test_malformed_json_returns_a_client_error(self):
        await self.login()
        for charset, body in (
            ("not-a-codec", b"{}"),
            ("utf-8", b"[" * 2000 + b"]" * 2000),
        ):
            with self.subTest(charset=charset, size=len(body)):
                response = await self.http.post(
                    "/api/jobs",
                    headers={**self.origin, "Content-Type": f"application/json; charset={charset}"},
                    data=body,
                )
                self.assertEqual(response.status, 400)
                self.assertIn("error", await response.json())
        self.assertFalse((await self.state())["running"])

    async def test_oversized_json_returns_a_json_error(self):
        await self.login()
        response = await self.http.post(
            "/api/jobs",
            headers={**self.origin, "Content-Type": "application/json"},
            data=b" " * (256 * 1024 + 1),
        )
        self.assertEqual(response.status, 413)
        self.assertIn("error", await response.json())
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_proxy_file_rotates_by_group_and_applies_only_to_one_queue(self):
        await self.login()
        proxies = ("http://first.example:8080", "socks5://login:secret@second.example:1080")
        default = "http://default.example:8080"
        self.jobs.proxy = parse_proxy(default)
        self.jobs.mail_proxy_url = default
        with patch.object(
            self.jobs.service,
            "register",
            new=AsyncMock(return_value=RegistrationResult(None, RegistrationStatus.FAILED)),
        ) as register:
            response = await self.http.post(
                "/api/jobs",
                headers=self.origin,
                json={
                    "mode": "file",
                    "numbers": "\n".join(f"+1234567{i}" for i in range(5)),
                    "proxies": "\ufeff" + "\r\n\r\n".join(proxies),
                    "numbers_per_proxy": 2,
                },
            )
            self.assertEqual(response.status, 202)
            final = await self.wait_for(lambda state: not state["running"])
            self.assertNotIn("secret", repr(final))
            self.assertEqual(len(register.await_args_list), 5)
            for call, url in zip(
                register.await_args_list,
                [proxies[0], proxies[0], proxies[1], proxies[1], proxies[0]],
                strict=True,
            ):
                self.assertEqual(call.kwargs, {"proxy": parse_proxy(url), "mail_proxy_url": url})

            response = await self.http.post(
                "/api/jobs", headers=self.origin, json={"mode": "phone", "phone": PHONE}
            )
            self.assertEqual(response.status, 202)
            await self.wait_for(lambda state: not state["running"])
            register.assert_awaited_with(
                PHONE, self.jobs, proxy=parse_proxy(default), mail_proxy_url=default
            )

    async def test_bad_proxy_file_or_interval_leaves_queue_untouched(self):
        await self.login()
        valid = "http://first.example:8080"
        for proxies, interval, expected in (
            (f"{valid}\nhttps://login:secret@second.example:443", 4, "строка 2"),
            (valid, True, "от 2 до 5"),
            ("\ufeff\r\n  ", 4, "список прокси пуст"),
        ):
            with self.subTest(interval=interval, expected=expected):
                response = await self.http.post(
                    "/api/jobs",
                    headers=self.origin,
                    json={
                        "mode": "phone",
                        "phone": PHONE,
                        "proxies": proxies,
                        "numbers_per_proxy": interval,
                    },
                )
                self.assertEqual(response.status, 400)
                error = (await response.json())["error"]
                self.assertIn(expected, error.lower())
                self.assertNotIn("secret", error)
        self.assertEqual((await self.state())["rows"], [])

    async def test_manual_file_consumes_one_prompt_at_a_time(self):
        await self.login()
        first, second = client_stub(), client_stub()
        with (
            patch.object(registration, "build_client", side_effect=[first, second]),
            patch.object(registration, "send_code", new=AsyncMock(return_value=sent_code())),
        ):
            response = await self.http.post(
                "/api/jobs",
                headers=self.origin,
                json={"mode": "file", "numbers": f"{PHONE}\n{SECOND_PHONE}"},
            )
            self.assertEqual(response.status, 202)
            initial = await self.wait_for(lambda state: state["prompt"] is not None)
            prompt_id = initial["prompt"]["id"]
            self.assertEqual(initial["prompt"]["phone"], PHONE)
            self.assertEqual([row["status"] for row in initial["rows"]], ["running", "queued"])
            response = await self.http.get("/api/sessions/12345678")
            self.assertEqual(response.status, 409)

            response = await self.http.post(
                f"/api/prompts/{prompt_id}", headers=self.origin, json={"value": "01234"}
            )
            self.assertEqual(response.status, 200)
            next_state = await self.wait_for(
                lambda state: (state["prompt"] or {}).get("phone") == SECOND_PHONE
            )
            first.disconnect.assert_awaited_once()
            first.sign_in.assert_awaited_once_with(
                phone=PHONE, code="01234", phone_code_hash="hash"
            )
            response = await self.http.post(
                f"/api/prompts/{prompt_id}", headers=self.origin, json={"value": "99999"}
            )
            self.assertEqual(response.status, 409)
            next_id = next_state["prompt"]["id"]
            response = await self.http.post(
                f"/api/prompts/{next_id}", headers=self.origin, json={"value": None}
            )
            self.assertEqual(response.status, 200)
            final = await self.wait_for(lambda state: not state["running"])
            self.assertEqual([row["status"] for row in final["rows"]], ["succeeded", "failed"])
            self.assertIsNone(final["prompt"])
            second.sign_in.assert_not_awaited()

    async def test_stop_closes_client_and_clears_prompt(self):
        await self.login()
        client = client_stub()
        with (
            patch.object(registration, "build_client", return_value=client),
            patch.object(registration, "send_code", new=AsyncMock(return_value=sent_code())),
        ):
            response = await self.http.post(
                "/api/jobs", headers=self.origin, json={"mode": "phone", "phone": PHONE}
            )
            self.assertEqual(response.status, 202)
            await self.wait_for(lambda state: state["prompt"] is not None)
            response = await self.http.delete("/api/jobs/current", headers=self.origin)
            self.assertEqual(response.status, 200)
            state = await self.wait_for(lambda state: not state["running"])
            self.assertEqual(state["rows"][0]["status"], "cancelled")
            self.assertIsNone(state["prompt"])
        client.disconnect.assert_awaited_once()

    async def test_auto_uses_server_configured_provider(self):
        await self.login()
        url = "socks5://auto.example:1080"
        data = {"mode": "auto", "count": 1, "first_name": "Test", "proxies": url}
        response = await self.http.post("/api/jobs", headers=self.origin, json=data)
        self.assertEqual(response.status, 409)
        activation = Activation(PHONE, "fixture-id")
        provider = SimpleNamespace(
            acquire_number=AsyncMock(return_value=activation),
            wait_for_code=AsyncMock(return_value="01234"),
            finalize=AsyncMock(),
        )

        @asynccontextmanager
        async def factory():
            yield provider

        self.jobs.provider_factory = factory
        client = client_stub()
        with (
            patch.object(registration, "build_client", return_value=client) as build,
            patch.object(registration, "send_code", new=AsyncMock(return_value=sent_code())),
        ):
            response = await self.http.post("/api/jobs", headers=self.origin, json=data)
            self.assertEqual(response.status, 202)
            state = await self.wait_for(lambda state: not state["running"])
        self.assertEqual(state["rows"][0]["phone"], PHONE)
        self.assertEqual(state["rows"][0]["status"], "succeeded")
        build.assert_called_once_with(PHONE, "", proxy=parse_proxy(url))
        provider.finalize.assert_awaited_once_with(activation, used=True)
