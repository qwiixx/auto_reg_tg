"""Proxy URL validation and per-batch rotation."""

from dataclasses import dataclass, field
from importlib.util import find_spec

from models import ProxySettings

PROXY_INTERVALS = (2, 3, 4, 5)
DEFAULT_NUMBERS_PER_PROXY = 4
MAX_PROXY_FILE_SIZE = 64 * 1024


def parse_proxy(url: str) -> ProxySettings:
    if find_spec("aiohttp_socks") is None:
        raise ValueError("Для прокси установите aiohttp-socks.")
    from python_socks import ProxyType, parse_proxy_url

    try:
        protocol, host, port, username, password = parse_proxy_url(url)
    except ValueError as exc:
        raise ValueError("Проверьте формат URL прокси и порт.") from exc
    if protocol not in {ProxyType.HTTP, ProxyType.SOCKS5} or not port:
        raise ValueError("Введите HTTP или SOCKS5-прокси с портом.")
    return {
        "proxy_type": protocol,
        "addr": host,
        "port": port,
        "username": username or None,
        "password": password or None,
        "rdns": True,
    }


@dataclass(frozen=True, slots=True)
class ProxyRotation:
    targets: tuple[tuple[str, ProxySettings], ...] = field(repr=False)
    numbers_per_proxy: int

    def for_attempt(self, index: int) -> tuple[str, ProxySettings]:
        return self.targets[index // self.numbers_per_proxy % len(self.targets)]


def parse_proxy_rotation(content: object, numbers_per_proxy: object) -> ProxyRotation:
    if (
        not isinstance(content, str)
        or len(content.encode("utf-8", errors="surrogatepass")) > MAX_PROXY_FILE_SIZE
    ):
        raise ValueError("Загрузите UTF-8 файл прокси размером до 64 КБ.")
    if type(numbers_per_proxy) is not int or numbers_per_proxy not in PROXY_INTERVALS:
        raise ValueError("Выберите от 2 до 5 номеров на один прокси.")

    targets = []
    for line_number, line in enumerate(content.removeprefix("\ufeff").splitlines(), start=1):
        url = line.strip()
        if not url:
            continue
        try:
            targets.append((url, parse_proxy(url)))
        except ValueError as exc:
            raise ValueError(f"Прокси, строка {line_number}: {exc}") from exc
    if not targets:
        raise ValueError("Список прокси пуст.")
    return ProxyRotation(tuple(targets), numbers_per_proxy)
