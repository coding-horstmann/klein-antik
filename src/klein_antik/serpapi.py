from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import requests


SERPAPI_URL = "https://serpapi.com/search.json"
SOLD_MARKERS = {
    "ebay.de": "Dieses Angebot wurde verkauft am",
    "ebay.com": "This listing sold on",
    "ebay.co.uk": "This listing sold on",
    "ebay.fr": "Cet objet a ete vendu le",
}
DUCKDUCKGO_LOCALES = {
    "ebay.de": "de-de",
    "ebay.com": "us-en",
    "ebay.co.uk": "uk-en",
    "ebay.fr": "fr-fr",
}
GENERIC_QUERY_TOKENS = {
    "art",
    "deco",
    "design",
    "figur",
    "glass",
    "glas",
    "jugendstil",
    "keramik",
    "lampe",
    "metall",
    "metallobjekt",
    "nouveau",
    "numbered",
    "schmuck",
    "signed",
    "silber",
    "silver",
    "vase",
    "vintage",
}


class SerpApiError(RuntimeError):
    def __init__(self, message: str, *, calls_used: int = 1) -> None:
        super().__init__(message)
        self.calls_used = calls_used


def _request(
    *,
    api_key: str,
    params: dict[str, str],
    timeout: int,
    allow_no_results: bool = False,
) -> dict[str, Any]:
    try:
        response = requests.get(
            SERPAPI_URL,
            params={**params, "no_cache": "false", "api_key": api_key},
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
        message = str(data["error"])
        if allow_no_results and "hasn't returned any results" in message.lower():
            return data
        raise SerpApiError(message)
    status = str((data.get("search_metadata") or {}).get("status") or "")
    if status and status.lower() != "success":
        raise SerpApiError(f"SerpApi-Status: {status}")
    return data


def search_sold(
    *,
    api_key: str,
    query: str,
    ebay_domain: str,
    category_id: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    del category_id
    sold_marker = SOLD_MARKERS.get(ebay_domain, "This listing sold on")
    discovery = _request(
        api_key=api_key,
        params={
            "engine": "duckduckgo",
            "q": f'site:{ebay_domain}/itm "{sold_marker}" {query}',
            "kl": DUCKDUCKGO_LOCALES.get(ebay_domain, "wt-wt"),
            "m": "50",
            "safe": "-2",
        },
        timeout=timeout,
        allow_no_results=True,
    )
    candidates = sold_candidates(
        query=query,
        ebay_domain=ebay_domain,
        results=discovery.get("organic_results") or [],
    )
    detail_calls = 0
    detail_error = ""
    target = next((item for item in candidates if item.get("price")), None)
    if target is None and candidates:
        target = candidates[0]
    if target:
        detail_calls = 1
        try:
            detail = _request(
                api_key=api_key,
                params={
                    "engine": "ebay_product",
                    "ebay_domain": ebay_domain,
                    "product_id": target["product_id"],
                },
                timeout=timeout,
            )
            enrich_with_product_detail(target, detail)
        except SerpApiError as exc:
            detail_error = str(exc)

    search_information = discovery.get("search_information") or {}
    return {
        "search_metadata": discovery.get("search_metadata") or {},
        "search_information": {
            **search_information,
            "total_results": len(candidates),
        },
        "organic_results": candidates,
        "source_engine": "duckduckgo",
        "source_response": discovery,
        "detail_error": detail_error,
        "_discovery_calls_used": 1,
        "_detail_calls_used": detail_calls,
    }


def sold_candidates(
    *,
    query: str,
    ebay_domain: str,
    results: Any,
) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in results:
        if not isinstance(raw, dict):
            continue
        link = str(raw.get("link") or "").strip()
        parsed = urlparse(link)
        if parsed.netloc.lower().removeprefix("www.") != ebay_domain.lower():
            continue
        if "/itm/" not in parsed.path:
            continue
        snippet = str(raw.get("snippet") or "")
        if not is_sold_snippet(snippet):
            continue
        title = re.sub(
            r"\s*\|\s*eBay(?:\.[a-z.]+|\s+[A-Z]{2})?\s*$",
            "",
            str(raw.get("title") or ""),
            flags=re.IGNORECASE,
        ).strip()
        if not relevant_to_query(query, f"{title} {snippet}"):
            continue
        product_id = product_id_for({"link": link})
        if product_id in seen or product_id.startswith("fallback:"):
            continue
        seen.add(product_id)
        price_raw = sold_price(snippet)
        sold_date = sold_date_text(snippet)
        candidates.append(
            {
                "product_id": product_id,
                "title": title or "eBay-Listing",
                "link": f"https://www.{ebay_domain}/itm/{product_id}",
                "price": price_raw,
                "sold_date": sold_date,
                "condition": "",
                "thumbnail": "",
                "shipping": "",
                "seller": {},
                "discovery_result": raw,
            }
        )
    return candidates


def normalized_text(value: str) -> str:
    ascii_text = "".join(
        char
        for char in unicodedata.normalize("NFKD", value.replace("ß", "ss"))
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def relevant_to_query(query: str, text: str) -> bool:
    query_tokens = {token for token in normalized_text(query).split() if len(token) >= 3}
    if not query_tokens:
        return True
    text_tokens = set(normalized_text(text).split())
    overlap = query_tokens & text_tokens
    required = 1 if len(query_tokens) <= 2 else 2
    specific_tokens = query_tokens - GENERIC_QUERY_TOKENS
    return len(overlap) >= required and (not specific_tokens or bool(overlap & specific_tokens))


def is_sold_snippet(snippet: str) -> bool:
    text = normalized_text(snippet)
    return any(
        marker in text
        for marker in (
            "dieses angebot wurde verkauft",
            "this listing sold on",
            "cet objet a ete vendu",
        )
    )


def sold_price(snippet: str) -> str:
    patterns = (
        r"\bEUR\s*\d[\d.\s]*(?:,\d{2})?",
        r"\d[\d.\s]*(?:,\d{2})?\s*(?:EUR|€)",
        r"(?:US\s*)?\$\s*\d[\d,]*(?:\.\d{2})?",
        r"£\s*\d[\d,]*(?:\.\d{2})?",
    )
    for pattern in patterns:
        match = re.search(pattern, snippet, flags=re.IGNORECASE)
        if match:
            return re.sub(r"^eur", "EUR", match.group(0).strip(), flags=re.IGNORECASE)
    return ""


def sold_date_text(snippet: str) -> str:
    match = re.search(
        (
            r"(?:Dieses Angebot wurde verkauft am|This listing sold on|"
            r"Cet objet a [ée]t[ée] vendu le)\s+(.+?)(?:\.\s|$)"
        ),
        snippet,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def enrich_with_product_detail(item: dict[str, Any], detail: dict[str, Any]) -> None:
    product = detail.get("product_results") or {}
    if not isinstance(product, dict):
        return
    item["title"] = str(product.get("title") or item["title"]).strip()
    item["condition"] = str(product.get("condition") or "")
    item["thumbnail"] = product_image(product)
    shipping = product.get("shipping") if isinstance(product.get("shipping"), dict) else {}
    item["shipping"] = str(shipping.get("from") or "")
    item["product_detail"] = product
    if not item.get("price"):
        buy = product.get("buy") if isinstance(product.get("buy"), dict) else {}
        buy_now = buy.get("buy_it_now") if isinstance(buy.get("buy_it_now"), dict) else {}
        price = buy_now.get("price") if isinstance(buy_now.get("price"), dict) else {}
        amount = price.get("amount")
        currency = str(price.get("currency") or "")
        if amount is not None and currency:
            item["price"] = {"raw": f"{currency} {amount}", "extracted": amount}


def product_image(product: dict[str, Any]) -> str:
    variants: list[dict[str, Any]] = []
    for media in product.get("media") or []:
        if isinstance(media, dict) and media.get("type") == "image":
            variants.extend(item for item in media.get("image") or [] if isinstance(item, dict))
    if not variants:
        return ""
    variants.sort(key=lambda item: int((item.get("size") or {}).get("width") or 0))
    preferred = [item for item in variants if int((item.get("size") or {}).get("width") or 0) <= 1000]
    return str((preferred or variants)[-1].get("link") or "")


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
