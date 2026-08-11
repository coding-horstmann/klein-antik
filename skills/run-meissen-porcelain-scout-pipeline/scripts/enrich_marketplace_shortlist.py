from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests


FORBIDDEN_MARKERS = ("ebay", "serpapi")
SHORTLIST_STATUSES = {
    "title_match_requires_image_review",
    "reference_cue_requires_object_verification",
}
USER_AGENT = "KleinAntikMeissenScout/1.0 (auditable detail enrichment)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze Railway-fetched detail evidence for a marketplace Meissen shortlist."
    )
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--run-id", type=int, action="append", required=True)
    parser.add_argument("--reference-pass", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--listing-id", action="append")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Reference pass must be a JSON object")
    return payload


def shortlist_ids(payload: dict[str, Any], explicit_ids: list[str] | None) -> list[str]:
    if explicit_ids:
        return list(dict.fromkeys(str(value).strip() for value in explicit_ids if str(value).strip()))
    records = payload.get("records")
    if not isinstance(records, list):
        raise RuntimeError("Reference pass has no records list")
    return [
        str(record.get("listing_id") or "")
        for record in records
        if isinstance(record, dict)
        and str(record.get("status") or "") in SHORTLIST_STATUSES
        and str(record.get("listing_id") or "")
    ]


def fetch_details(
    session: requests.Session,
    *,
    dashboard_url: str,
    run_ids: list[int],
    listing_ids: list[str],
) -> dict[str, Any]:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASSWORD are required")
    response = session.post(
        f"{dashboard_url.rstrip('/')}/api/exports/meissen-deal-pilot/enrich",
        auth=(username, password),
        headers={"User-Agent": USER_AGENT},
        json={"run_ids": run_ids, "listing_ids": listing_ids},
        timeout=180,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Detail enrichment did not return JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("details"), list):
        raise RuntimeError("Detail enrichment has no details list")
    return payload


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite frozen detail output: {args.output}")
    reference_pass = load_json(args.reference_pass)
    listing_ids = shortlist_ids(reference_pass, args.listing_id)
    if not listing_ids:
        raise SystemExit("Reference pass contains no shortlist listings")
    if len(listing_ids) > 25:
        raise SystemExit("Shortlist exceeds Railway detail limit of 25 listings")
    payload = fetch_details(
        requests.Session(),
        dashboard_url=args.dashboard_url,
        run_ids=list(dict.fromkeys(args.run_id)),
        listing_ids=listing_ids,
    )
    payload["input_hashes"] = {"reference_pass": sha256_file(args.reference_pass)}
    payload["frozen_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise SystemExit(f"Detail enrichment contains forbidden marker(s): {', '.join(found)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(encoded, encoding="utf-8")
    print(
        json.dumps(
            {
                "requested_count": payload.get("requested_count"),
                "detail_count": payload.get("detail_count"),
                "failure_count": payload.get("failure_count"),
                "output": str(args.output.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
