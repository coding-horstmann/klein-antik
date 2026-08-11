from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests


ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
USER_AGENT = "KleinAntikMeissenScout/1.0 (reference review)"
FORBIDDEN_MARKERS = ("ebay", "serpapi")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "antique",
    "approx",
    "choice",
    "century",
    "ceramic",
    "china",
    "cobalt",
    "color",
    "colour",
    "court",
    "decorated",
    "decoration",
    "dragon",
    "first",
    "floral",
    "flower",
    "for",
    "from",
    "germany",
    "gold",
    "glazed",
    "green",
    "group",
    "in",
    "meissen",
    "meissner",
    "ming",
    "motif",
    "of",
    "on",
    "painted",
    "painting",
    "polychrome",
    "porcelain",
    "quality",
    "red",
    "rich",
    "rose",
    "second",
    "the",
    "third",
    "twentieth",
    "white",
    "with",
    "yellow",
}
RISK_EXCLUSIONS = {"meissen_style", "after_meissen", "dresden_or_saxony", "reproduction"}
MODEL_PATTERN = re.compile(
    r"\b(?:model|mod(?:el)?|form|no|nr)\.?\s*([a-z]{0,3}\d{1,5}[a-z]{0,3})\b",
    re.I,
)
PHRASES = (
    "blue onion",
    "court dragon",
    "ming dragon",
    "red rose",
    "rote rose",
    "tischchenmuster",
    "vine leaf",
    "red phoenix",
    "indisch purpur",
    "indisch reich",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a cautious, title-led Meissen reference shortlist."
    )
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--reference-profile", required=True, type=Path)
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--zero-shot", required=True, type=Path)
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


def normalized_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", ascii_text).strip()


def title_features(title: str) -> tuple[set[str], set[str], set[str]]:
    text = normalized_text(title)
    models = {match.casefold() for match in MODEL_PATTERN.findall(text)}
    phrases = {phrase for phrase in PHRASES if phrase in text}
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9]{2,}", text)
        if token not in STOP_WORDS and not token.isdigit()
    }
    for phrase in phrases:
        tokens.difference_update(phrase.split())
    return models, phrases, tokens


def parse_price(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def fetch_ecb_rates() -> tuple[str, dict[str, Decimal]]:
    response = requests.get(ECB_RATES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    snapshot_date = ""
    rates = {"EUR": Decimal("1")}
    for node in root.iter():
        if node.attrib.get("time") and not node.attrib.get("currency"):
            snapshot_date = str(node.attrib["time"])
        currency = str(node.attrib.get("currency") or "").upper()
        rate = node.attrib.get("rate")
        if currency and rate:
            try:
                rates[currency] = Decimal(str(rate))
            except InvalidOperation:
                continue
    if not snapshot_date or len(rates) == 1:
        raise RuntimeError("ECB exchange-rate snapshot was incomplete")
    return snapshot_date, rates


def as_eur(value: Decimal | None, currency: str, rates: dict[str, Decimal]) -> Decimal | None:
    rate = rates.get(currency.upper())
    if value is None or rate is None or rate <= 0:
        return None
    return value / rate


def percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def price_summary(records: list[dict[str, Any]], rates: dict[str, Decimal]) -> dict[str, Any] | None:
    values: list[Decimal] = []
    bases: Counter[str] = Counter()
    for record in records:
        price = as_eur(
            parse_price(record.get("price_value")),
            str(record.get("currency") or ""),
            rates,
        )
        if price is not None:
            values.append(price)
            bases[str(record.get("price_basis") or "unknown")] += 1
    if not values:
        return None
    return {
        "count": len(values),
        "p25_eur": format(percentile(values, Decimal("0.25")), ".2f"),
        "median_eur": format(percentile(values, Decimal("0.50")), ".2f"),
        "p75_eur": format(percentile(values, Decimal("0.75")), ".2f"),
        "price_basis_counts": dict(sorted(bases.items())),
    }


def reference_match(
    deal_title: str,
    reference_title: str,
    *,
    token_frequency: Counter[str],
) -> tuple[int, str, list[str]]:
    deal_models, deal_phrases, deal_tokens = title_features(deal_title)
    reference_models, reference_phrases, reference_tokens = title_features(reference_title)
    shared_models = sorted(deal_models & reference_models)
    shared_phrases = sorted(deal_phrases & reference_phrases)
    shared_tokens = sorted(deal_tokens & reference_tokens)
    rare_tokens = [
        token for token in shared_tokens if token_frequency[token] <= 4 and len(token) >= 5
    ]
    if shared_models:
        return 100, "model_title_match", shared_models
    if shared_phrases:
        return 40 + len(shared_phrases), "decor_title_match", shared_phrases
    if rare_tokens:
        return 20 + len(rare_tokens), "rare_name_title_match", rare_tokens
    return 0, "no_title_match", []


def build_reference_pass(
    references: dict[str, Any],
    profile: dict[str, Any],
    deals: dict[str, Any],
    zero_shot: dict[str, Any],
    *,
    rates: dict[str, Decimal],
    snapshot_date: str,
) -> dict[str, Any]:
    reference_records = references.get("records")
    profile_records = profile.get("records")
    deal_records = deals.get("listings")
    zero_records = zero_shot.get("records")
    if not all(isinstance(value, list) for value in (reference_records, profile_records, deal_records, zero_records)):
        raise RuntimeError("Frozen inputs must contain record lists")

    raw_references = {
        str(record.get("reference_id") or ""): record
        for record in reference_records
        if isinstance(record, dict) and str(record.get("reference_id") or "")
    }
    profile_by_id = {
        str(record.get("reference_id") or ""): record
        for record in profile_records
        if isinstance(record, dict) and str(record.get("reference_id") or "")
    }
    zero_by_id = {
        str(record.get("listing_id") or ""): record
        for record in zero_records
        if isinstance(record, dict) and str(record.get("listing_id") or "")
    }

    usable_by_type: dict[str, list[dict[str, Any]]] = {}
    unknown_object_references: list[dict[str, Any]] = []
    token_frequencies: dict[str, Counter[str]] = {}
    for reference_id, raw_reference in raw_references.items():
        profile_record = profile_by_id.get(reference_id)
        if not profile_record:
            continue
        object_type = str(profile_record.get("object_type") or "unknown")
        risks = {str(risk) for risk in profile_record.get("risks", [])}
        if risks & RISK_EXCLUSIONS:
            continue
        if object_type == "unknown":
            unknown_object_references.append(raw_reference)
            continue
        usable_by_type.setdefault(object_type, []).append(raw_reference)
        token_frequencies.setdefault(object_type, Counter()).update(title_features(str(raw_reference.get("title") or ""))[2])

    review_records: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    for deal in deal_records:
        if not isinstance(deal, dict):
            continue
        listing_id = str(deal.get("listing_id") or "")
        zero_record = zero_by_id.get(listing_id, {})
        object_type = str(zero_record.get("object_type") or "unknown")
        screening_status = str(zero_record.get("screening_status") or "")
        attribution = str(zero_record.get("attribution_evidence") or "")
        result: dict[str, Any] = {
            "listing_id": listing_id,
            "title": str(deal.get("title") or ""),
            "url": str(deal.get("url") or ""),
            "object_type": object_type,
            "deal_price_eur": deal.get("price_eur"),
            "sale_mode": deal.get("sale_mode"),
            "auction_end": deal.get("auction_end"),
            "discovery_queries": deal.get("discovery_queries", []),
            "discovery_scopes": deal.get("discovery_scopes", []),
            "attribution_evidence": attribution,
            "risks": zero_record.get("risks", []),
            "manual_review_required": True,
        }
        if screening_status == "reject_before_reference_pass":
            result["status"] = "excluded_by_zero_shot"
            result["potential_comparables"] = []
        elif attribution not in {"title_claim", "canonical_url_claim"} or screening_status != "manual_object_review_required":
            result["status"] = "needs_attribution_evidence"
            result["potential_comparables"] = []
        elif object_type == "unknown":
            result["status"] = "needs_object_identification"
            result["potential_comparables"] = []
        else:
            candidates: list[tuple[int, str, list[str], dict[str, Any]]] = []
            for reference in usable_by_type.get(object_type, []):
                score, match_kind, signals = reference_match(
                    result["title"],
                    str(reference.get("title") or ""),
                    token_frequency=token_frequencies.get(object_type, Counter()),
                )
                if score:
                    candidates.append((score, match_kind, signals, reference))
            candidates.sort(key=lambda candidate: (-candidate[0], str(candidate[3].get("reference_id") or "")))
            selected = candidates[:8]
            potential_references = [candidate[3] for candidate in selected]
            result["potential_comparables"] = [
                {
                    "reference_id": str(reference.get("reference_id") or ""),
                    "match_kind": match_kind,
                    "title_signals": signals,
                    "title": str(reference.get("title") or ""),
                    "url": str(reference.get("url") or ""),
                    "price_value": reference.get("price_value"),
                    "currency": reference.get("currency"),
                    "price_basis": reference.get("price_basis"),
                    "price_eur_directional": (
                        format(
                            as_eur(
                                parse_price(reference.get("price_value")),
                                str(reference.get("currency") or ""),
                                rates,
                            ),
                            ".2f",
                        )
                        if as_eur(
                            parse_price(reference.get("price_value")),
                            str(reference.get("currency") or ""),
                            rates,
                        )
                        is not None
                        else None
                    ),
                }
                for _, match_kind, signals, reference in selected
            ]
            result["directional_price_summary"] = price_summary(potential_references, rates)
            if selected:
                result["status"] = "title_match_requires_image_review"
            else:
                unknown_frequencies = Counter(
                    token
                    for reference in unknown_object_references
                    for token in title_features(str(reference.get("title") or ""))[2]
                )
                cues: list[tuple[int, str, list[str], dict[str, Any]]] = []
                for reference in unknown_object_references:
                    score, match_kind, signals = reference_match(
                        result["title"],
                        str(reference.get("title") or ""),
                        token_frequency=unknown_frequencies,
                    )
                    if score:
                        cues.append((score, match_kind, signals, reference))
                cues.sort(key=lambda cue: (-cue[0], str(cue[3].get("reference_id") or "")))
                result["identification_cues"] = [
                    {
                        "reference_id": str(reference.get("reference_id") or ""),
                        "match_kind": match_kind,
                        "title_signals": signals,
                        "title": str(reference.get("title") or ""),
                        "url": str(reference.get("url") or ""),
                    }
                    for _, match_kind, signals, reference in cues[:3]
                ]
                result["status"] = (
                    "reference_cue_requires_object_verification"
                    if result["identification_cues"]
                    else "needs_object_identification"
                )
        statuses[str(result["status"])] += 1
        review_records.append(result)

    return {
        "schema_version": 1,
        "kind": "title_led_reference_shortlist",
        "input_hashes": {
            "references": sha256_file(Path(references["_path"])),
            "reference_profile": sha256_file(Path(profile["_path"])),
            "deals": sha256_file(Path(deals["_path"])),
            "zero_shot": sha256_file(Path(zero_shot["_path"])),
        },
        "fx_snapshot": {"provider": "ECB", "date": snapshot_date},
        "summary": {
            "record_count": len(review_records),
            "status_counts": dict(sorted(statuses.items())),
            "limitations": [
                "Title matches are a shortlist for image and mark review, not exact comparables.",
                "Directional EUR values use one current ECB snapshot and preserve the original price basis.",
                "This pass never produces a buy, bid, or automatic priority decision.",
            ],
        },
        "records": review_records,
    }


def attach_path(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["_path"] = str(path)
    return enriched


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing reference pass: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    snapshot_date, rates = fetch_ecb_rates()
    result = build_reference_pass(
        attach_path(load_json(args.references), args.references),
        attach_path(load_json(args.reference_profile), args.reference_profile),
        attach_path(load_json(args.deals), args.deals),
        attach_path(load_json(args.zero_shot), args.zero_shot),
        rates=rates,
        snapshot_date=snapshot_date,
    )
    write_json(args.output, result)
    print(json.dumps({"output": str(args.output), **result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
