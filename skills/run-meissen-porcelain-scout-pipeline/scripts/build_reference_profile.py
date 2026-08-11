from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


OBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("service", re.compile(r"\b(service|serviceware|tea service|coffee service)\b", re.I)),
    ("figurine", re.compile(r"\b(figur|figure|figurine|statuette|sculpture)\b", re.I)),
    ("vase", re.compile(r"\b(vase|vas)\b", re.I)),
    ("teapot", re.compile(r"\b(teapot|tea pot|coffee pot|kaffeekanne|teekanne)\b", re.I)),
    ("jug", re.compile(r"\b(jug|pitcher|krug)\b", re.I)),
    ("cup", re.compile(r"\b(cup|teacup|tasse)\b", re.I)),
    ("saucer", re.compile(r"\b(saucer|untertasse)\b", re.I)),
    ("plate", re.compile(r"\b(plate|teller)\b", re.I)),
    ("bowl", re.compile(r"\b(bowl|schale)\b", re.I)),
    ("box", re.compile(r"\b(box|dose)\b", re.I)),
    ("candlestick", re.compile(r"\b(candlestick|leuchter)\b", re.I)),
)
PIECE_COUNT_PATTERN = re.compile(
    r"\b(\d{1,3})\s*(?:pcs?\.?|pieces?|parts?|teile?|teilig)\b", re.I
)
RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("meissen_style", re.compile(r"\b(meissen[- ]?style|meissen-like|style meissen)\b", re.I)),
    ("after_meissen", re.compile(r"\b(after meissen|nach meissen)\b", re.I)),
    ("dresden_or_saxony", re.compile(r"\b(dresden|saxony|sachsen)\b", re.I)),
    ("reproduction", re.compile(r"\b(reproduction|repro|copy|replica)\b", re.I)),
    ("condition", re.compile(r"\b(restored|repair|repaired|damage|damaged|chip|crack)\b", re.I)),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a cautious, title-only Meissen reference profile."
    )
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def classify_title(title: str) -> tuple[str, int | None, list[str]]:
    object_type = "unknown"
    for candidate, pattern in OBJECT_PATTERNS:
        if pattern.search(title):
            object_type = candidate
            break
    piece_count_match = PIECE_COUNT_PATTERN.search(title)
    piece_count = int(piece_count_match.group(1)) if piece_count_match else None
    risks = [name for name, pattern in RISK_PATTERNS if pattern.search(title)]
    return object_type, piece_count, risks


def decimal_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price.is_finite() else None


def percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    if not values:
        raise ValueError("Cannot calculate a percentile for no values")
    index = int((len(values) - 1) * fraction)
    return values[index]


def price_summary(values: list[Decimal]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": format(ordered[0], "f"),
        "p25": format(percentile(ordered, Decimal("0.25")), "f"),
        "median": format(percentile(ordered, Decimal("0.50")), "f"),
        "p75": format(percentile(ordered, Decimal("0.75")), "f"),
        "max": format(ordered[-1], "f"),
    }


def build_profile(corpus: dict[str, Any]) -> dict[str, Any]:
    records = corpus.get("records")
    if not isinstance(records, list):
        raise ValueError("Reference corpus has no records list")

    profiled_records: list[dict[str, Any]] = []
    grouped_prices: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    object_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Reference corpus contains a non-object record")
        title = str(record.get("title") or "")
        object_type, piece_count, risks = classify_title(title)
        profile = {
            "reference_id": str(record.get("reference_id") or ""),
            "object_type": object_type,
            "piece_count": piece_count,
            "risks": risks,
            "classification_evidence": ["title"],
        }
        profiled_records.append(profile)
        object_counts[object_type] += 1
        risk_counts.update(risks)

        price = decimal_price(record.get("price_value"))
        if price is not None:
            grouped_prices[
                (
                    object_type,
                    str(record.get("currency") or "unknown"),
                    str(record.get("price_basis") or "unknown"),
                )
            ].append(price)

    groups = []
    for (object_type, currency, price_basis), prices in sorted(grouped_prices.items()):
        groups.append(
            {
                "object_type": object_type,
                "currency": currency,
                "price_basis": price_basis,
                "price_summary": price_summary(prices),
            }
        )

    return {
        "schema_version": 1,
        "profile_kind": "title_only_preliminary",
        "reference_corpus_generated_at": corpus.get("generated_at"),
        "reference_count": len(profiled_records),
        "classified_count": sum(
            count for object_type, count in object_counts.items() if object_type != "unknown"
        ),
        "object_type_counts": dict(sorted(object_counts.items())),
        "risk_counts": dict(sorted(risk_counts.items())),
        "price_groups": groups,
        "records": profiled_records,
        "limitations": [
            "Classification uses titles only; images, marks, decor, dimensions, and condition are not verified.",
            "Price groups do not normalize currencies or price bases.",
            "A group is not an exact-comparable set and must not be used as an automatic buy signal.",
        ],
    }


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite frozen profile: {args.output}")
    try:
        corpus = json.loads(args.references.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot load reference corpus: {exc}") from exc
    if not isinstance(corpus, dict) or corpus.get("category") != "meissen_porcelain":
        raise SystemExit("Input is not a Meissen reference corpus")

    profile = build_profile(corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "reference_count": profile["reference_count"],
                "classified_count": profile["classified_count"],
                "unknown_count": profile["object_type_counts"].get("unknown", 0),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
