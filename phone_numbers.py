"""Validation shared by console, file uploads and number providers."""

import re

_PHONE = re.compile(r"\+?[0-9() .\t-]+")
_FORMATTING = str.maketrans("", "", "() .\t-")


def normalize_phone(value: str) -> str:
    value = value.strip()
    if _PHONE.fullmatch(value):
        digits = value.removeprefix("+").translate(_FORMATTING)
        if 8 <= len(digits) <= 15 and digits[0] != "0":
            return "+" + digits
    raise ValueError("Введите международный номер: 8–15 цифр, начиная с кода страны.")


def parse_phone_list(content: str) -> list[str]:
    phones: dict[str, None] = {}
    for line_number, line in enumerate(content.removeprefix("\ufeff").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            phones[normalize_phone(line)] = None
        except ValueError as exc:
            raise ValueError(f"Строка {line_number}: {exc}") from exc
    if not phones:
        raise ValueError("Список номеров пуст.")
    return list(phones)
