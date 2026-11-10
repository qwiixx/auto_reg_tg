"""Telegram client construction and protocol-specific integration.

Private Telethon hooks and the Android profile are retained for compatibility
with the original script. Recheck them when upgrading pinned dependencies.
"""

import os
import re
from datetime import UTC, datetime

from opentele2.api import API
from opentele2.tl import TelegramClient
from telethon import errors, functions
from telethon.tl import types as t

from models import ProxySettings, SentCodeResponse

PKG = "org.telegram.messenger.web"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(HERE, "sessions")


def session_path(phone: str) -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, "reg_" + re.sub(r"\D", "", phone))


def build_client(
    phone: str, device_token: str, *, proxy: ProxySettings | None = None
) -> TelegramClient:
    api = API.TelegramAndroid.copy()
    api.api_id = 4
    api.api_hash = "014b35b6184100b085b0d0572f9b5103"
    api.device_model = "samsungSM-S928B"
    api.system_version = "SDK 35"
    api.app_version = "12.8.3 (69229)"
    api.lang_pack = "android"
    api.lang_code = "en"
    api.system_lang_code = "en-us"
    client = TelegramClient(session_path(phone), api=api, proxy=proxy)
    offset = datetime.now(UTC).astimezone().utcoffset()
    tz = int(offset.total_seconds()) if offset else 0
    client._init_request.params = t.JsonObject(
        value=[
            t.JsonObjectValue("device_token", t.JsonString(device_token)),
            t.JsonObjectValue(
                "data",
                t.JsonString("49C1522548EBACD46CE322B6FD47F6092BB745D0F88082145CAF35E14DCC38E1"),
            ),
            t.JsonObjectValue("installer", t.JsonString("com.android.vending")),
            t.JsonObjectValue("package_id", t.JsonString(PKG)),
            t.JsonObjectValue("tz_offset", t.JsonNumber(float(tz))),
            t.JsonObjectValue("perf_cat", t.JsonNumber(3.0)),
        ]
    )
    return client


async def send_code(client: TelegramClient, phone: str) -> SentCodeResponse:
    request = functions.auth.SendCodeRequest(
        phone,
        client.api_id,
        client.api_hash,
        t.CodeSettings(allow_firebase=True, allow_app_hash=True),
    )
    try:
        return await client(request)
    except errors.RPCError as exc:
        challenge = re.fullmatch(
            r"RECAPTCHA_CHECK_([A-Za-z0-9_/-]+?)__([A-Za-z0-9_-]+)",
            exc.message or "",
        )
        if exc.code != 403 or challenge is None:
            raise

    try:
        from dependent import config, get_token
    except ModuleNotFoundError as exc:
        if exc.name != "dependent":
            raise
        raise RuntimeError("Для reCAPTCHA установите пакет из репозитория fluctuation.") from exc

    action, sitekey = challenge.groups()
    if sitekey != config.SITEKEY or PKG != config.PACKAGE:
        raise RuntimeError("Запрос reCAPTCHA не совпадает с настройками fluctuation.")
    token = await get_token(action=action.encode("ascii"))
    if not token:
        raise RuntimeError("fluctuation не получил токен reCAPTCHA.")

    return await client(functions.InvokeWithReCaptchaRequest(token=token, query=request))


async def accept_authorization(client: TelegramClient, user: t.User) -> t.User:
    """Keep the private login hook isolated from the workflow."""
    return await client._on_login(user)
