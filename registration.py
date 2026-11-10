"""Async Telegram login and registration, independent of the input surface."""

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from opentele2.tl import TelegramClient
from telethon import errors, functions
from telethon.tl import types as t

from mail_tm import MailTimeoutError, MailTM, MailTMError
from models import (
    CodeChallenge,
    LoginAttempt,
    ProxySettings,
    RegistrationInput,
    RegistrationResult,
    RegistrationStatus,
    SentCodeResponse,
)
from phone_numbers import normalize_phone
from telegram_gateway import accept_authorization, build_client, send_code

logger = logging.getLogger(__name__)
EMAIL_TIMEOUT_SECONDS = 180
MAX_CODE_ATTEMPTS = 3


class RegistrationRejected(Exception):
    """An expected workflow outcome with a stable reason for callers."""


class RegistrationService:
    """Use one instance per process to protect concurrent access to session files."""

    def __init__(self, device_token: str) -> None:
        self._device_token = device_token
        self._active_phones: set[str] = set()

    def is_active(self, phone: str) -> bool:
        return phone in self._active_phones

    async def register(
        self,
        phone: str,
        inputs: RegistrationInput,
        *,
        proxy: ProxySettings | None = None,
        mail_proxy_url: str | None = None,
        max_code_attempts: int = MAX_CODE_ATTEMPTS,
    ) -> RegistrationResult:
        async with self.attempt(
            phone,
            inputs,
            proxy=proxy,
            mail_proxy_url=mail_proxy_url,
            max_code_attempts=max_code_attempts,
        ) as result:
            return result

    @asynccontextmanager
    async def attempt(
        self,
        phone: str,
        inputs: RegistrationInput,
        *,
        proxy: ProxySettings | None = None,
        mail_proxy_url: str | None = None,
        max_code_attempts: int = MAX_CODE_ATTEMPTS,
    ) -> AsyncIterator[RegistrationResult]:
        """Expose the outcome before disconnecting so callers can settle external reservations."""
        if max_code_attempts < 1:
            raise ValueError("max_code_attempts must be positive")
        try:
            phone = normalize_phone(phone)
        except ValueError:
            yield RegistrationResult(None, RegistrationStatus.FAILED, "invalid_phone")
            return
        if phone in self._active_phones:
            yield RegistrationResult(phone, RegistrationStatus.FAILED, "already_running")
            return

        self._active_phones.add(phone)
        attempt = None
        try:
            status = RegistrationStatus.FAILED
            reason = None
            try:
                attempt = await arm(phone, self._device_token, proxy=proxy)
                await complete(
                    attempt,
                    inputs,
                    mail_proxy_url=mail_proxy_url,
                    max_code_attempts=max_code_attempts,
                )
                status = (
                    RegistrationStatus.ALREADY_AUTHORIZED
                    if attempt.existing_session
                    else RegistrationStatus.SUCCEEDED
                )
            except RegistrationRejected as exc:
                reason = str(exc)
            except MailTimeoutError:
                reason = "mail_timeout"
            except MailTMError:
                reason = "mail_error"
            except errors.FloodWaitError as exc:
                logger.info("Telegram requested a %s-second wait", exc.seconds)
                reason = "telegram_rate_limited"
            except errors.RPCError as exc:
                logger.warning("Telegram RPC failed: %s", type(exc).__name__)
                reason = "telegram_error"
            except EOFError:
                raise
            except Exception as exc:
                logger.error("Registration failed: %s", type(exc).__name__)
                reason = "registration_error"
            yield RegistrationResult(phone, status, reason)
        finally:
            try:
                if attempt is not None:
                    await disconnect_client(attempt.client)
            finally:
                self._active_phones.remove(phone)


async def disconnect_client(client: TelegramClient) -> None:
    """Cleanup must not mask the outcome or an active exception."""
    await await_cleanup(_disconnect(client))


async def await_cleanup[T](operation: Coroutine[Any, Any, T]) -> T:
    """Wait for resource cleanup before propagating cancellation to the caller."""
    task = asyncio.create_task(operation)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception as exc:
        logger.warning("Could not disconnect Telegram client: %s", type(exc).__name__)


async def arm(phone: str, device_token: str, *, proxy: ProxySettings | None = None) -> LoginAttempt:
    client = build_client(phone, device_token, proxy=proxy)
    attempt = None
    try:
        await client.connect()
        if await client.is_user_authorized():
            attempt = LoginAttempt(
                client=client, phone=phone, authorized=True, existing_session=True
            )
            return attempt

        response = await send_code(client, phone)
        if isinstance(response, t.auth.SentCodePaymentRequired):
            raise RegistrationRejected("payment_required")
        if isinstance(response, t.auth.SentCodeSuccess):
            await accept_authorization(client, response.authorization.user)
            attempt = LoginAttempt(client=client, phone=phone, authorized=True)
        else:
            attempt = LoginAttempt(client=client, phone=phone, sent_code=response)
        return attempt
    finally:
        if attempt is None:
            await disconnect_client(client)


async def setup_email(
    client: TelegramClient, phone: str, phone_code_hash: str, *, mail_proxy_url: str | None = None
) -> tuple[SentCodeResponse, str | None]:
    purpose = t.EmailVerifyPurposeLoginSetup(phone_number=phone, phone_code_hash=phone_code_hash)
    async with MailTM(proxy_url=mail_proxy_url) as mail:
        account = await mail.create_account()
        sent_email = await client(
            functions.account.SendVerifyEmailCodeRequest(purpose=purpose, email=account.address)
        )
        confirmation = await mail.wait_for_message(
            account.token,
            match=lambda message: bool(message.code) and len(message.code) == sent_email.length,
            timeout=EMAIL_TIMEOUT_SECONDS,
            address=account.address,
        )
        verified = await client(
            functions.account.VerifyEmailRequest(
                purpose=purpose, verification=t.EmailVerificationCode(code=confirmation.code)
            )
        )
        if not isinstance(verified, t.account.EmailVerifiedLogin):
            raise RegistrationRejected("unexpected_email_response")

        response = verified.sent_code
        if isinstance(response, t.auth.SentCode) and isinstance(
            response.type, t.auth.SentCodeTypeEmailCode
        ):
            message = await mail.wait_for_message(
                account.token,
                match=lambda message: (
                    message.id != confirmation.id
                    and bool(message.code)
                    and len(message.code) == response.type.length
                ),
                timeout=EMAIL_TIMEOUT_SECONDS,
                address=account.address,
            )
            return response, message.code
        return response, None


async def sign_up(
    client: TelegramClient, phone: str, phone_code_hash: str, inputs: RegistrationInput
) -> None:
    first_name = (await inputs.read_first_name(phone) or "").strip()
    if not first_name:
        raise RegistrationRejected("first_name_required")
    authorization = await client(
        functions.auth.SignUpRequest(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            first_name=first_name,
            last_name="",
        )
    )
    await accept_authorization(client, authorization.user)


async def finish_login(
    client: TelegramClient,
    phone: str,
    sent: t.auth.SentCode,
    inputs: RegistrationInput,
    mail_code: str | None = None,
    *,
    max_code_attempts: int = MAX_CODE_ATTEMPTS,
) -> None:
    phone_code_hash = sent.phone_code_hash
    by_email = isinstance(sent.type, t.auth.SentCodeTypeEmailCode)
    attempts = 1 if mail_code is not None else max_code_attempts
    for number in range(1, attempts + 1):
        code = mail_code
        if code is None:
            code = await inputs.read_code(
                CodeChallenge(
                    phone=phone,
                    delivery=type(sent.type).__name__,
                    length=getattr(sent.type, "length", None),
                    email_pattern=sent.type.email_pattern if by_email else None,
                    attempt=number,
                    max_attempts=attempts,
                )
            )
        code = (code or "").strip()
        if not code:
            raise RegistrationRejected("code_missing")
        try:
            if by_email:
                result = await client(
                    functions.auth.SignInRequest(
                        phone_number=phone,
                        phone_code_hash=phone_code_hash,
                        email_verification=t.EmailVerificationCode(code=code),
                    )
                )
            else:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                return
        except errors.PhoneCodeInvalidError as exc:
            if number == attempts:
                raise RegistrationRejected("code_invalid") from exc
            continue
        except errors.PhoneCodeExpiredError as exc:
            raise RegistrationRejected("code_expired") from exc
        except errors.SessionPasswordNeededError:
            password = await inputs.read_password(phone)
            if not password:
                raise RegistrationRejected("password_required") from None
            await client.sign_in(password=password)
            return
        except errors.PhoneNumberUnoccupiedError:
            await sign_up(client, phone, phone_code_hash, inputs)
            return
        if isinstance(result, t.auth.AuthorizationSignUpRequired):
            await sign_up(client, phone, phone_code_hash, inputs)
        else:
            await accept_authorization(client, result.user)
        return


async def complete(
    attempt: LoginAttempt,
    inputs: RegistrationInput,
    *,
    mail_proxy_url: str | None = None,
    max_code_attempts: int = MAX_CODE_ATTEMPTS,
) -> None:
    client, phone = attempt.client, attempt.phone
    if attempt.authorized:
        return
    response = attempt.sent_code
    if response is None:
        raise ValueError("Pending login has no sent code.")
    phone_code_hash = response.phone_code_hash
    mail_code = None

    if isinstance(response.type, t.auth.SentCodeTypeSetUpEmailRequired):
        response, mail_code = await setup_email(
            client, phone, phone_code_hash, mail_proxy_url=mail_proxy_url
        )
    elif isinstance(response.type, t.auth.SentCodeTypeApp):
        if response.next_type is None:
            raise RegistrationRejected("app_code_without_fallback")
        response = await client(functions.auth.ResendCodeRequest(phone, phone_code_hash))

    if isinstance(response, t.auth.SentCodePaymentRequired):
        raise RegistrationRejected("payment_required")
    if isinstance(response, t.auth.SentCodeSuccess):
        await accept_authorization(client, response.authorization.user)
        return
    if not isinstance(response, t.auth.SentCode):
        raise RuntimeError(f"Unexpected Telegram response: {type(response).__name__}")
    await finish_login(
        client, phone, response, inputs, mail_code, max_code_attempts=max_code_attempts
    )
