"""Run a single panel batch and bridge registration prompts to the browser."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from secrets import token_urlsafe
from typing import Literal

from models import CodeChallenge, ProxySettings, RegistrationResult
from number_provider import (
    NumberProvider,
    ProviderConfigurationError,
    ProviderFactory,
    register_automatic,
)
from proxy_config import ProxyRotation
from registration import RegistrationRejected, RegistrationService

logger = logging.getLogger(__name__)
INPUT_TIMEOUT = 300


@dataclass(slots=True)
class JobRow:
    phone: str | None
    status: str = "queued"
    reason: str | None = None
    provider_cleanup_failed: bool = False

    def finish(self, result: RegistrationResult) -> None:
        self.phone = result.phone or self.phone
        self.status = result.status
        self.reason = result.reason
        self.provider_cleanup_failed = result.provider_cleanup_failed


@dataclass(slots=True)
class PendingInput:
    id: str
    kind: Literal["code", "password", "first_name"]
    phone: str
    answer: asyncio.Future[str | None] = field(repr=False)
    challenge: CodeChallenge | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "phone": self.phone,
            "challenge": asdict(self.challenge) if self.challenge else None,
        }


class PanelJobs:
    def __init__(
        self,
        service: RegistrationService,
        provider_factory: ProviderFactory | None = None,
        *,
        proxy: ProxySettings | None = None,
        mail_proxy_url: str | None = None,
    ) -> None:
        self.service = service
        self.provider_factory = provider_factory
        self.proxy = proxy
        self.mail_proxy_url = mail_proxy_url
        self.rows: list[JobRow] = []
        self._task: asyncio.Task[None] | None = None
        self._prompt: PendingInput | None = None
        self.stopping = False
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict:
        prompt = self._prompt
        return {
            "running": self.running,
            "stopping": self.running and self.stopping,
            "auto_available": self.provider_factory is not None,
            "rows": [asdict(row) for row in self.rows],
            "prompt": prompt.snapshot() if prompt and not prompt.answer.done() else None,
            "error": self.error,
        }

    def start(
        self,
        phones: list[str],
        *,
        count: int = 0,
        first_name: str = "Test",
        rotation: ProxyRotation | None = None,
    ) -> None:
        if self.running:
            raise RuntimeError("Очередь уже выполняется.")
        self.rows = [JobRow(phone) for phone in phones] or [JobRow(None) for _ in range(count)]
        self.stopping = False
        self.error = None
        self._task = asyncio.create_task(
            self._run(automatic=count > 0, first_name=first_name, rotation=rotation)
        )

    def cancel(self) -> None:
        if self._task is None or not self.running or self.stopping:
            return
        self.stopping = True
        for row in self.rows:
            if row.status in {"queued", "running"}:
                row.status = "cancelled"
        self._task.cancel()

    async def close(self) -> None:
        self.cancel()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    def answer(self, prompt_id: str, value: str | None) -> bool:
        prompt = self._prompt
        if self.stopping or prompt is None or prompt.id != prompt_id or prompt.answer.done():
            return False
        prompt.answer.set_result(value)
        return True

    async def read_code(self, challenge: CodeChallenge) -> str | None:
        return await self._read("code", challenge.phone, challenge)

    async def read_password(self, phone: str) -> str | None:
        return await self._read("password", phone)

    async def read_first_name(self, phone: str) -> str | None:
        return await self._read("first_name", phone)

    async def _read(
        self,
        kind: Literal["code", "password", "first_name"],
        phone: str,
        challenge: CodeChallenge | None = None,
    ) -> str | None:
        answer = asyncio.get_running_loop().create_future()
        self._prompt = PendingInput(token_urlsafe(16), kind, phone, answer, challenge)
        try:
            async with asyncio.timeout(INPUT_TIMEOUT):
                return await answer
        except TimeoutError as exc:
            raise RegistrationRejected("panel_input_timeout") from exc
        finally:
            self._prompt = None

    async def _run(
        self, *, automatic: bool, first_name: str, rotation: ProxyRotation | None
    ) -> None:
        try:
            if automatic:
                if self.provider_factory is None:
                    raise RuntimeError("Automatic provider is missing")
                async with self.provider_factory() as provider:
                    await self._process_rows(provider, first_name, rotation)
            else:
                await self._process_rows(None, first_name, rotation)
        except Exception as exc:
            logger.error("Panel batch failed: %s", type(exc).__name__)
            self.error = (
                str(exc)
                if isinstance(exc, ProviderConfigurationError)
                else "Не удалось завершить очередь."
            )
            for row in self.rows:
                if row.status in {"queued", "running"}:
                    row.status = "failed"
                    row.reason = "registration_error"

    async def _process_rows(
        self, provider: NumberProvider | None, first_name: str, rotation: ProxyRotation | None
    ) -> None:
        for index, row in enumerate(self.rows):
            row.status = "running"
            mail_proxy_url, proxy = (
                rotation.for_attempt(index) if rotation else (self.mail_proxy_url, self.proxy)
            )
            if provider is None:
                if row.phone is None:
                    raise RuntimeError("Manual row has no phone")
                result = await self.service.register(
                    row.phone, self, proxy=proxy, mail_proxy_url=mail_proxy_url
                )
            else:
                result = await register_automatic(
                    self.service,
                    provider,
                    first_name=first_name,
                    proxy=proxy,
                    mail_proxy_url=mail_proxy_url,
                )
            row.finish(result)
