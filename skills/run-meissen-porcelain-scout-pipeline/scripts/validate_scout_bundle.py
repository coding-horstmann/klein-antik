from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


FORBIDDEN_MARKERS = ("ebay", "serpapi")
ALLOWED_SOURCES = {
    "auctionet",
    "interencheres",
    "tradera",
    "bukowskis",
    "bruun_rasmussen",
    "dba",
    "tori",
    "blocket",
    "snapphaneauktioner",
}
PRIORITIES = ("A", "B", "watch", "reject")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a frozen non-eBay Meissen scouting bundle."
    )
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read valid JSON from {path}: {exc}") from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decimal_value(value: Any, field: str, candidate_id: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            f"Candidate {candidate_id} has invalid {field}: {value!r}"
        ) from exc
    if not number.is_finite():
        raise ValidationError(f"Candidate {candidate_id} has non-finite {field}")
    return number


def require_list(payload: Any, key: str, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ValidationError(f"{label} must contain a {key!r} list")
    if not all(isinstance(item, dict) for item in payload[key]):
        raise ValidationError(f"Every {label} {key!r} item must be an object")
    return payload[key]


def reject_forbidden(payload: Any, label: str) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise ValidationError(
            f"{label} contains forbidden source marker(s): {', '.join(found)}"
        )


def unique_index(
    records: list[dict[str, Any]], key: str, label: str
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for position, record in enumerate(records, start=1):
        value = str(record.get(key) or "").strip()
        if not value:
            raise ValidationError(f"{label} item {position} has no {key}")
        if value in index:
            raise ValidationError(f"Duplicate {label} {key}: {value}")
        index[value] = record
    return index


def validate_hashes(
    candidate_payload: dict[str, Any], reference_path: Path, deal_path: Path
) -> None:
    hashes = candidate_payload.get("input_hashes")
    if not isinstance(hashes, dict):
        raise ValidationError("Candidate bundle must contain input_hashes")
    expected = {
        "references": sha256_file(reference_path),
        "deals": sha256_file(deal_path),
    }
    for key, actual in expected.items():
        claimed = str(hashes.get(key) or "").lower()
        if not SHA256_PATTERN.fullmatch(claimed):
            raise ValidationError(f"input_hashes.{key} is not a SHA-256 value")
        if claimed != actual:
            raise ValidationError(
                f"input_hashes.{key} does not match the frozen input file"
            )


def validate_bundle(
    reference_payload: Any,
    deal_payload: Any,
    candidate_payload: Any,
    reference_path: Path,
    deal_path: Path,
) -> dict[str, Any]:
    reject_forbidden(reference_payload, "Reference corpus")
    reject_forbidden(deal_payload, "Deal dataset")
    reject_forbidden(candidate_payload, "Candidate bundle")

    references = require_list(reference_payload, "records", "Reference corpus")
    deals = require_list(deal_payload, "listings", "Deal dataset")
    candidates = require_list(candidate_payload, "candidates", "Candidate bundle")
    reference_index = unique_index(references, "reference_id", "reference")
    deal_index = unique_index(deals, "listing_id", "deal")
    unique_index(candidates, "candidate_id", "candidate")
    validate_hashes(candidate_payload, reference_path, deal_path)

    for listing_id, listing in deal_index.items():
        source = str(listing.get("source") or "").strip().lower()
        if source not in ALLOWED_SOURCES:
            raise ValidationError(
                f"Deal {listing_id} uses source outside the allowlist: {source!r}"
            )
        if not str(listing.get("url") or "").startswith(("http://", "https://")):
            raise ValidationError(f"Deal {listing_id} has no canonical HTTP URL")
        images = listing.get("image_urls")
        if not isinstance(images, list) or not any(str(item).strip() for item in images):
            raise ValidationError(f"Deal {listing_id} has no image evidence")

    priority_counts: dict[str, int] = {priority: 0 for priority in PRIORITIES}
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        listing_id = str(candidate.get("listing_id") or "")
        if listing_id not in deal_index:
            raise ValidationError(
                f"Candidate {candidate_id} references unknown deal {listing_id!r}"
            )
        object_type = str(candidate.get("object_type") or "").strip().lower()
        if not object_type or object_type == "unknown":
            raise ValidationError(f"Candidate {candidate_id} has no known object_type")
        if candidate.get("manual_review_required") is not True:
            raise ValidationError(
                f"Candidate {candidate_id} must require manual review"
            )

        reference_ids = candidate.get("reference_ids")
        if not isinstance(reference_ids, list) or not reference_ids:
            raise ValidationError(f"Candidate {candidate_id} has no reference_ids")
        normalized_reference_ids = [str(item) for item in reference_ids]
        if len(normalized_reference_ids) != len(set(normalized_reference_ids)):
            raise ValidationError(
                f"Candidate {candidate_id} has duplicate reference_ids"
            )
        missing = sorted(set(normalized_reference_ids) - set(reference_index))
        if missing:
            raise ValidationError(
                f"Candidate {candidate_id} references unknown IDs: {', '.join(missing)}"
            )

        comparable_count = candidate.get("exact_comparable_count")
        if not isinstance(comparable_count, int) or comparable_count < 1:
            raise ValidationError(
                f"Candidate {candidate_id} has invalid exact_comparable_count"
            )
        if comparable_count != len(normalized_reference_ids):
            raise ValidationError(
                f"Candidate {candidate_id} comparable count does not match reference_ids"
            )

        deal_price = decimal_value(
            candidate.get("deal_price_eur"), "deal_price_eur", candidate_id
        )
        conservative = decimal_value(
            candidate.get("conservative_reference_eur"),
            "conservative_reference_eur",
            candidate_id,
        )
        median = decimal_value(
            candidate.get("median_reference_eur"),
            "median_reference_eur",
            candidate_id,
        )
        spread = decimal_value(
            candidate.get("directional_spread_eur"),
            "directional_spread_eur",
            candidate_id,
        )
        ratio = decimal_value(
            candidate.get("price_ratio"), "price_ratio", candidate_id
        )
        if deal_price <= 0 or conservative <= 0 or median <= 0:
            raise ValidationError(f"Candidate {candidate_id} prices must be positive")
        if median < conservative:
            raise ValidationError(
                f"Candidate {candidate_id} median is below conservative reference"
            )
        if abs(spread - (conservative - deal_price)) > Decimal("0.02"):
            raise ValidationError(f"Candidate {candidate_id} spread is inconsistent")
        if abs(ratio - (deal_price / conservative)) > Decimal("0.02"):
            raise ValidationError(f"Candidate {candidate_id} price ratio is inconsistent")

        priority = str(candidate.get("priority") or "")
        confidence = str(candidate.get("confidence") or "").strip().lower()
        if priority not in PRIORITIES:
            raise ValidationError(
                f"Candidate {candidate_id} has invalid priority {priority!r}"
            )
        if comparable_count < 3:
            if confidence != "sparse":
                raise ValidationError(
                    f"Candidate {candidate_id} with fewer than three comparables must be sparse"
                )
            if priority in {"A", "B"}:
                raise ValidationError(
                    f"Sparse candidate {candidate_id} cannot have priority {priority}"
                )
        if not isinstance(candidate.get("risks"), list):
            raise ValidationError(f"Candidate {candidate_id} risks must be a list")
        if str(candidate.get("decision") or "").lower() in {"buy", "auto_buy", "bid"}:
            raise ValidationError(
                f"Candidate {candidate_id} contains an automated purchase decision"
            )
        priority_counts[priority] += 1

    return {
        "valid": True,
        "reference_count": len(references),
        "deal_count": len(deals),
        "candidate_count": len(candidates),
        "priority_counts": priority_counts,
        "input_hashes": {
            "references": sha256_file(reference_path),
            "deals": sha256_file(deal_path),
        },
    }


def main() -> int:
    args = parse_args()
    try:
        references = load_json(args.references)
        deals = load_json(args.deals)
        candidates = load_json(args.candidates)
        report = validate_bundle(
            references,
            deals,
            candidates,
            args.references,
            args.deals,
        )
    except ValidationError as exc:
        raise SystemExit(f"INVALID: {exc}") from exc

    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit(f"Refusing to overwrite validation report: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(f"VALID: {report['candidate_count']} candidate(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
