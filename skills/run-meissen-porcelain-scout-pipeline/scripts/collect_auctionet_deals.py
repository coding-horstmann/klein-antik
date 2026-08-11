from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


SOURCE = "auctionet"
SEARCH_URL = "https://auctionet.com/en/search/9-ceramics-porcelain"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
PAGE_SIZE = 48
USER_AGENT = "KleinAntikMeissenScout/1.0 (auditable research collector)"
FORBIDDEN_MARKERS = ("ebay", "serpapi")
RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("meissen_style", re.compile(r"\bmeissen[- ]?(?:style|like)\b", re.I)),
    ("after_meissen", re.compile(r"\bafter meissen\b", re.I)),
    ("dresden_or_saxony", re.compile(r"\b(?:dresden|saxony|sachsen)\b", re.I)),
    ("reproduction", re.compile(r"\b(?:reproduction|repro|replica|copy)\b", re.I)),
    (
        "quality_or_seconds",
        re.compile(r"\b(?:second|2nd|third|3rd)\s+(?:quality|choice|wahl)\b", re.I),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze current Auctionet Meissen porcelain listings for review."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--query", default="Meissen")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--limit", type=int, default=48)
    parser.add_argument("--run-id")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_amount(value: Any) -> tuple[Decimal | None, str]:
    text = html.unescape(str(value or "")).replace("\u00a0", " ").strip()
    upper = text.upper()
    currency = ""
    for marker, code in (
        ("EUR", "EUR"),
        ("€", "EUR"),
        ("USD", "USD"),
        ("$", "USD"),
        ("GBP", "GBP"),
        ("£", "GBP"),
        ("SEK", "SEK"),
        ("DKK", "DKK"),
        ("NOK", "NOK"),
        ("CHF", "CHF"),
    ):
        if marker in upper:
            currency = code
            break
    match = re.search(r"\d[\d\s.,']*", text)
    if not match:
        return None, currency
    raw_number = match.group(0).replace(" ", "").replace("'", "")
    decimal_separator = ""
    if "," in raw_number and "." in raw_number:
        decimal_separator = "," if raw_number.rfind(",") > raw_number.rfind(".") else "."
    elif "," in raw_number and len(raw_number.rsplit(",", 1)[1]) == 2:
        decimal_separator = ","
    elif "." in raw_number and len(raw_number.rsplit(".", 1)[1]) == 2:
        decimal_separator = "."
    if decimal_separator:
        other_separator = "." if decimal_separator == "," else ","
        normalized = raw_number.replace(other_separator, "").replace(decimal_separator, ".")
    else:
        normalized = raw_number.replace(",", "").replace(".", "")
    try:
        return Decimal(normalized), currency
    except InvalidOperation:
        return None, currency


def extract_items(markup: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(markup, "html.parser")
    items: list[dict[str, Any]] = []
    for node in soup.select("[data-react-props]"):
        raw_props = node.get("data-react-props")
        if not isinstance(raw_props, str):
            continue
        try:
            props = json.loads(html.unescape(raw_props))
        except json.JSONDecodeError:
            continue
        candidate_items = props.get("items") if isinstance(props, dict) else None
        if isinstance(candidate_items, list) and len(candidate_items) > len(items):
            items = [item for item in candidate_items if isinstance(item, dict)]
    return items


def title_risks(title: str) -> list[str]:
    return [name for name, pattern in RISK_PATTERNS if pattern.search(title)]


def image_urls(item: dict[str, Any]) -> list[str]:
    values = item.get("imageUrls")
    candidates = values if isinstance(values, list) else []
    candidates = [item.get("mainImageUrl"), *candidates]
    seen: set[str] = set()
    urls: list[str] = []
    for value in candidates:
        url = urljoin("https://auctionet.com", str(value or "").strip())
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def fetch_ecb_rates(session: requests.Session) -> tuple[str, dict[str, Decimal]]:
    response = session.get(ECB_RATES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    rates = {"EUR": Decimal("1")}
    snapshot_date = ""
    for node in root.iter():
        currency = node.attrib.get("currency")
        rate = node.attrib.get("rate")
        time = node.attrib.get("time")
        if time and not currency:
            snapshot_date = time
        if currency and rate:
            try:
                rates[currency.upper()] = Decimal(rate)
            except InvalidOperation:
                continue
    if not snapshot_date or len(rates) == 1:
        raise RuntimeError("ECB exchange-rate snapshot was incomplete")
    return snapshot_date, rates


def price_in_eur(
    price: Decimal | None, currency: str, rates: dict[str, Decimal]
) -> tuple[Decimal | None, Decimal | None]:
    if price is None or not currency or currency not in rates:
        return None, None
    rate = rates[currency]
    if rate <= 0:
        return None, None
    return price / rate, rate


def listing_from_item(
    item: dict[str, Any], *, collected_at: str, rates: dict[str, Decimal]
) -> dict[str, Any] | None:
    external_id = str(item.get("id") or item.get("auctionId") or "").strip()
    title = html.unescape(str(item.get("shortTitle") or "")).strip()
    url = urljoin("https://auctionet.com", str(item.get("url") or "").strip())
    if not external_id or not title or not url:
        return None
    price_raw = html.unescape(str(item.get("amountValue") or "")).strip()
    price_value, currency = parse_amount(price_raw)
    source_currency = str(item.get("currency") or "").upper().strip()
    if not currency and source_currency:
        currency = source_currency
    price_eur, fx_rate = price_in_eur(price_value, currency, rates)
    risks = title_risks(title)
    return {
        "listing_id": f"{SOURCE}:{external_id}",
        "source": SOURCE,
        "external_id": external_id,
        "title": title,
        "url": url,
        "image_urls": image_urls(item),
        "price_raw": price_raw,
        "price_value": format(price_value, "f") if price_value is not None else None,
        "currency": currency or None,
        "source_currency_label": source_currency or None,
        "price_eur": format(price_eur, ".2f") if price_eur is not None else None,
        "fx_rate": format(fx_rate, "f") if fx_rate is not None else None,
        "sale_mode": "auction",
        "availability": "active",
        "auction_end": str(item.get("auctionEndsAtTitle") or "").strip() or None,
        "auction_end_raw": str(item.get("auctionEndTime") or "").strip() or None,
        "estimate_raw": str(item.get("amountTitle") or "").strip() or None,
        "attribution_status": "risk" if risks else "title_claim",
        "risks": risks,
        "collected_at": collected_at,
    }


def collect_active_listings(
    session: requests.Session,
    *,
    query: str,
    max_pages: int,
    limit: int,
    collected_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if max_pages < 1 or limit < 1:
        raise ValueError("max-pages and limit must be positive")
    raw_items: list[dict[str, Any]] = []
    page_snapshots: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        response = session.get(
            SEARCH_URL,
            params={"q": query, "page": page},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        items = extract_items(response.text)
        page_snapshots.append(
            {
                "page": page,
                "url": SEARCH_URL,
                "item_count": len(items),
                "html_sha256": hashlib.sha256(response.content).hexdigest(),
            }
        )
        raw_items.extend(items)
        if len(items) < PAGE_SIZE or len(raw_items) >= limit:
            break

    currencies: set[str] = set()
    for item in raw_items[:limit]:
        _, currency = parse_amount(item.get("amountValue"))
        if not currency:
            currency = str(item.get("currency") or "").upper().strip()
        if currency and currency != "EUR":
            currencies.add(currency)
    rates = {"EUR": Decimal("1")}
    if currencies:
        _, rates = fetch_ecb_rates(session)

    listings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        listing = listing_from_item(item, collected_at=collected_at, rates=rates)
        if listing is None or listing["listing_id"] in seen_ids:
            continue
        seen_ids.add(str(listing["listing_id"]))
        listings.append(listing)
        if len(listings) >= limit:
            break
    return listings, page_snapshots


def assert_permitted(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise RuntimeError(f"Frozen batch contains forbidden source marker(s): {', '.join(found)}")


def build_payload(
    *, run_id: str, query: str, collected_at: str, listings: list[dict[str, Any]], pages: list[dict[str, Any]]
) -> dict[str, Any]:
    currency_counts = Counter(str(listing.get("currency") or "unknown") for listing in listings)
    risk_counts = Counter(risk for listing in listings for risk in listing["risks"])
    return {
        "run": {
            "run_id": run_id,
            "collected_at": collected_at,
            "sources": [SOURCE],
            "query": query,
            "forbidden_sources_checked": True,
            "pages": pages,
            "currency_counts": dict(sorted(currency_counts.items())),
            "risk_counts": dict(sorted(risk_counts.items())),
        },
        "listings": listings,
    }


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing deal batch: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    collected_at = utc_now()
    session = requests.Session()
    listings, pages = collect_active_listings(
        session,
        query=args.query,
        max_pages=args.max_pages,
        limit=args.limit,
        collected_at=collected_at,
    )
    payload = build_payload(
        run_id=run_id,
        query=args.query,
        collected_at=collected_at,
        listings=listings,
        pages=pages,
    )
    assert_permitted(payload)
    write_payload(args.output, payload)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "listing_count": len(listings),
                "pages_fetched": len(pages),
                "currency_counts": payload["run"]["currency_counts"],
                "risk_counts": payload["run"]["risk_counts"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
