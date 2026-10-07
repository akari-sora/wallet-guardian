"""Integer raw amounts and Decimal valuations. Binary floats are not an accounting source."""

from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 80

UINT256_MAX = 2**256 - 1


def parse_raw(value: str | int) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("raw amount is missing")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if not text or text[0] == "-" or not text.isdigit():
            raise ValueError("raw amount must be a non-negative integer string")
        number = int(text)
    if number < 0 or number > UINT256_MAX:
        raise ValueError("raw amount is outside uint256")
    return number


def format_units(raw: str | int, decimals: int) -> str:
    amount = Decimal(parse_raw(raw)) / (Decimal(10) ** decimals)
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_or_none(value: str | int | Decimal | None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        raise ValueError("prices must not be binary floats")
    return Decimal(str(value))
