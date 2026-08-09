from __future__ import annotations

import hashlib
import os
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from .market_sources import relevant_to_query


EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID = "EBAY_DE"


def credentials_configured() -> bool:
    return bool(
        os.environ.get("EBAY_CLIENT_ID", "").strip()
        and os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    )


def importer_ready() -> bool:
    return credentials_configured() or os.environ.get(
        "EBAY_DEAL_IMPORTER_READY", ""
    ).strip().lower() in {"1", "true", "yes"}


def collect(query: str, category_id: str | None, *, limit: int) -> list[dict[str, Any]]:
    if not credentials_configured():
        raise RuntimeError("EBAY_CLIENT_ID und EBAY_CLIENT_SECRET fehlen.")

    with requests.Session() as session:
        token = _access_token(session)
        response = session.get(
            EBAY_SEARCH_URL,
            params=_search_params(query, category_id, limit),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Accept-Language": "de-DE",
                "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()

    summaries = payload.get("itemSummaries") if isinstance(payload, dict) else []
    if not isinstance(summaries, list):
        return []

    results: list[dict[str, Any]] = []
    for item in summaries:
        if not isinstance(item, dict):
            continue
        result = _normalize_item(item, query)
        if result and relevant_to_query(query, result):
            results.append(result)
    return results


def _access_token(session: requests.Session) -> str:
    response = session.post(
        EBAY_TOKEN_URL,
        auth=(
            os.environ["EBAY_CLIENT_ID"].strip(),
            os.environ["EBAY_CLIENT_SECRET"].strip(),
        ),
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=25,
    )
    response.raise_for_status()
    token = response.json().get("access_token", "")
    if not isinstance(token, str) or not token:
        raise RuntimeError("eBay hat kein Zugriffstoken geliefert.")
    return token


def _search_params(query: str, category_id: str | None, limit: int) -> dict[str, str]:
    params = {
        "q": query,
        "limit": str(max(1, min(50, limit))),
        "filter": "sellerAccountTypes:{INDIVIDUAL}",
    }
    if category_id:
        params["category_ids"] = category_id
    return params


def _normalize_item(item: dict[str, Any], query: str) -> dict[str, Any] | None:
    title = _text(item.get("title"))
    if not title:
        return None
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    amount = _decimal(price.get("value"))
    currency = _text(price.get("currency"))
    image = item.get("image") if isinstance(item.get("image"), dict) else {}
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    item_id = _text(item.get("itemId")) or _text(item.get("legacyItemId"))
    url = _text(item.get("itemWebUrl"))
    if not url and item_id:
        url = f"https://www.ebay.de/itm/{item_id.split('|')[1] if '|' in item_id else item_id}"
    if not url:
        return None
    if not item_id:
        item_id = hashlib.sha256(url.encode("utf-8")).hexdigest()

    condition = _text(item.get("condition"))
    description = " ".join(part for part in [title, condition] if part)
    return {
        "source": "ebay_active",
        "source_item_id": item_id,
        "title": title,
        "url": url,
        "image_url": _text(image.get("imageUrl")),
        "price_value": amount,
        "price_raw": _price_text(amount, currency),
        "currency": currency,
        "condition_text": condition,
        "seller_account_type": "individual",
        "seller_name": _text(seller.get("username")),
        "listing_end": _text(item.get("itemEndDate")),
        "description": description,
        "raw_result": {
            "query": query,
            "marketplace": MARKETPLACE_ID,
            "legacy_item_id": _text(item.get("legacyItemId")),
            "buying_options": item.get("buyingOptions") or [],
            "bid_count": item.get("bidCount"),
            "category_id": _text(item.get("categoryId")),
            "category_path": _text(item.get("categoryPath")),
            "item_location": item.get("itemLocation") or {},
            "seller": {
                "username": _text(seller.get("username")),
                "feedback_percentage": seller.get("feedbackPercentage"),
                "feedback_score": seller.get("feedbackScore"),
            },
        },
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price_text(value: Decimal | None, currency: str) -> str:
    if value is None:
        return ""
    rendered = f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{rendered} {currency}".strip()


def _text(value: Any) -> str:
    return str(value or "").strip()
