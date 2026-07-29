from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import requests


SERPAPI_URL = "https://serpapi.com/search.json"


class SerpApiError(RuntimeError):
    pass


def search_sold(
    *,
    api_key: str,
    query: str,
    ebay_domain: str,
    category_id: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    params = {
        "engine": "ebay",
        "ebay_domain": ebay_domain,
        "_nkw": query,
        "_pgn": "1",
        "_ipg": "200",
        "show_only": "Sold",
        "LH_ItemCondition": "3000|10",
        "LH_PrefLoc": "1",
        "no_cache": "false",
        "api_key": api_key,
    }
    if category_id:
        params["category_id"] = category_id

    try:
        response = requests.get(
            SERPAPI_URL,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "klein-antik/0.1"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        message = str(exc).replace(api_key, "[redacted]")
        raise SerpApiError(f"SerpApi-Verbindungsfehler: {message}") from None

    try:
        data = response.json()
    except requests.JSONDecodeError:
        raise SerpApiError(f"SerpApi HTTP {response.status_code}: ungueltige JSON-Antwort.") from None
    if not isinstance(data, dict):
        raise SerpApiError("SerpApi hat keine JSON-Struktur geliefert.")
    if not response.ok:
        message = str(data.get("error") or "Anfrage abgelehnt.")
        raise SerpApiError(f"SerpApi HTTP {response.status_code}: {message}")
    if data.get("error"):
        raise SerpApiError(str(data["error"]))
    status = str((data.get("search_metadata") or {}).get("status") or "")
    if status and status.lower() != "success":
        raise SerpApiError(f"SerpApi-Status: {status}")
    return data


def product_id_for(result: dict[str, Any]) -> str:
    product_id = str(result.get("product_id") or "").strip()
    if product_id:
        return product_id

    link = str(result.get("link") or "")
    match = re.search(r"/itm/(?:[^/?]+/)?(\d{9,15})", link)
    if match:
        return match.group(1)

    fallback = "\n".join(
        [
            link,
            str(result.get("title") or ""),
            str(result.get("thumbnail") or ""),
        ]
    )
    return f"fallback:{hashlib.sha256(fallback.encode('utf-8')).hexdigest()[:32]}"


def parse_price(value: Any) -> tuple[Decimal | None, str, str]:
    if isinstance(value, dict):
        if value.get("extracted") is not None:
            raw = str(value.get("raw") or "")
            return _decimal(value.get("extracted")), raw, currency_from_text(raw)
        if isinstance(value.get("from"), dict):
            amount, raw, currency = parse_price(value["from"])
            return amount, raw, currency
        if isinstance(value.get("to"), dict):
            return parse_price(value["to"])
        raw = str(value.get("raw") or "")
    else:
        raw = str(value or "")

    currency = currency_from_text(raw)

    cleaned = re.sub(r"[^\d,.\-]", "", raw)
    if not cleaned:
        return None, raw, currency
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        decimal_part = cleaned.rsplit(",", 1)[-1]
        cleaned = cleaned.replace(".", "")
        cleaned = cleaned.replace(",", "." if len(decimal_part) <= 2 else "")
    try:
        return Decimal(cleaned), raw, currency
    except InvalidOperation:
        return None, raw, currency


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def currency_from_text(raw: str) -> str:
    upper = raw.upper()
    if "EUR" in upper or "€" in raw:
        return "EUR"
    if "GBP" in upper or "£" in raw:
        return "GBP"
    if "USD" in upper or "$" in raw:
        return "USD"
    return ""


def normalized_result(result: dict[str, Any]) -> dict[str, Any]:
    amount, price_raw, currency = parse_price(result.get("price"))
    if not currency:
        domain_currency = str(result.get("currency") or "").upper()
        currency = domain_currency if len(domain_currency) == 3 else ""
    seller = result.get("seller") if isinstance(result.get("seller"), dict) else {}
    return {
        "product_id": product_id_for(result),
        "title": str(result.get("title") or "eBay-Listing").strip(),
        "url": str(result.get("link") or "").strip(),
        "image_url": str(result.get("thumbnail") or "").strip(),
        "price_value": amount,
        "price_raw": price_raw,
        "currency": currency,
        "condition_text": str(result.get("condition") or "").strip(),
        "sold_date": str(result.get("sold_date") or "").strip(),
        "shipping_text": str(result.get("shipping") or "").strip(),
        "seller": seller,
        "raw_result": result,
    }
