from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_price_filter(value: str) -> Decimal | None:
    normalized = value.strip().replace(",", ".")
    if not normalized:
        return None
    try:
        price = Decimal(normalized)
    except InvalidOperation:
        return None
    return price if price >= 0 else None


def format_price_filter(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")
