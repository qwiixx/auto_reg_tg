"""HeroSMS adapter for the automatic registration workflow."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from models import CodeChallenge
from number_provider import Activation, ProviderConfigurationError

logger = logging.getLogger(__name__)
BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
POLL_INTERVAL = 5
SMS_DELIVERIES = frozenset(
    {"SentCodeTypeSms", "SentCodeTypeFirebaseSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"}
)
WAIT_STATUSES = frozenset({"STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND"})


class HeroSMSError(Exception):
    pass


class NoNumbersError(HeroSMSError):
    pass


class NoBalanceError(HeroSMSError):
    pass


class EarlyCancelError(HeroSMSError):
    pass


ERRORS = {
    "NO_NUMBERS": NoNumbersError,
    "NO_BALANCE": NoBalanceError,
    "NO_MONEY": NoBalanceError,
    "EARLY_CANCEL_DENIED": EarlyCancelError,
}
KNOWN_REJECTIONS = frozenset({"BAD_KEY", "BAD_SERVICE", "BAD_STATUS", "UNPROCESSABLE_ENTITY"})


async def _send_once(
    request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    # The provider uses GET for purchases. Disable aiohttp's retry on disconnect.
    try:
        return await handler(request)
    except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError):
        raise HeroSMSError("Соединение с HeroSMS оборвалось.") from None


def _text(value: object) -> str:
    if isinstance(value, str) or type(value) is int:
        return str(value).strip()
    return ""


class HeroSMS:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        country: int,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._country = country
        self._base_url = base_url

    async def _request(self, action: str, **params: str | int) -> str | dict[str, object]:
        try:
            async with self._session.get(
                self._base_url,
                params={"api_key": self._api_key, "action": action, **params},
                allow_redirects=False,
                middlewares=(_send_once,),
            ) as response:
                status = response.status
                body = (await response.text()).strip()
        except TimeoutError:
            logger.debug("HeroSMS request timed out action=%s", action)
            raise TimeoutError(f"HeroSMS не ответил на {action} вовремя.") from None
        except (HeroSMSError, aiohttp.ClientError, LookupError, UnicodeError) as exc:
            logger.debug("HeroSMS request failed action=%s error=%s", action, type(exc).__name__)
            raise HeroSMSError(f"Ошибка запроса HeroSMS {action}.") from None

        logger.debug("HeroSMS response action=%s http_status=%d", action, status)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
        error_code = _text(payload.get("title")) if isinstance(payload, dict) else body
        if error_code in ERRORS:
            raise ERRORS[error_code](error_code)
        if error_code == "NO_ACTIVATION":
            return error_code
        if not 200 <= status < 300 or (isinstance(payload, dict) and error_code):
            logger.warning(
                "HeroSMS rejected request action=%s http_status=%d code=%s",
                action,
                status,
                error_code if error_code in KNOWN_REJECTIONS else "unknown",
            )
            raise HeroSMSError(f"HeroSMS отклонил запрос {action} (HTTP {status}).")
        return payload if isinstance(payload, dict) else body

    async def acquire_number(self) -> Activation:
        result = await self._request("getNumberV2", service="tg", country=self._country)
        if not isinstance(result, dict):
            raise HeroSMSError("HeroSMS не вернул данные активации.")
        reference = _text(result.get("activationId")) or _text(result.get("id"))
        if not reference:
            raise HeroSMSError("HeroSMS не вернул ID активации.")
        phone = _text(result.get("phoneNumber")) or _text(result.get("number"))
        # Keep a known reservation available for cancellation even if the number is invalid.
        return Activation(phone=phone, reference=reference)

    async def wait_for_code(self, activation: Activation, challenge: CodeChallenge) -> str | None:
        if challenge.delivery not in SMS_DELIVERIES:
            return None
        while True:
            result = await self._request("getStatus", id=activation.reference)
            if isinstance(result, dict):
                code = _text(result.get("code")) or _text(result.get("smsCode"))
                if code:
                    return code
                status = _text(result.get("status"))
            else:
                status, _, code = result.partition(":")
                if status == "STATUS_OK" and code.strip():
                    return code.strip()
            if status in {"STATUS_CANCEL", "NO_ACTIVATION"}:
                return None
            if status not in WAIT_STATUSES:
                raise HeroSMSError("HeroSMS вернул неизвестный статус SMS.")
            await asyncio.sleep(POLL_INTERVAL)

    async def finalize(self, activation: Activation, *, used: bool) -> None:
        result = await self._request("setStatus", id=activation.reference, status=6 if used else 8)
        expected = "ACCESS_ACTIVATION" if used else "ACCESS_CANCEL"
        status = _text(result.get("status")) if isinstance(result, dict) else result
        if status != expected and not (not used and status == "STATUS_CANCEL"):
            raise HeroSMSError("HeroSMS не подтвердил завершение активации.")


@asynccontextmanager
async def open_provider() -> AsyncIterator[HeroSMS]:
    api_key = os.getenv("HEROSMS_API_KEY", "").strip()
    if not api_key:
        raise ProviderConfigurationError("Укажите HEROSMS_API_KEY для HeroSMS.")
    raw_country = os.getenv("HEROSMS_COUNTRY", "0").strip()
    try:
        country = int(raw_country)
        if country < 0:
            raise ValueError
    except ValueError:
        raise ProviderConfigurationError("Укажите числовой ID страны в HEROSMS_COUNTRY.") from None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        yield HeroSMS(session, api_key, country)
