"""Клиент для API Mail.tm (https://mail.tm/)."""

import asyncio
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any

import aiohttp

try:
    import aiohttp_socks
except ModuleNotFoundError as exc:
    if exc.name != "aiohttp_socks":
        raise
    aiohttp_socks = None


_RE = re.compile(r"(?<![0-9])([0-9]{5,8})(?![0-9])")
_REQUEST_ERRORS = (aiohttp.ClientError, TimeoutError, OSError, ValueError)
if aiohttp_socks is not None:
    _REQUEST_ERRORS += (
        aiohttp_socks.ProxyConnectionError,
        aiohttp_socks.ProxyError,
        aiohttp_socks.ProxyTimeoutError,
    )


class MailTMError(Exception):
    pass


class MailTimeoutError(MailTMError):
    pass


class _MailRateLimitError(MailTMError):
    def __init__(self, method: str, path: str, retry_after: float):
        super().__init__(f"Mail.tm вернул HTTP 429 на {method} {path}.")
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(value)))
    except (ValueError, OverflowError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class Account:
    address: str
    password: str = field(repr=False)
    id: str
    token: str = field(repr=False)


class MailMessage:
    def __init__(
        self,
        subject: str,
        intro: str,
        text: str,
        html: list[str],
        *,
        message_id: str = "",
        sender: str = "",
    ) -> None:
        self.subject = subject
        self.intro = intro
        self.text = text
        self.html = html
        self.id = message_id
        self.sender = sender

    @property
    def code(self) -> str | None:
        for content in (self.subject, self.intro, self.text, *self.html):
            match = _RE.search(content or "")
            if match:
                return match.group(1)
        return None


def _rand(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _items(data: object) -> list[dict[str, Any]]:
    items = data.get("hydra:member") if isinstance(data, dict) else data
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise MailTMError("Mail.tm вернул неожиданный формат списка.")
    return items


class MailTM:
    def __init__(
        self, base_url: str = "https://api.mail.tm", *, proxy_url: str | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.proxy_url = proxy_url
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MailTM":
        if self.session is not None and not self.session.closed:
            raise MailTMError("MailTM уже открыт.")
        connector = None
        if self.proxy_url:
            if aiohttp_socks is None:
                raise MailTMError("Для прокси Mail.tm установите aiohttp-socks.")
            if not self.proxy_url.lower().startswith(("http://", "socks5://")):
                raise MailTMError("Для MAIL_PROXY укажите http://host:port или socks5://host:port.")
            try:
                connector = aiohttp_socks.ProxyConnector.from_url(self.proxy_url, rdns=True)
            except ValueError:
                raise MailTMError("Проверьте адрес и порт в MAIL_PROXY.") from None
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=30)
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        if self.session is None or self.session.closed:
            raise MailTMError("Используйте MailTM внутри async with.")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with self.session.request(
                method, self.base_url + path, json=body, headers=headers
            ) as response:
                if response.status == 429:
                    raise _MailRateLimitError(
                        method, path, _retry_after_seconds(response.headers.get("Retry-After"))
                    )
                if response.status >= 400:
                    raise MailTMError(f"Mail.tm вернул HTTP {response.status} на {method} {path}.")
                data = await response.json(content_type=None)
        except _REQUEST_ERRORS as exc:
            raise MailTMError(
                f"Ошибка запроса Mail.tm {method} {path}: {type(exc).__name__}."
            ) from exc
        if not isinstance(data, (dict, list)):
            raise MailTMError("Mail.tm вернул неожиданный формат ответа.")
        return data

    async def _poll(
        self, path: str, token: str, poll_interval: float
    ) -> dict[str, Any] | list[Any]:
        delay = max(1.0, poll_interval)
        max_delay = max(60.0, delay)
        while True:
            try:
                return await self._request("GET", path, token=token)
            except _MailRateLimitError as exc:
                await asyncio.sleep(max(delay, exc.retry_after))
                delay = min(max_delay, delay * 2)

    async def create_account(self) -> Account:
        domains = _items(await self._request("GET", "/domains"))
        domain = next(
            (
                item["domain"]
                for item in domains
                if item.get("domain") and item.get("isActive", True) and not item.get("isPrivate")
            ),
            None,
        )
        if domain is None:
            raise MailTMError("Mail.tm не вернул доступный домен.")
        address = f"{_rand(14)}@{domain}"
        password = _rand(16)
        credentials = {"address": address, "password": password}
        account = await self._request("POST", "/accounts", body=credentials)
        auth = await self._request("POST", "/token", body=credentials)
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(account, dict) or not isinstance(token, str) or not token:
            raise MailTMError("Mail.tm не вернул данные аккаунта или токен.")
        return Account(address, password, str(account.get("id", "")), token)

    async def wait_for_message(
        self,
        token: str,
        match: Callable[[MailMessage], bool] | None = None,
        timeout: float = 300,
        poll_interval: float = 5,
        address: str = "",
    ) -> MailMessage:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        checked: set[str] = set()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    items = _items(await self._poll("/messages", token, poll_interval))
                    for summary in items:
                        message_id = summary.get("id")
                        if not isinstance(message_id, str) or not message_id:
                            raise MailTMError("Mail.tm не вернул ID письма.")
                        if message_id in checked:
                            continue
                        detail = await self._poll("/messages/" + message_id, token, poll_interval)
                        if not isinstance(detail, dict):
                            raise MailTMError("Mail.tm вернул неверный формат письма.")
                        html = detail.get("html")
                        if html is None:
                            html = []
                        elif isinstance(html, str):
                            html = [html] if html else []
                        if not isinstance(html, list) or not all(
                            isinstance(item, str) for item in html
                        ):
                            raise MailTMError("Mail.tm вернул неверный HTML письма.")
                        sender = detail.get("from")
                        if sender is not None and not isinstance(sender, dict):
                            raise MailTMError("Mail.tm вернул неверного отправителя.")
                        subject = detail.get("subject")
                        intro = summary.get("intro")
                        text = detail.get("text")
                        sender_address = sender.get("address") if sender else None
                        if any(
                            value is not None and not isinstance(value, str)
                            for value in (subject, intro, text, sender_address)
                        ):
                            raise MailTMError("Mail.tm вернул неверные текстовые поля письма.")
                        message = MailMessage(
                            subject or "",
                            intro or "",
                            text or "",
                            html,
                            message_id=message_id,
                            sender=sender_address or "",
                        )
                        if match is None or match(message):
                            return message
                        checked.add(message_id)
                    await asyncio.sleep(poll_interval)
        except TimeoutError as exc:
            raise MailTimeoutError(address) from exc

    async def get_code(
        self,
        token: str,
        timeout: float = 300,
        poll_interval: float = 5,
        from_contains: str | None = None,
        address: str = "",
    ) -> str | None:
        sender = from_contains.casefold() if from_contains else ""
        message = await self.wait_for_message(
            token,
            match=lambda item: item.code is not None and sender in item.sender.casefold(),
            timeout=timeout,
            poll_interval=poll_interval,
            address=address,
        )
        return message.code
