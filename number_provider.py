"""Provider contract and lifecycle for automatic registration."""

import asyncio
import logging
import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Protocol

from app_logging import log_attempt
from models import CodeChallenge, ProxySettings, RegistrationResult, RegistrationStatus
from registration import RegistrationRejected, RegistrationService, await_cleanup

logger = logging.getLogger(__name__)
DEFAULT_CODE_TIMEOUT = 120.0
PROVIDER_REQUEST_TIMEOUT = 30.0


class ProviderConfigurationError(ValueError):
    """Configuration error with a message safe to show the operator."""


@dataclass(frozen=True, slots=True)
class Activation:
    phone: str
    reference: str = field(repr=False)


class NumberProvider(Protocol):
    async def acquire_number(self) -> Activation: ...

    async def wait_for_code(
        self, activation: Activation, challenge: CodeChallenge
    ) -> str | None: ...

    async def finalize(self, activation: Activation, *, used: bool) -> None: ...


ProviderFactory = Callable[[], AbstractAsyncContextManager[NumberProvider]]


def load_provider_factory(spec: str) -> ProviderFactory:
    module, _, name = spec.partition(":")
    if not module or not name.isidentifier():
        raise ValueError("Укажите провайдера в формате module:factory.")
    try:
        factory = getattr(import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"Не удалось загрузить фабрику {spec}.") from exc
    if not callable(factory):
        raise ValueError(f"{spec} не является фабрикой.")
    return factory


async def _finalize(provider: NumberProvider, activation: Activation, *, used: bool) -> bool:
    action = "complete" if used else "cancel"
    logger.info("Settling provider activation action=%s", action)
    try:
        async with asyncio.timeout(PROVIDER_REQUEST_TIMEOUT):
            await provider.finalize(activation, used=used)
    except Exception as exc:
        logger.warning(
            "Provider finalization failed action=%s error=%s", action, type(exc).__name__
        )
        return False
    logger.info("Provider confirmed activation settlement action=%s", action)
    return True


@dataclass(frozen=True, slots=True)
class _ProviderInput:
    provider: NumberProvider
    activation: Activation
    first_name: str
    code_timeout: float

    async def read_code(self, challenge: CodeChallenge) -> str | None:
        if challenge.delivery in {"SentCodeTypeApp", "SentCodeTypeEmailCode"}:
            logger.info("Provider cannot deliver this Telegram code")
            raise RegistrationRejected("provider_delivery_unsupported")
        try:
            async with asyncio.timeout(self.code_timeout):
                return await self.provider.wait_for_code(self.activation, challenge)
        except TimeoutError as exc:
            logger.warning("Provider code request timed out")
            raise RegistrationRejected("provider_code_timeout") from exc
        except Exception as exc:
            logger.warning("Provider code request failed: %s", type(exc).__name__)
            raise RegistrationRejected("provider_code_error") from exc

    async def read_first_name(self, phone: str) -> str:
        return self.first_name

    async def read_password(self, phone: str) -> None:
        return None


async def register_automatic(
    service: RegistrationService,
    provider: NumberProvider,
    *,
    first_name: str,
    code_timeout: float = DEFAULT_CODE_TIMEOUT,
    proxy: ProxySettings | None = None,
    mail_proxy_url: str | None = None,
) -> RegistrationResult:
    """Process one activation and settle it according to the Telegram outcome."""
    if not math.isfinite(code_timeout) or code_timeout <= 0:
        raise ValueError("code_timeout must be finite and positive")
    if not first_name.strip():
        raise ValueError("first_name must be non-empty")

    with log_attempt():
        logger.info("Requesting a number from the provider")
        try:
            async with asyncio.timeout(PROVIDER_REQUEST_TIMEOUT):
                activation = await provider.acquire_number()
        except TimeoutError:
            logger.warning("Provider acquisition timed out")
            return RegistrationResult(None, RegistrationStatus.FAILED, "provider_acquire_timeout")
        except Exception as exc:
            logger.warning("Provider acquisition failed: %s", type(exc).__name__)
            return RegistrationResult(None, RegistrationStatus.FAILED, "provider_acquire_error")

        logger.info("Provider assigned a number")
        used = False
        try:
            async with service.attempt(
                activation.phone,
                _ProviderInput(provider, activation, first_name, code_timeout),
                proxy=proxy,
                mail_proxy_url=mail_proxy_url,
                max_code_attempts=1,
            ) as result:
                used = result.status == RegistrationStatus.SUCCEEDED
        finally:
            finalized = await await_cleanup(_finalize(provider, activation, used=used))
        return result if finalized else replace(result, provider_cleanup_failed=True)
