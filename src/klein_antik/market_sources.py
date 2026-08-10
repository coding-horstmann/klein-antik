from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


USER_AGENT = (
    "klein-antik-market-research/1.0 "
    "(+https://klein-antik-dashboard-production.up.railway.app/)"
)
REQUEST_TIMEOUT_SECONDS = 35
SOURCE_PAGE_SIZES = {
    "auctionet": 48,
    "quittenbaum": 15,
    "lempertz": 10,
    "bruun_rasmussen": 30,
}
SOURCE_MAX_PAGES = {
    "auctionet": 5,
    "quittenbaum": 5,
    "lempertz": 2,
    "bruun_rasmussen": 1,
}

SOURCE_LABELS = {
    "auctionet": "Auctionet",
    "quittenbaum": "Quittenbaum",
    "lempertz": "Lempertz",
    "bruun_rasmussen": "Bruun Rasmussen",
}

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
        "quittenbaum": collect_quittenbaum,
        "lempertz": collect_lempertz,
        "bruun_rasmussen": collect_bruun_rasmussen,
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
) -> requests.Response:
    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            if not retryable or attempt == 2:
                raise MarketSourceError(f"{url}: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise MarketSourceError(f"{url}: Anfrage fehlgeschlagen")


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
