from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag


FORBIDDEN_MARKERS = ("ebay", "serpapi")
SOURCE = "auctionet"
USER_AGENT = "KleinAntikMeissenScout/1.0 (shortlist detail enrichment)"
ELIGIBLE_STATUSES = {
    "title_match_requires_image_review",
    "reference_cue_requires_object_verification",
}
CONDITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("restored", re.compile(r"\b(?:restored|restoration)\b", re.I)),
    ("repaired", re.compile(r"\b(?:repaired|repair|glued)\b", re.I)),
    ("damage", re.compile(r"\b(?:damaged|damage|loss|missing)\b", re.I)),
    ("chip", re.compile(r"\b(?:chip|chipped)\b", re.I)),
    ("crack", re.compile(r"\b(?:crack|cracked)\b", re.I)),
)
MODEL_PATTERN = re.compile(
    r"\b(?:model|mod(?:el)?|form|no|nr)\.?\s*([a-z]{0,3}\d{1,5}[a-z]{0,3})\b",
    re.I,
)
HEIGHT_PATTERN = re.compile(r"\bheight\s*(\d{1,3}(?:[.,]\d+)?)\s*cm\b", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze Auctionet detail evidence for an existing Meissen shortlist."
    )
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--reference-pass", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--listing-id", action="append")
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root in {path} must be an object")
    return payload


def reject_forbidden(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise RuntimeError(f"Shortlist contains forbidden source marker(s): {', '.join(found)}")


def section_text(soup: BeautifulSoup, label: str) -> str:
    heading = soup.find(
        lambda node: isinstance(node, Tag)
        and node.name in {"h1", "h2", "h3"}
        and node.get_text(" ", strip=True).casefold() == label.casefold()
    )
    if not isinstance(heading, Tag):
        return ""
    chunks: list[str] = []
    for sibling in heading.find_next_siblings():
        if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3"}:
            break
        text = sibling.get_text(" ", strip=True)
        if text:
            chunks.append(text)
    return " ".join(chunks)


def condition_risks(value: str) -> list[str]:
    return [name for name, pattern in CONDITION_PATTERNS if pattern.search(value)]


def model_numbers(*values: str) -> list[str]:
    found: set[str] = set()
    for value in values:
        found.update(match.upper() for match in MODEL_PATTERN.findall(value))
    return sorted(found)


def height_cm(*values: str) -> float | None:
    for value in values:
        match = HEIGHT_PATTERN.search(value)
        if not match:
            continue
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            continue
    return None


def extract_detail(markup: str, listing: dict[str, Any], *, fetched_at: str) -> dict[str, Any]:
    soup = BeautifulSoup(markup, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if isinstance(heading, Tag) else str(listing.get("title") or "")
    description = section_text(soup, "Description")
    condition = section_text(soup, "Condition")
    combined = " ".join((title, description, condition))
    return {
        "listing_id": str(listing.get("listing_id") or ""),
        "source": SOURCE,
        "url": str(listing.get("url") or ""),
        "title": title,
        "description": description or None,
        "condition": condition or None,
        "condition_risks": condition_risks(condition),
        "model_numbers": model_numbers(combined),
        "height_cm": height_cm(description, combined),
        "html_sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(),
        "fetched_at": fetched_at,
        "manual_review_required": True,
    }


def shortlist_ids(reference_pass: dict[str, Any], explicit_ids: list[str] | None) -> list[str]:
    if explicit_ids:
        return list(dict.fromkeys(str(value) for value in explicit_ids if str(value)))
    records = reference_pass.get("records")
    if not isinstance(records, list):
        raise RuntimeError("Reference pass has no records list")
    return [
        str(record.get("listing_id") or "")
        for record in records
        if isinstance(record, dict) and str(record.get("status") or "") in ELIGIBLE_STATUSES
    ]


def enrich_shortlist(
    session: requests.Session,
    deals: dict[str, Any],
    reference_pass: dict[str, Any],
    *,
    selected_ids: list[str],
    limit: int,
    fetched_at: str,
) -> dict[str, Any]:
    reject_forbidden(deals)
    if limit < 1:
        raise ValueError("limit must be positive")
    listings = deals.get("listings")
    if not isinstance(listings, list):
        raise RuntimeError("Deal batch has no listings list")
    index = {
        str(listing.get("listing_id") or ""): listing
        for listing in listings
        if isinstance(listing, dict) and str(listing.get("listing_id") or "")
    }
    unknown = sorted(set(selected_ids) - set(index))
    if unknown:
        raise RuntimeError(f"Unknown shortlist listing IDs: {', '.join(unknown)}")
    details: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for listing_id in selected_ids[:limit]:
        listing = index[listing_id]
        if str(listing.get("source") or "").lower() != SOURCE:
            failures.append({"listing_id": listing_id, "reason": "source is not enabled"})
            continue
        url = str(listing.get("url") or "")
        if not url.startswith("https://auctionet.com/"):
            failures.append({"listing_id": listing_id, "reason": "invalid canonical URL"})
            continue
        try:
            response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            failures.append({"listing_id": listing_id, "reason": f"request failed: {type(exc).__name__}"})
            continue
        details.append(extract_detail(response.text, listing, fetched_at=fetched_at))
    return {
        "run": {
            "source": SOURCE,
            "fetched_at": fetched_at,
            "deal_batch_sha256": deals.get("_sha256"),
            "reference_pass_sha256": reference_pass.get("_sha256"),
            "requested_listing_count": len(selected_ids),
            "detail_count": len(details),
            "failure_count": len(failures),
        },
        "details": details,
        "failures": failures,
        "limitations": [
            "Detail text supplements frozen listing data and does not establish authenticity.",
            "Condition risks exclude automated prioritization and require human review.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing shortlist details: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    deals = load_json(args.deals)
    reference_pass = load_json(args.reference_pass)
    deals["_sha256"] = sha256_file(args.deals)
    reference_pass["_sha256"] = sha256_file(args.reference_pass)
    selected_ids = shortlist_ids(reference_pass, args.listing_id)
    result = enrich_shortlist(
        requests.Session(),
        deals,
        reference_pass,
        selected_ids=selected_ids,
        limit=args.limit,
        fetched_at=utc_now(),
    )
    write_json(args.output, result)
    print(json.dumps({"output": str(args.output), **result["run"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
