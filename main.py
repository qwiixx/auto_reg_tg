"""CLI for manual and provider-backed registration."""

import argparse
import asyncio
import math
import os
from pathlib import Path

from console import ConsoleInput, get_phone_number, get_proxy, get_proxy_interval
from models import RegistrationResult, RegistrationStatus
from number_provider import DEFAULT_CODE_TIMEOUT, load_provider_factory, register_automatic
from phone_numbers import normalize_phone, parse_phone_list
from proxy_config import parse_proxy
from registration import RegistrationService
from telegram_gateway import SESSIONS_DIR

FAILURE_MESSAGES = {
    "already_running": "этот номер уже обрабатывается",
    "app_code_without_fallback": "доступен только код в приложении Telegram",
    "code_expired": "срок действия кода истёк",
    "code_invalid": "Telegram отклонил код",
    "code_missing": "код не получен или пропущен",
    "first_name_required": "требуется имя для нового аккаунта",
    "invalid_phone": "некорректный номер",
    "mail_error": "ошибка Mail.tm",
    "mail_timeout": "письмо с кодом не пришло вовремя",
    "password_required": "требуется пароль 2FA",
    "payment_required": "Telegram запросил платную верификацию",
    "provider_acquire_error": "провайдер не выдал номер",
    "provider_acquire_timeout": "провайдер не выдал номер вовремя",
    "provider_code_error": "ошибка получения кода у провайдера",
    "provider_code_timeout": "провайдер не прислал код вовремя",
    "provider_delivery_unsupported": "этот канал доставки требует ручного ввода",
    "registration_error": "ошибка регистрации",
    "telegram_error": "Telegram вернул ошибку",
    "telegram_rate_limited": "Telegram ограничил частоту запросов",
    "unexpected_email_response": "неожиданный ответ при подтверждении почты",
}


def show_result(result: RegistrationResult) -> None:
    target = result.phone or "Получение номера"
    if result.status == RegistrationStatus.ALREADY_AUTHORIZED:
        print(f"{target}: сессия уже авторизована.")
    elif result.status == RegistrationStatus.SUCCEEDED:
        print(f"{target}: вход завершён. Сессии в {SESSIONS_DIR}.")
    else:
        message = FAILURE_MESSAGES.get(result.reason or "", "вход не завершён")
        print(f"{target}: {message}.")
    if result.provider_cleanup_failed:
        print("  Провайдер не подтвердил завершение активации.")


async def run_auto(
    args: argparse.Namespace, parser: argparse.ArgumentParser, service: RegistrationService
) -> None:
    if args.count < 1 or not math.isfinite(args.code_timeout) or args.code_timeout <= 0:
        parser.error("--count и --code-timeout должны быть положительными конечными числами.")
    if not args.first_name.strip():
        parser.error("--first-name не может быть пустым.")
    try:
        proxy = parse_proxy(args.proxy) if args.proxy else None
        factory = load_provider_factory(args.provider)
    except ValueError as exc:
        parser.error(str(exc))

    async with factory() as provider:
        for _ in range(args.count):
            show_result(
                await register_automatic(
                    service,
                    provider,
                    first_name=args.first_name,
                    code_timeout=args.code_timeout,
                    proxy=proxy,
                    mail_proxy_url=args.proxy,
                )
            )


async def run_manual(
    args: argparse.Namespace, parser: argparse.ArgumentParser, service: RegistrationService
) -> None:
    try:
        if args.file is not None:
            phones = parse_phone_list(args.file.read_text(encoding="utf-8-sig"))
        elif args.phone is not None:
            phones = [normalize_phone(args.phone)]
        else:
            phones = iter(get_phone_number, None)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print("=== Ручная регистрация Telegram ===")
    interval = get_proxy_interval()
    proxy_url, proxy = None, None
    inputs = ConsoleInput()
    for index, phone in enumerate(phones):
        if index % interval == 0:
            proxy_url, proxy = get_proxy(proxy_url, proxy)
        print(f"\nЗапрашиваю код для {phone}…")
        show_result(await service.register(phone, inputs, proxy=proxy, mail_proxy_url=proxy_url))


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Регистрация и вход в Telegram.")
    parser.set_defaults(file=None, phone=None)
    modes = parser.add_subparsers(dest="mode")
    manual = modes.add_parser("manual", help="Ручной ввод номеров и кодов")
    source = manual.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="UTF-8 файл, один номер на строку")
    source.add_argument("--phone", help="Один номер в международном формате")
    auto = modes.add_parser("auto", help="Номера и коды через API провайдера")
    auto.add_argument(
        "--provider", required=True, help="Фабрика провайдера в формате module:factory"
    )
    auto.add_argument("--count", type=int, default=1, help="Количество попыток, по умолчанию 1")
    auto.add_argument(
        "--code-timeout", type=float, default=DEFAULT_CODE_TIMEOUT, help="Ожидание кода в секундах"
    )
    auto.add_argument("--first-name", default="Test", help="Имя для нового аккаунта")
    auto.add_argument("--proxy", help="HTTP/SOCKS5 прокси для Telegram и Mail.tm")
    args = parser.parse_args(argv)
    service = RegistrationService(os.getenv("DEVICE_TOKEN", "").strip())
    if args.mode == "auto":
        await run_auto(args, auto, service)
    else:
        await run_manual(args, manual, service)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nВыход.")
