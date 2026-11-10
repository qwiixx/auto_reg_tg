"""Console input for manual registration and proxy configuration."""

from getpass import getpass

from models import CodeChallenge, ProxySettings
from phone_numbers import normalize_phone
from proxy_config import DEFAULT_NUMBERS_PER_PROXY, PROXY_INTERVALS, parse_proxy


def get_proxy_interval() -> int:
    while True:
        entered = input(
            f"Сколько номеров на один прокси? [2–5, по умолчанию {DEFAULT_NUMBERS_PER_PROXY}]: "
        )
        entered = entered.strip()
        if not entered:
            return DEFAULT_NUMBERS_PER_PROXY
        if entered in tuple(map(str, PROXY_INTERVALS)):
            return int(entered)
        print("Введите число от 2 до 5.")


def get_proxy(
    previous_url: str | None, previous_settings: ProxySettings | None
) -> tuple[str | None, ProxySettings | None]:
    print("Прокси в формате http://хост:порт или socks5://логин:пароль@хост:порт.")
    while True:
        url = input("Прокси (Enter — оставить текущий, - — напрямую): ").strip()
        if not url:
            return previous_url, previous_settings
        if url == "-":
            return None, None
        try:
            return url, parse_proxy(url)
        except ValueError as exc:
            print(exc)


def get_phone_number() -> str | None:
    while True:
        raw = input("\nНомер телефона в международном формате (Enter — выход): ").strip()
        if not raw:
            return None
        try:
            return normalize_phone(raw)
        except ValueError as exc:
            print(exc)


class ConsoleInput:
    async def read_code(self, challenge: CodeChallenge) -> str:
        if challenge.attempt == 1:
            print(f"  Канал доставки: {challenge.delivery}")
        else:
            print(f"  Неверный код. Попытка {challenge.attempt} из {challenge.max_attempts}.")
        target = f" из письма на {challenge.email_pattern}" if challenge.email_pattern else ""
        length = f", {challenge.length} симв." if challenge.length else ""
        return input(f"  Код{target} ({challenge.phone}{length}, Enter — пропуск): ")

    async def read_password(self, phone: str) -> str:
        return getpass(f"  Пароль 2FA ({phone}): ")

    async def read_first_name(self, phone: str) -> str:
        return input(f"  Имя для нового аккаунта ({phone}) [Test]: ").strip() or "Test"
