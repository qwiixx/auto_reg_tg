"""Explicit state passed between registration stages."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypedDict

from opentele2.tl import TelegramClient
from python_socks import ProxyType
from telethon.tl import types as t


class ProxySettings(TypedDict):
    proxy_type: ProxyType
    addr: str
    port: int
    username: str | None
    password: str | None
    rdns: bool


SentCodeResponse = t.auth.SentCode | t.auth.SentCodeSuccess | t.auth.SentCodePaymentRequired


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    client: TelegramClient = field(repr=False)
    phone: str
    sent_code: t.auth.SentCode | None = field(default=None, repr=False)
    authorized: bool = False
    existing_session: bool = False

    def __post_init__(self) -> None:
        if self.authorized == (self.sent_code is not None):
            raise ValueError("Expected either an authorized session or a pending code.")
        if self.existing_session and not self.authorized:
            raise ValueError("An existing session must be authorized.")


@dataclass(frozen=True, slots=True)
class CodeChallenge:
    phone: str
    delivery: str
    length: int | None
    email_pattern: str | None
    attempt: int
    max_attempts: int


class RegistrationInput(Protocol):
    async def read_code(self, challenge: CodeChallenge) -> str | None: ...

    async def read_password(self, phone: str) -> str | None: ...

    async def read_first_name(self, phone: str) -> str | None: ...


class RegistrationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_AUTHORIZED = "already_authorized"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    phone: str | None
    status: RegistrationStatus
    reason: str | None = None
    provider_cleanup_failed: bool = False
