from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


OBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("service_or_set", re.compile(r"\b(?:service|set|collection|parts?|pieces?|pcs?)\b", re.I)),
    ("figurine", re.compile(r"\b(?:figurine|figure|figural|statue)\b", re.I)),
    ("wall_bracket", re.compile(r"\bwall bracket\b", re.I)),
    ("candlestick", re.compile(r"\b(?:candlestick|candleholder)\b", re.I)),
    ("teapot_or_coffee_pot", re.compile(r"\b(?:teapot|coffee pot|tea pot)\b", re.I)),
    ("cup_and_saucer", re.compile(r"\b(?:cup with saucer|cup and saucer|mocha cup)\b", re.I)),
    ("vase", re.compile(r"\b(?:vase|beaker)\b", re.I)),
    ("box", re.compile(r"\b(?:lidded box|box)\b", re.I)),
    ("tile_or_plaque", re.compile(r"\b(?:tile|portrait|plaque)\b", re.I)),
    ("ashtray", re.compile(r"\bashtray\b", re.I)),
    ("plate_or_dish", re.compile(r"\b(?:plate|dish|platter|bowl|cake plate)\b", re.I)),
)
QUALITY_PATTERN = re.compile(
    r"\b(?:second|2nd|third|3rd|fourth|4th)"
    r"(?:\s*(?:/|and)\s*(?:second|2nd|third|3rd|fourth|4th))*"
    r"\s+(?:quality|choice|wahl)\b",
    re.I,
)
STYLE_PATTERN = re.compile(r"\bmeissen[- ]?(?:style|like)\b", re.I)
OTHER_MAKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("other_maker_chelsea", re.compile(r"\bchelsea porcelain\b", re.I)),
    ("other_maker_hc_selb", re.compile(r"\bh\s*&\s*c\s+selb\b", re.I)),
)
UNVERIFIED_MAKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("maker_unverified_stadt_meissen", re.compile(r"\bstadt meissen\b", re.I)),
    ("maker_unverified_npm", re.compile(r"\bNPM\s+meissen\b", re.I)),
    ("maker_unverified_somag", re.compile(r"\bsomag\s+meissen\b", re.I)),
)
PIECE_COUNT_PATTERN = re.compile(r"\b(\d{1,3})\s*(?:pcs?\.?|pieces?|parts?)\b", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a zero-shot, non-price Meissen deal triage from frozen evidence."
    )
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root in {path} must be an object")
    return payload


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_object(title: str) -> str:
    for object_type, pattern in OBJECT_PATTERNS:
        if pattern.search(title):
            return object_type
    return "unknown"


def classify_risks(title: str, source_risks: list[str]) -> list[str]:
    risks = list(source_risks)
    for name, pattern in OTHER_MAKER_PATTERNS:
        if pattern.search(title):
            risks.append(name)
    for name, pattern in UNVERIFIED_MAKER_PATTERNS:
        if pattern.search(title):
            risks.append(name)
    if QUALITY_PATTERN.search(title):
        risks.append("quality_or_seconds")
    if STYLE_PATTERN.search(title):
        risks.append("meissen_style")
    return sorted(set(risks))


def screening_status(risks: list[str]) -> str:
    if "meissen_style" in risks or any(risk.startswith("other_maker_") for risk in risks):
        return "reject_before_reference_pass"
    if "quality_or_seconds" in risks:
        return "restricted_comparables_only"
    if any(risk.startswith("maker_unverified_") for risk in risks):
        return "verify_maker_before_reference_pass"
    return "manual_object_review_required"


def build_zero_shot(
    deals: dict[str, Any], image_manifest: dict[str, Any], *, deal_hash: str, image_hash: str
) -> dict[str, Any]:
    listings = deals.get("listings")
    images = image_manifest.get("images")
    if not isinstance(listings, list) or not isinstance(images, list):
        raise RuntimeError("Frozen deal and image inputs must contain lists")
    image_ids = {
        str(image.get("listing_id") or "")
        for image in images
        if isinstance(image, dict) and str(image.get("image_file") or "")
    }
    records: list[dict[str, Any]] = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        listing_id = str(listing.get("listing_id") or "")
        title = str(listing.get("title") or "")
        source_risks = [str(value) for value in listing.get("risks", []) if str(value)]
        risks = classify_risks(title, source_risks)
        count_match = PIECE_COUNT_PATTERN.search(title)
        records.append(
            {
                "listing_id": listing_id,
                "object_type": classify_object(title),
                "piece_count": int(count_match.group(1)) if count_match else None,
                "title": title,
                "source_risks": source_risks,
                "risks": risks,
                "image_evidence_frozen": listing_id in image_ids,
                "classification_evidence": ["title", "contact_sheet"],
                "screening_status": screening_status(risks),
                "requires_individual_image_review": True,
                "uses_reference_prices": False,
            }
        )
    status_counts = Counter(record["screening_status"] for record in records)
    object_counts = Counter(record["object_type"] for record in records)
    risk_counts = Counter(risk for record in records for risk in record["risks"])
    return {
        "input_hashes": {"deals": deal_hash, "images": image_hash},
        "summary": {
            "record_count": len(records),
            "missing_image_evidence": sum(not record["image_evidence_frozen"] for record in records),
            "screening_status_counts": dict(sorted(status_counts.items())),
            "object_type_counts": dict(sorted(object_counts.items())),
            "risk_counts": dict(sorted(risk_counts.items())),
            "uses_reference_prices": False,
        },
        "records": records,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing zero-shot file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    deal_payload = load_json(args.deals)
    image_payload = load_json(args.images)
    result = build_zero_shot(
        deal_payload,
        image_payload,
        deal_hash=sha256_file(args.deals),
        image_hash=sha256_file(args.images),
    )
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
