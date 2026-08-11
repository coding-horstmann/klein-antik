from __future__ import annotations

import html
import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup, Tag


USER_AGENT = (
    "klein-antik-market-research/1.0 "
    "(+https://klein-antik-dashboard-production.up.railway.app/)"
)
REQUEST_TIMEOUT_SECONDS = 35
EXTERNAL_REQUEST_TIMEOUT_SECONDS = 12
SOURCE_PAGE_SIZES = {
    "auctionet": 48,
    "blocket": 48,
    "dba": 48,
    "tori": 48,
    "quittenbaum": 15,
    "lempertz": 10,
    "bruun_rasmussen": 30,
    "van_ham": 20,
    "dorotheum": 200,
    "liveauctioneers": 24,
    "invaluable": 24,
    "christies": 12,
    "heritage": 24,
}
SOURCE_MAX_PAGES = {
    "auctionet": 200,
    "blocket": 1,
    "dba": 1,
    "tori": 1,
    "quittenbaum": 5,
    "lempertz": 2,
    "bruun_rasmussen": 1,
    "van_ham": 1,
    "dorotheum": 1,
    "liveauctioneers": 1,
    "invaluable": 1,
    "christies": 1,
    "heritage": 1,
}

SOURCE_LABELS = {
    "auctionet": "Auctionet",
    "blocket": "Blocket",
    "dba": "DBA",
    "tori": "Tori",
    "quittenbaum": "Quittenbaum",
    "lempertz": "Lempertz",
    "bruun_rasmussen": "Bruun Rasmussen",
    "mehlis": "Mehlis",
    "van_ham": "Van Ham",
    "dorotheum": "Dorotheum",
    "liveauctioneers": "LiveAuctioneers",
    "invaluable": "Invaluable",
    "christies": "Christie's",
    "heritage": "Heritage Auctions",
}

EXTERNAL_PILOT_SOURCES = (
    "liveauctioneers",
    "invaluable",
    "christies",
    "heritage",
)
EXTERNAL_PILOT_QUERY_IDS = (
    "meissen",
    "miriam-haskell",
    "wmf-vase",
    "orrefors-signed",
    "georg-jensen-silver",
    "paavo-tynell-lamp",
    "lisa-larson-figure",
)

MEISSEN_ARCHIVE_QUERY_ID = "meissen"
MEISSEN_ARCHIVE_SOURCE = "auctionet"
MEISSEN_ARCHIVE_START_PAGE = 6
MEISSEN_ARCHIVE_TARGET_PAGE = 200
MEISSEN_ARCHIVE_RESULT_LIMIT = (
    (MEISSEN_ARCHIVE_TARGET_PAGE - MEISSEN_ARCHIVE_START_PAGE + 1)
    * SOURCE_PAGE_SIZES[MEISSEN_ARCHIVE_SOURCE]
)
MEISSEN_PORCELAIN_PILOT_SOURCES = ("van_ham", "quittenbaum", "bruun_rasmussen")
MEISSEN_PORCELAIN_PILOT_PAGE_COUNTS = {
    "van_ham": 1,
    "quittenbaum": 1,
    "bruun_rasmussen": 1,
}
MEISSEN_DEAL_PILOT_SOURCES = ("blocket", "dba", "tori")
# The explicit Meissen pilot cannot find listings whose seller omitted the maker.
# Keep the broad discovery vocabulary small and source-language specific. Every
# record from this matrix remains unverified until a later image or mark review.
MEISSEN_BROAD_DISCOVERY_QUERIES = (
    ("meissen-broad-blocket-porslin", "blocket", "porslin"),
    ("meissen-broad-blocket-lokmonster", "blocket", "lokmonster"),
    ("meissen-broad-dba-porcelaen", "dba", "porcelaen"),
    ("meissen-broad-dba-logmonster", "dba", "logmonster"),
    ("meissen-broad-tori-posliini", "tori", "posliini"),
    ("meissen-broad-tori-sipulikoriste", "tori", "sipulikoriste"),
)
MEISSEN_BROAD_DISCOVERY_QUERY_IDS = tuple(
    query_id for query_id, _source, _query in MEISSEN_BROAD_DISCOVERY_QUERIES
)
MEISSEN_MARKETPLACE_DISCOVERY_QUERY_IDS = (
    MEISSEN_ARCHIVE_QUERY_ID,
    *MEISSEN_BROAD_DISCOVERY_QUERY_IDS,
)
MEISSEN_MARKETPLACE_DISCOVERY_TASKS = (
    *((MEISSEN_ARCHIVE_QUERY_ID, source, "Meissen", "explicit_query") for source in MEISSEN_DEAL_PILOT_SOURCES),
    *(
        (query_id, source, query, "broad_porcelain_category")
        for query_id, source, query in MEISSEN_BROAD_DISCOVERY_QUERIES
    ),
)

PRIVATE_MARKETPLACE_CONFIG = {
    "blocket": {
        "base_url": "https://www.blocket.se",
        "currency": "SEK",
        "image_host": "images.blocketcdn.se",
    },
    "dba": {
        "base_url": "https://www.dba.dk",
        "currency": "DKK",
        "image_host": "images.dbastatic.dk",
    },
    "tori": {
        "base_url": "https://www.tori.fi",
        "currency": "EUR",
        "image_host": "img.tori.net",
    },
}
CONDITION_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("damage", re.compile(r"\b(?:damage[ds]?|damaged|defekt|skadad|vaurio\w*)\b", re.I)),
    ("repair", re.compile(r"\b(?:repair(?:ed)?|restored|limmad|korjattu)\b", re.I)),
    ("chip", re.compile(r"\b(?:chip(?:ped)?|nagel|flisa\w*)\b", re.I)),
    ("crack", re.compile(r"\b(?:crack(?:ed)?|spricka\w*|halkeama\w*)\b", re.I)),
)
CONDITION_NEGATIONS = re.compile(r"\b(?:no|without|none|ingen|inga|inte|ei|ikke)\s*$", re.I)
MEISSEN_PORCELAIN_BACKFILL_SOURCES = ("quittenbaum",)
MEISSEN_PORCELAIN_BACKFILL_BATCH_PAGES = 2
DOROTHEUM_MEISSEN_AUCTION_URL = "https://www.dorotheum.com/en/a/123070/"

CATEGORY_SOURCES = {
    "meissen_porcelain": ("lempertz", "auctionet"),
    "designer_jewelry": ("auctionet",),
    "art_nouveau_metalware": ("auctionet", "quittenbaum"),
    "design_glass": ("auctionet",),
    "ceramics": ("auctionet",),
    "silver_jewelry": ("auctionet",),
    "small_lamps": ("auctionet", "quittenbaum"),
    "metal_objects": ("auctionet", "quittenbaum"),
    "design_objects": ("auctionet",),
}


class MarketSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectedBatch:
    """One consecutive source-page batch and its pagination state."""

    results: list[dict[str, Any]]
    pages_fetched: int
    exhausted: bool


def sources_for_category(category: str) -> tuple[str, ...]:
    return CATEGORY_SOURCES.get(category, ("auctionet",))


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
            "Accept": "text/html,application/xhtml+xml",
        }
    )
    return session


def collect(
    source: str,
    query: str,
    *,
    limit: int = 30,
    max_pages: int = 1,
    page_interval: float = 0.0,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    return collect_batch(
        source,
        query,
        limit=limit,
        start_page=1,
        page_count=max_pages,
        page_interval=page_interval,
        session=session,
    ).results


def collect_batch(
    source: str,
    query: str,
    *,
    limit: int = 30,
    start_page: int = 1,
    page_count: int = 1,
    page_interval: float = 0.0,
    session: requests.Session | None = None,
) -> CollectedBatch:
    collectors: dict[str, Callable[..., list[dict[str, Any]]]] = {
        "auctionet": collect_auctionet,
        "blocket": collect_blocket,
        "dba": collect_dba,
        "tori": collect_tori,
        "quittenbaum": collect_quittenbaum,
        "lempertz": collect_lempertz,
        "bruun_rasmussen": collect_bruun_rasmussen,
        "van_ham": collect_van_ham,
        "dorotheum": collect_dorotheum,
        "liveauctioneers": collect_liveauctioneers,
        "invaluable": collect_invaluable,
        "christies": collect_christies,
        "heritage": collect_heritage,
    }
    if source not in collectors:
        raise MarketSourceError(f"Unbekannte Marktquelle: {source}")
    own_session = session is None
    active_session = session or build_session()
    try:
        if start_page < 1:
            raise MarketSourceError("Die erste Archivseite muss mindestens 1 sein.")
        page_size = SOURCE_PAGE_SIZES[source]
        source_max_page = SOURCE_MAX_PAGES[source]
        if start_page > source_max_page:
            return CollectedBatch(results=[], pages_fetched=0, exhausted=True)
        page_cap = min(
            start_page + max(1, page_count) - 1,
            source_max_page,
        )
        raw_results: list[dict[str, Any]] = []
        search_query = search_query_for(query, source=source)
        pages_fetched = 0
        exhausted = False
        for page in range(start_page, page_cap + 1):
            page_results = collectors[source](
                active_session,
                search_query,
                limit=page_size,
                page=page,
            )
            pages_fetched += 1
            raw_results.extend(page_results)
            if len(page_results) < page_size or page >= source_max_page:
                exhausted = True
                break
            if len(raw_results) >= limit:
                break
            if page < page_cap and page_interval > 0:
                time.sleep(page_interval)

        results: list[dict[str, Any]] = []
        source_item_ids: set[str] = set()
        for result in raw_results:
            source_item_id = result["source_item_id"]
            if source_item_id in source_item_ids:
                continue
            source_item_ids.add(source_item_id)
            if relevant_to_query(query, result):
                results.append(result)
        return CollectedBatch(
            results=results[:limit],
            pages_fetched=pages_fetched,
            exhausted=exhausted,
        )
    finally:
        if own_session:
            active_session.close()


def _get(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    attempts: int = 3,
) -> requests.Response:
    for attempt in range(attempts):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            if not retryable or attempt == attempts - 1:
                raise MarketSourceError(f"{url}: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise MarketSourceError(f"{url}: Anfrage fehlgeschlagen")


def _get_external(
    session: requests.Session,
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    return _get(
        session,
        url,
        params=params,
        headers=_external_request_headers(source),
        timeout=EXTERNAL_REQUEST_TIMEOUT_SECONDS,
        attempts=1,
    )


def _external_request_headers(source: str) -> dict[str, str]:
    prefix = f"MARKET_{source.upper()}"
    headers: dict[str, str] = {}
    cookie = os.environ.get(f"{prefix}_COOKIE", "").strip()
    authorization = os.environ.get(f"{prefix}_AUTHORIZATION", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    return headers


def collect_auctionet(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get(
        session,
        "https://auctionet.com/en/search",
        params={
            "is": "ended",
            "order": "sold_recent",
            "page": page,
            "q": query,
        },
    )
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, Any]] = []
    for node in soup.select("[data-react-props]"):
        raw_props = node.get("data-react-props")
        if not isinstance(raw_props, str):
            continue
        try:
            props = json.loads(raw_props)
        except json.JSONDecodeError:
            continue
        candidate_items = props.get("items") if isinstance(props, dict) else None
        if isinstance(candidate_items, list) and len(candidate_items) > len(items):
            items = [item for item in candidate_items if isinstance(item, dict)]

    results: list[dict[str, Any]] = []
    for item in items[:limit]:
        amount_raw = _clean_text(str(item.get("amountValue") or ""))
        sold = bool(item.get("hasMetReserve")) and amount_raw not in {"", "-"}
        title = html.unescape(_clean_text(str(item.get("shortTitle") or "")))
        price, currency = parse_money(amount_raw)
        results.append(
            _result(
                source="auctionet",
                source_item_id=str(item.get("id") or item.get("auctionId") or ""),
                title=title,
                url=urljoin("https://auctionet.com", str(item.get("url") or "")),
                image_url=str(item.get("mainImageUrl") or ""),
                price_status="sold" if sold else "unsold",
                price_value=price if sold else None,
                price_raw=amount_raw if sold else "",
                currency=currency if sold else "",
                price_basis="hammer" if sold else "unknown",
                estimate_raw=_clean_text(str(item.get("amountTitle") or "")),
                sale_date=_clean_text(str(item.get("auctionEndTime") or "")),
                attribution=_attribution(title),
                raw_result=item,
            )
        )
    return [result for result in results if result["source_item_id"] and result["title"]]


def collect_blocket(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    return collect_private_marketplace(
        session, query, limit=limit, page=page, source="blocket"
    )


def collect_dba(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    return collect_private_marketplace(
        session, query, limit=limit, page=page, source="dba"
    )


def collect_tori(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    return collect_private_marketplace(
        session, query, limit=limit, page=page, source="tori"
    )


def collect_private_marketplace(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
    source: str,
) -> list[dict[str, Any]]:
    """Collect one active fixed-price marketplace page for the source gate."""
    config = PRIVATE_MARKETPLACE_CONFIG[source]
    base_url = str(config["base_url"])
    currency = str(config["currency"])
    image_host = str(config["image_host"])
    response = _get(
        session,
        f"{base_url}/recommerce/forsale/search",
        params={"q": query, "page": page},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []
    for card in soup.select("article.sf-search-ad"):
        link = card.select_one('a[href*="/recommerce/forsale/item/"]')
        title_node = card.select_one("h2")
        image_node = card.select_one(f'img[src*="{image_host}"]')
        if not isinstance(link, Tag) or title_node is None or image_node is None:
            continue
        item_url = urljoin(base_url, str(link.get("href") or ""))
        id_match = re.search(r"/recommerce/forsale/item/(\d+)", item_url)
        title = _clean_text(title_node.get_text(" ", strip=True))
        image_url = _clean_text(str(image_node.get("src") or ""))
        card_text = _clean_text(card.get_text(" ", strip=True))
        price_pattern = (
            r"\b([\d.,\s\u00a0]+)\s*(?:\u20ac|EUR)"
            if currency == "EUR"
            else r"\b([\d.\s\u00a0]+)\s*kr\b"
        )
        price_match = re.search(price_pattern, card_text, re.IGNORECASE)
        price_raw = _clean_text(price_match.group(0)) if price_match else ""
        price, _ = parse_money(f"{currency} {price_raw}") if price_raw else (None, "")
        if not id_match or not title or not image_url:
            continue
        results.append(
            _result(
                source=source,
                source_item_id=id_match.group(1),
                title=title,
                url=item_url,
                image_url=image_url,
                price_status="ask",
                price_value=price,
                price_raw=price_raw,
                currency=currency if price is not None else "",
                price_basis="reserve",
                estimate_raw="",
                attribution=_attribution(title),
                raw_result={
                    "availability": "active",
                    "sale_mode": "fixed_price",
                    "search_query": query,
                    "search_page": page,
                    "card_text": card_text,
                },
            )
        )
        if len(results) >= limit:
            break
    return results


def enrich_private_marketplace_listing(
    session: requests.Session,
    *,
    source: str,
    url: str,
    title: str,
) -> dict[str, Any]:
    """Fetch bounded detail evidence for an already frozen marketplace listing."""
    config = PRIVATE_MARKETPLACE_CONFIG[source]
    image_host = str(config["image_host"])
    response = _get(session, url, attempts=1)
    soup = BeautifulSoup(response.text, "html.parser")
    description_node = soup.select_one('meta[name="description"][content]')
    description = _clean_text(str(description_node.get("content") or "")) if description_node else ""
    image_urls: list[str] = []
    for node in soup.select("img[src], img[data-src]"):
        image_url = str(node.get("data-src") or node.get("src") or "")
        if image_host not in image_url or image_url in image_urls:
            continue
        image_urls.append(image_url)
        if len(image_urls) == 8:
            break
    evidence_text = " ".join((title, description))
    condition_risks: list[str] = []
    for risk, pattern in CONDITION_RISK_PATTERNS:
        for match in pattern.finditer(evidence_text):
            prefix = evidence_text[max(0, match.start() - 24) : match.start()]
            if CONDITION_NEGATIONS.search(prefix):
                continue
            condition_risks.append(risk)
            break
    return {
        "source": source,
        "url": url,
        "title": title,
        "description": description,
        "image_urls": image_urls,
        "condition_risks": sorted(set(condition_risks)),
        "html_sha256": hashlib.sha256(response.content).hexdigest(),
    }


def collect_van_ham(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    # Van Ham publishes completed lots in its own searchable auction archive.
    # The item page is the authoritative place for the realised price and image.
    encoded_query = quote(query, safe="")
    response = _get(
        session,
        (
            "https://auction.van-ham.com/en/--search-1-block-328-"
            f"ff_tags-{encoded_query}-order_by_sort-1-search_closed-browse.html"
        ),
        params={"page": page} if page > 1 else None,
    )
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for link in soup.select('a[href*="-item.html"]'):
        if not isinstance(link, Tag):
            continue
        item_url = urljoin("https://auction.van-ham.com", str(link.get("href") or ""))
        if not item_url or item_url in seen_urls:
            continue
        seen_urls.add(item_url)
        card = link.find_parent(["article", "li", "div"])
        image_node = card.select_one("img[src]") if isinstance(card, Tag) else None
        fallback_title = _clean_text(link.get_text(" ", strip=True))
        if not fallback_title and image_node:
            fallback_title = _clean_text(str(image_node.get("alt") or ""))
        candidates.append(
            (
                item_url,
                fallback_title,
                urljoin(
                    "https://auction.van-ham.com",
                    str(image_node.get("src") or "") if image_node else "",
                ),
            )
        )

    results: list[dict[str, Any]] = []
    for index, (item_url, fallback_title, fallback_image) in enumerate(candidates[:limit]):
        detail = _get(session, item_url)
        detail_soup = BeautifulSoup(detail.text, "html.parser")
        title_node = detail_soup.select_one("h1")
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
        title = re.sub(r"^Lot\s+[^|]+\|\s*", "", title, flags=re.IGNORECASE)
        title = title or fallback_title
        detail_text = _clean_text(detail_soup.get_text(" ", strip=True))
        lower_text = detail_text.lower()
        price_match = re.search(
            r"(?:result|ergebnis)\s*:\s*"
            r"(?:\((?:incl\. premium|inkl\. aufgeld)\)\s*)?"
            r"((?:EUR\s*)?[\d.,\s]+(?:€|EUR)?)",
            detail_text,
            flags=re.IGNORECASE,
        )
        price_raw = _clean_text(price_match.group(1)) if price_match else ""
        price, currency = parse_money(price_raw)
        estimate_match = re.search(
            r"(?:estimate|taxe)\s*:?\s*(.*?)\s*(?:result|ergebnis)\s*:",
            detail_text,
            flags=re.IGNORECASE,
        )
        estimate_raw = _clean_text(estimate_match.group(1)) if estimate_match else ""
        image_node = detail_soup.select_one('meta[property="og:image"][content]')
        image_url = (
            str(image_node.get("content") or "") if image_node else fallback_image
        )
        sale_date_match = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", detail_text)
        sale_date = _clean_text(sale_date_match.group(0)) if sale_date_match else ""
        sold = "lot was sold" in lower_text or "los ist verkauft" in lower_text
        results.append(
            _result(
                source="van_ham",
                source_item_id=_id_from_url(item_url),
                title=title,
                url=item_url,
                image_url=image_url,
                price_status="sold" if sold and price is not None else "unknown",
                price_value=price if sold else None,
                price_raw=price_raw if sold else "",
                currency=currency if sold else "",
                price_basis=(
                    "premium_included"
                    if "incl. premium" in lower_text or "inkl. aufgeld" in lower_text
                    else "realised"
                )
                if sold and price is not None
                else "unknown",
                estimate_raw=estimate_raw,
                sale_date=sale_date,
                attribution=_attribution(title),
                raw_result={"result": price_raw, "estimate": estimate_raw},
            )
        )
        if index + 1 < min(len(candidates), limit):
            time.sleep(0.5)
    return [result for result in results if result["source_item_id"] and result["title"]]


def collect_dorotheum(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    # This curated completed Dorotheum auction is dedicated to Meissen porcelain.
    # It is deliberately a one-page pilot so its result quality can be reviewed first.
    response = _get(session, DOROTHEUM_MEISSEN_AUCTION_URL)
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for link in soup.select('a[href*="/en/l/"]'):
        if not isinstance(link, Tag):
            continue
        item_url = urljoin("https://www.dorotheum.com", str(link.get("href") or ""))
        if not item_url or item_url in seen_urls:
            continue
        seen_urls.add(item_url)
        card = link.find_parent(["article", "li", "div"])
        card_text = _clean_text(card.get_text(" ", strip=True)) if isinstance(card, Tag) else ""
        title_node = (
            card.select_one("h1, h2, h3, h4, .lot-title, .item-title")
            if isinstance(card, Tag)
            else None
        )
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
        title = title or _clean_text(link.get_text(" ", strip=True))
        price_match = re.search(
            r"Realized\s+price\s*:\s*\**\s*((?:EUR\s*)?[\d.,\s]+(?:€|EUR)?)",
            card_text,
            flags=re.IGNORECASE,
        )
        price_raw = _clean_text(price_match.group(1)) if price_match else ""
        price, currency = parse_money(price_raw)
        image_node = card.select_one("img[src]") if isinstance(card, Tag) else None
        results.append(
            _result(
                source="dorotheum",
                source_item_id=_id_from_url(item_url),
                title=title,
                url=item_url,
                image_url=urljoin(
                    "https://www.dorotheum.com",
                    str(image_node.get("src") or "") if image_node else "",
                ),
                price_status="sold" if price is not None else "unknown",
                price_value=price,
                price_raw=price_raw,
                currency=currency,
                price_basis="premium_included" if price is not None else "unknown",
                estimate_raw="",
                attribution=_attribution(title),
                raw_result={"auction_url": DOROTHEUM_MEISSEN_AUCTION_URL},
            )
        )
        if len(results) >= limit:
            break
    return [result for result in results if result["source_item_id"] and result["title"]]


def collect_quittenbaum(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    path = "https://www.quittenbaum.de/en/search/"
    if page > 1:
        path = f"https://www.quittenbaum.de/en/search/page/{page}/"
    response = _get(
        session,
        path,
        params={"q": query},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []
    for card in soup.select("li.auction-object")[:limit]:
        if not isinstance(card, Tag):
            continue
        title_node = card.select_one("h2.auction-object-title")
        link_node = card.select_one("a[href]")
        price_node = card.select_one(".auction-object-price")
        if not title_node or not link_node:
            continue
        title = _clean_text(title_node.get_text(" ", strip=True))
        manufacturer_node = card.select_one(".manufacture")
        manufacturer = (
            _clean_text(manufacturer_node.get_text(" ", strip=True))
            if manufacturer_node
            else ""
        )
        price_text = _clean_text(price_node.get_text(" ", strip=True)) if price_node else ""
        lower_price = price_text.lower()
        if "hammer price" in lower_price:
            price_status = "sold"
            price_basis = "hammer"
        elif "reserve price" in lower_price or "buy" in lower_price:
            price_status = "ask"
            price_basis = "reserve"
        elif "starting price" in lower_price or "current bid" in lower_price:
            price_status = "current_bid"
            price_basis = "current_bid"
        elif "estimate" in lower_price:
            price_status = "estimate"
            price_basis = "estimate"
        else:
            price_status = "unknown"
            price_basis = "unknown"
        price, currency = parse_money(price_text)
        image_node = card.select_one("img[src]")
        item_url = urljoin(
            "https://www.quittenbaum.de",
            str(link_node.get("href") or ""),
        )
        results.append(
            _result(
                source="quittenbaum",
                source_item_id=str(card.get("id") or _id_from_url(item_url)),
                title=title,
                url=item_url,
                image_url=urljoin(
                    "https://www.quittenbaum.de",
                    str(image_node.get("src") or "") if image_node else "",
                ),
                price_status=price_status,
                price_value=price,
                price_raw=price_text,
                currency=currency,
                price_basis=price_basis,
                estimate_raw="",
                attribution=_attribution(f"{manufacturer} {title}"),
                raw_result={"manufacturer": manufacturer, "price": price_text},
            )
        )
    return results


def collect_bruun_rasmussen(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get(
        session,
        "https://bruun-rasmussen.dk/m/lots",
        params={"locale": "en", "q": query, "status": "sold"},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []
    for card in soup.select('li[id^="lot_"]')[:limit]:
        if not isinstance(card, Tag):
            continue
        link_node = card.select_one("a.lot-list-item[href]")
        title_node = card.select_one("p.description")
        if not link_node or not title_node:
            continue
        price_node = next(
            (
                node
                for node in card.select("p")
                if "price realised" in node.get_text(" ", strip=True).lower()
            ),
            None,
        )
        amount_node = price_node.select_one("currency-amount") if price_node else None
        price = None
        currency = ""
        price_raw = ""
        if amount_node:
            price_raw = _clean_text(
                f"{amount_node.get('amount', '')} {amount_node.get('currency', '')}"
            )
            price, currency = parse_money(price_raw)
        estimate_node = next(
            (
                node
                for node in card.select("p")
                if node.get_text(" ", strip=True).lower().startswith("estimate")
            ),
            None,
        )
        image_node = card.select_one("img[src]")
        item_url = urljoin(
            "https://bruun-rasmussen.dk",
            str(link_node.get("href") or ""),
        )
        title = _clean_text(title_node.get_text(" ", strip=True))
        results.append(
            _result(
                source="bruun_rasmussen",
                source_item_id=str(card.get("id") or "").removeprefix("lot_"),
                title=title,
                url=item_url,
                image_url=urljoin(
                    "https://bruun-rasmussen.dk",
                    str(image_node.get("src") or "") if image_node else "",
                ),
                price_status="sold" if price is not None else "unknown",
                price_value=price,
                price_raw=price_raw,
                currency=currency,
                price_basis="realised" if price is not None else "unknown",
                estimate_raw=(
                    _clean_text(estimate_node.get_text(" ", strip=True))
                    if estimate_node
                    else ""
                ),
                attribution=_attribution(title),
                raw_result={"price": price_raw},
            )
        )
    return results


def collect_lempertz(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"id": "113", "tx_kesearch_pi1[sword]": query}
    if page > 1:
        params["tx_kesearch_pi1[page]"] = page
    response = _get(
        session,
        "https://www.lempertz.com/en/search.html",
        params=params,
    )
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []
    cards = soup.select(".result-list-item-type-lempertz_lot")[: min(limit, 15)]
    for index, card in enumerate(cards):
        if not isinstance(card, Tag):
            continue
        link_node = card.select_one(".result-title a[href]")
        if not link_node:
            continue
        item_url = urljoin(
            "https://www.lempertz.com",
            str(link_node.get("href") or ""),
        )
        title = _clean_text(link_node.get_text(" ", strip=True))
        title = re.sub(r"^Lot\s+\d+[A-Z]?\s*", "", title, flags=re.IGNORECASE)
        image_node = card.select_one(".result-preview-img img[src]")
        card_text = _clean_text(card.get_text(" ", strip=True))
        estimate_match = re.search(r"Estimate:\s*(.+)$", card_text, re.IGNORECASE)
        estimate_raw = estimate_match.group(1) if estimate_match else ""

        detail = _get(session, item_url)
        detail_soup = BeautifulSoup(detail.text, "html.parser")
        result_node: Tag | None = None
        for wrapper in detail_soup.select(".lot-price-wrapper, .lot-price-wraper"):
            label = wrapper.select_one(".lot-price-label")
            if label and "result" in label.get_text(" ", strip=True).lower():
                result_node = wrapper.select_one(".lot-price")
                break
        result_text = (
            _clean_text(result_node.get_text(" ", strip=True)) if result_node else ""
        )
        price, currency = parse_money(result_text)
        basis = "premium_included" if "incl. premium" in result_text.lower() else "realised"
        results.append(
            _result(
                source="lempertz",
                source_item_id=_id_from_url(item_url),
                title=title,
                url=item_url,
                image_url=(
                    urljoin(
                        "https://www.lempertz.com",
                        str(image_node.get("src") or ""),
                    )
                    if image_node
                    else ""
                ),
                price_status="sold" if price is not None else "estimate",
                price_value=price,
                price_raw=result_text,
                currency=currency,
                price_basis=basis if price is not None else "estimate",
                estimate_raw=estimate_raw,
                attribution=_attribution(title),
                raw_result={"estimate": estimate_raw, "result": result_text},
            )
        )
        if index + 1 < len(cards):
            time.sleep(0.6)
    return results


def collect_liveauctioneers(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get_external(
        session,
        "liveauctioneers",
        f"https://www.liveauctioneers.com/search/{_slug(query)}",
        params={"page": page},
    )
    return _collect_link_cards(
        response.text,
        source="liveauctioneers",
        base_url="https://www.liveauctioneers.com",
        link_selector='a[href*="/item/"], a[href*="/price-result/"]',
        limit=limit,
    )


def collect_invaluable(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get_external(
        session,
        "invaluable",
        "https://www.invaluable.com/search",
        params={"keyword": query, "page": page},
    )
    return _collect_link_cards(
        response.text,
        source="invaluable",
        base_url="https://www.invaluable.com",
        link_selector='a[href*="/auction-lot/"]',
        limit=limit,
    )


def collect_christies(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get_external(
        session,
        "christies",
        "https://www.christies.com/en/results",
        params={"keyword": query, "page": page},
    )
    return _collect_link_cards(
        response.text,
        source="christies",
        base_url="https://www.christies.com",
        link_selector='a[href*="/lot/lot-"]',
        limit=limit,
    )


def collect_heritage(
    session: requests.Session,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[dict[str, Any]]:
    response = _get_external(
        session,
        "heritage",
        "https://www.ha.com/c/search-results.zx",
        params={"Ntt": query, "page": page},
    )
    return _collect_link_cards(
        response.text,
        source="heritage",
        base_url="https://www.ha.com",
        link_selector='a[href*="/itm/"]',
        limit=limit,
    )


def _collect_link_cards(
    document: str,
    *,
    source: str,
    base_url: str,
    link_selector: str,
    limit: int,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(document, "html.parser")
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for link in soup.select(link_selector):
        if not isinstance(link, Tag):
            continue
        item_url = urljoin(base_url, str(link.get("href") or ""))
        if not item_url or item_url in seen_urls:
            continue
        seen_urls.add(item_url)
        card = _listing_card(link)
        title = _listing_title(link, card)
        if not title:
            continue
        card_text = _clean_text(card.get_text(" ", strip=True))
        price_status, price_basis, price_raw, price, currency = _price_details(card_text)
        results.append(
            _result(
                source=source,
                source_item_id=_id_from_url(item_url),
                title=title,
                url=item_url,
                image_url=_listing_image(card, base_url),
                price_status=price_status,
                price_value=price,
                price_raw=price_raw,
                currency=currency,
                price_basis=price_basis,
                estimate_raw=price_raw if price_status == "estimate" else "",
                sale_date="",
                attribution=_attribution(title),
                raw_result={"price_text": price_raw},
            )
        )
        if len(results) >= limit:
            break
    return results


def _listing_card(link: Tag) -> Tag:
    for parent in link.parents:
        if not isinstance(parent, Tag):
            continue
        class_names = " ".join(parent.get("class") or []).lower()
        if parent.name in {"article", "li"} or any(
            marker in class_names
            for marker in ("card", "item", "lot", "result", "listing", "product")
        ):
            return parent
    return link


def _listing_title(link: Tag, card: Tag) -> str:
    for value in (
        link.get("aria-label"),
        link.get("title"),
        link.get_text(" ", strip=True),
    ):
        title = _clean_text(str(value or ""))
        if len(title) >= 4 and title.lower() not in {"view lot", "view item", "details"}:
            return title
    heading = card.select_one("h1, h2, h3, h4, [data-testid*='title']")
    return _clean_text(heading.get_text(" ", strip=True)) if heading else ""


def _listing_image(card: Tag, base_url: str) -> str:
    image = card.select_one("img[src], img[data-src], img[data-original]")
    if not image:
        return ""
    raw_url = str(
        image.get("data-src") or image.get("data-original") or image.get("src") or ""
    )
    return urljoin(base_url, raw_url)


def _price_details(
    text: str,
) -> tuple[str, str, str, Decimal | None, str]:
    labels = (
        (
            "sold",
            "realised",
            ("price realized", "price realised", "hammer price", "sold for", "sold:"),
        ),
        ("estimate", "estimate", ("estimate", "est:")),
        ("current_bid", "current_bid", ("current bid", "starting bid", "bid:")),
        ("ask", "reserve", ("reserve price", "buy now")),
    )
    lowered = text.lower()
    for status, basis, markers in labels:
        for marker in markers:
            offset = lowered.find(marker)
            if offset < 0:
                continue
            fragment = _clean_text(text[offset : offset + 120])
            price, currency = parse_money(fragment)
            if price is not None:
                return status, basis, fragment, price, currency
    return "unknown", "unknown", "", None, ""


def _slug(value: str) -> str:
    normalized = _normalize(value)
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def parse_money(value: str) -> tuple[Decimal | None, str]:
    text = _clean_text(value)
    currency = ""
    currency_markers = (
        ("EUR", "EUR"),
        ("€", "EUR"),
        ("DKK", "DKK"),
        ("SEK", "SEK"),
        ("USD", "USD"),
        ("$", "USD"),
        ("GBP", "GBP"),
        ("£", "GBP"),
        ("NOK", "NOK"),
        ("CHF", "CHF"),
    )
    upper = text.upper()
    for marker, code in currency_markers:
        if marker.upper() in upper:
            currency = code
            break
    match = re.search(r"\d[\d\s.,']*", text)
    if not match:
        return None, currency
    raw_number = match.group(0).replace(" ", "").replace("'", "")
    decimal_separator = ""
    if "," in raw_number and "." in raw_number:
        decimal_separator = "," if raw_number.rfind(",") > raw_number.rfind(".") else "."
    elif "," in raw_number:
        if len(raw_number.rsplit(",", 1)[1]) == 2:
            decimal_separator = ","
    elif "." in raw_number:
        if len(raw_number.rsplit(".", 1)[1]) == 2:
            decimal_separator = "."

    if decimal_separator:
        thousands_separator = "." if decimal_separator == "," else ","
        normalized = raw_number.replace(thousands_separator, "")
        normalized = normalized.replace(decimal_separator, ".")
    else:
        normalized = raw_number.replace(",", "").replace(".", "")
    try:
        return Decimal(normalized), currency
    except InvalidOperation:
        return None, currency


GENERIC_QUERY_WORDS = {
    "aeltere",
    "art",
    "bronze",
    "deco",
    "designglas",
    "designobjekt",
    "emaille",
    "figur",
    "figure",
    "finnische",
    "glas",
    "glass",
    "glasvogel",
    "jardiniere",
    "jewelry",
    "jewellery",
    "jugendstil",
    "kanne",
    "karaffe",
    "keramik",
    "lampe",
    "lamp",
    "leuchte",
    "metall",
    "metallobjekt",
    "metallwaren",
    "nouveau",
    "numbered",
    "object",
    "of",
    "original",
    "pewter",
    "schmuck",
    "signed",
    "signierter",
    "silber",
    "silver",
    "silverplate",
    "tafelaufsatz",
    "tablett",
    "vase",
    "versilbert",
    "vintage",
    "zinn",
}

JEWELRY_WORDS = {
    "bracelet",
    "brooch",
    "brosche",
    "collier",
    "earring",
    "jewellery",
    "jewelry",
    "kette",
    "necklace",
    "pendant",
    "ring",
    "schmuck",
}

SIGNED_DESIGNER_JEWELRY_MARKERS = {
    "bengel",
    "boucher",
    "coro",
    "eisenberg",
    "fahrner",
    "haskell",
    "hobe",
    "instone",
    "jensen",
    "joseff",
    "lapponia",
    "murrle",
    "napier",
    "pineda",
    "schreiner",
    "sphinx",
    "trifari",
    "weckstrom",
}
SIGNATURE_WORDS = {"mark", "marked", "signature", "signed", "signiert"}

METALWARE_STYLE_WORDS = {
    "art deco",
    "art nouveau",
    "jugendstil",
    "secession",
}

METALWARE_WORDS = {
    "bowl",
    "brass",
    "bronze",
    "candlestick",
    "candelabra",
    "carafe",
    "centerpiece",
    "jardiniere",
    "metal",
    "pewter",
    "plate",
    "silver",
    "tray",
    "vase",
}

SCANDINAVIAN_GLASS_WORDS = {
    "boda",
    "holmegaard",
    "iittala",
    "kosta",
    "nuutajarvi",
    "orrefors",
    "riihimaki",
}

FINNISH_LIGHT_WORDS = {
    "finland",
    "finnish",
    "idman",
    "orno",
    "paavo tynell",
    "taito",
}

SEARCH_QUERY_OVERRIDES = {
    "signierter vintage designerschmuck": "signed jewelry",
    "jugendstil art deco metallwaren": "Art Nouveau metalware",
    "skandinavisches designglas": "Scandinavian glass",
    "aeltere finnische leuchte": "Finnish lamp",
    "n e from silber": "N.E. From",
    "marcel boucher schmuck": "Marcel Boucher",
    "osiris jugendstil zinn": "Osiris pewter",
    "bing grondahl figur": "Bing Grondahl",
    "hagenauer wien figur": "Hagenauer",
    "gunnar cyren designobjekt": "Gunnar Cyren",
}

RELEVANCE_ANCHOR_OVERRIDES = {
    "crown trifari": ("trifari",),
    "schreiner new york schmuck": ("schreiner",),
    "hagenauer wien figur": ("hagenauer",),
}

SEARCH_DROP_WORDS = {"numbered", "original", "signed", "signierter", "vintage"}
SEARCH_TRANSLATIONS = {
    "designglas": ("design", "glass"),
    "designobjekt": ("design", "object"),
    "emaille": ("enamel",),
    "figur": ("figure",),
    "glas": ("glass",),
    "glasvogel": ("glass", "bird"),
    "jardiniere": ("jardiniere",),
    "jugendstil": ("art", "nouveau"),
    "kanne": ("jug",),
    "karaffe": ("carafe",),
    "keramik": ("ceramics",),
    "lampe": ("lamp",),
    "leuchte": ("lamp",),
    "metall": ("metal",),
    "metallobjekt": ("metal", "object"),
    "metallwaren": ("metalware",),
    "schmuck": ("jewelry",),
    "silber": ("silver",),
    "skandinavisches": ("scandinavian",),
    "tafelaufsatz": ("centerpiece",),
    "tablett": ("tray",),
    "versilbert": ("silverplate",),
    "zinn": ("pewter",),
}

OBJECT_WORDS = {
    "jardiniere": {"jardiniere", "planter"},
    "karaffe": {"carafe", "decanter", "karaffe"},
    "kanne": {"ewer", "jug", "kanne", "pitcher", "pot"},
    "tafelaufsatz": {"centerpiece", "centre-piece", "epergne", "tafelaufsatz"},
    "tablett": {"salver", "tablett", "tray"},
    "vase": {"vase"},
    "glasvogel": {"bird", "glasvogel"},
}

FIGURE_WORDS = {
    "animal",
    "bear",
    "bird",
    "boy",
    "cat",
    "child",
    "dog",
    "elephant",
    "figure",
    "figurine",
    "girl",
    "horse",
    "monkey",
    "sculpture",
    "statue",
    "statuette",
}
TABLEWARE_WORDS = {
    "bowl",
    "bowls",
    "cup",
    "cups",
    "dish",
    "dishes",
    "jug",
    "jugs",
    "pitcher",
    "pitchers",
    "plate",
    "plates",
    "service",
    "teapot",
    "teapots",
    "vase",
    "vases",
}
LIGHT_WORDS = {
    "ceiling",
    "chandelier",
    "floorlamp",
    "lamp",
    "light",
    "pendant",
    "sconce",
    "tablelamp",
    "walllight",
}


def search_query_for(query: str, *, source: str = "") -> str:
    normalized_query = _normalize(query)
    if normalized_query in SEARCH_QUERY_OVERRIDES:
        return SEARCH_QUERY_OVERRIDES[normalized_query]
    if normalized_query in RELEVANCE_ANCHOR_OVERRIDES:
        return " ".join(RELEVANCE_ANCHOR_OVERRIDES[normalized_query])

    search_tokens: list[str] = []
    for token in normalized_query.split():
        if len(token) == 1 or token in SEARCH_DROP_WORDS:
            continue
        translated = SEARCH_TRANSLATIONS.get(token, (token,))
        search_tokens.extend(translated)
    if source == "quittenbaum":
        search_tokens = [
            token for token in search_tokens if token not in {"art", "deco", "nouveau"}
        ]
    return " ".join(dict.fromkeys(search_tokens)) or query


def relevant_to_query(query: str, result: dict[str, Any]) -> bool:
    title = _normalize(str(result.get("title") or ""))
    manufacturer = _normalize(
        str((result.get("raw_result") or {}).get("manufacturer") or "")
    )
    haystack = f"{title} {manufacturer}".strip()
    normalized_query = _normalize(query)

    if normalized_query == "signierter vintage designerschmuck":
        words = set(haystack.split())
        has_jewelry = bool(words.intersection(JEWELRY_WORDS))
        has_signature = bool(words.intersection(SIGNATURE_WORDS))
        has_designer = bool(words.intersection(SIGNED_DESIGNER_JEWELRY_MARKERS))
        return has_jewelry and (has_signature or has_designer)
    if normalized_query == "jugendstil art deco metallwaren":
        return any(word in haystack for word in METALWARE_STYLE_WORDS) and any(
            word in haystack.split() for word in METALWARE_WORDS
        )
    if normalized_query == "skandinavisches designglas":
        has_glass_object = any(
            word in haystack.split() for word in {"glass", "vase", "decanter", "bowl"}
        )
        return has_glass_object and any(
            word in haystack.split() for word in SCANDINAVIAN_GLASS_WORDS
        )
    if normalized_query == "aeltere finnische leuchte":
        has_light = any(
            word in haystack.split() for word in {"lamp", "light", "pendant", "valaisin"}
        )
        return has_light and any(word in haystack for word in FINNISH_LIGHT_WORDS)

    anchors = list(RELEVANCE_ANCHOR_OVERRIDES.get(normalized_query, ())) or [
        token
        for token in normalized_query.split()
        if len(token) > 1 and token not in GENERIC_QUERY_WORDS
    ]
    if normalized_query.startswith("n e from "):
        maker_relevant = "n e from" in haystack or "ne from" in haystack
        return maker_relevant and _object_relevant(normalized_query, haystack)
    if not anchors:
        return False
    required = len(anchors) if len(anchors) <= 2 else 2
    maker_relevant = sum(anchor in haystack.split() for anchor in anchors) >= required
    return maker_relevant and _object_relevant(normalized_query, haystack)


def _object_relevant(query: str, haystack: str) -> bool:
    haystack_words = set(haystack.split())
    for marker, acceptable_words in OBJECT_WORDS.items():
        if marker in query.split() and not acceptable_words.intersection(haystack_words):
            return False
    if "figur" in query.split():
        if TABLEWARE_WORDS.intersection(haystack_words):
            return False
    if ({"lampe", "leuchte"}.intersection(query.split())) and not LIGHT_WORDS.intersection(
        haystack_words
    ):
        return False
    if "schmuck" in query.split() and not JEWELRY_WORDS.intersection(haystack_words):
        return False
    return True


def _result(
    *,
    source: str,
    source_item_id: str,
    title: str,
    url: str,
    image_url: str,
    price_status: str,
    price_value: Decimal | None,
    price_raw: str,
    currency: str,
    price_basis: str,
    estimate_raw: str,
    attribution: str,
    raw_result: dict[str, Any],
    sale_date: str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "source_item_id": source_item_id,
        "title": title,
        "url": url,
        "image_url": image_url,
        "price_status": price_status,
        "price_value": price_value,
        "price_raw": price_raw,
        "currency": currency,
        "price_basis": price_basis,
        "estimate_raw": estimate_raw,
        "sale_date": sale_date,
        "attribution": attribution,
        "raw_result": raw_result,
    }


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _normalize(value: str) -> str:
    translations = str.maketrans(
        {
            "ß": "ss",
            "ẞ": "SS",
            "ø": "o",
            "Ø": "O",
            "æ": "ae",
            "Æ": "AE",
            "œ": "oe",
            "Œ": "OE",
            "ł": "l",
            "Ł": "L",
        }
    )
    value = value.translate(translations)
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def _id_from_url(url: str) -> str:
    path = url.rstrip("/").rsplit("/", 1)[-1]
    return path or url


def _attribution(value: str) -> str:
    lowered = value.lower()
    uncertain_tokens = (
        "attributed",
        "zugeschrieben",
        "tillskriven",
        "style of",
        "in the style",
        "probably",
        "possibly",
        "wohl ",
        "?",
    )
    return "uncertain" if any(token in lowered for token in uncertain_tokens) else "stated"
