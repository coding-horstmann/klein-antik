from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row


REFERENCE_QUERY = """
    SELECT
        l.source,
        l.source_item_id,
        l.title,
        l.url,
        l.image_url,
        l.price_value,
        l.price_raw,
        l.currency,
        l.price_basis,
        l.sale_date,
        l.attribution,
        l.first_seen_at,
        l.last_seen_at,
        array_agg(DISTINCT q.id ORDER BY q.id) AS query_ids,
        array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts
    FROM market_listings AS l
    JOIN market_listing_query_matches AS qm ON qm.listing_id = l.id
    JOIN search_queries AS q ON q.id = qm.query_id
    WHERE q.category = 'meissen_porcelain'
      AND l.price_status = 'sold'
      AND l.price_value IS NOT NULL
    GROUP BY l.id
    ORDER BY l.source, l.source_item_id
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export sold Meissen market references without modifying the database."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--dashboard-url",
        help="Use the protected dashboard export when DATABASE_URL is internal-only.",
    )
    return parser.parse_args()


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return value


def count_values(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(record.get(key) or "unknown") for record in records)
    return dict(sorted(counts.items()))


def export_corpus(database_url: str) -> dict[str, Any]:
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    ) as connection:
        rows = connection.execute(REFERENCE_QUERY).fetchall()

    records: list[dict[str, Any]] = []
    reference_ids: set[str] = set()
    for row in rows:
        record = json_value(dict(row))
        reference_id = f"{record['source']}:{record['source_item_id']}"
        if reference_id in reference_ids:
            raise RuntimeError(f"Duplicate reference ID: {reference_id}")
        reference_ids.add(reference_id)
        record["reference_id"] = reference_id
        records.append(record)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "category": "meissen_porcelain",
        "filters": {
            "price_status": "sold",
            "priced_only": True,
            "forbidden_deal_sources_excluded": True,
        },
        "record_count": len(records),
        "source_counts": count_values(records, "source"),
        "currency_counts": count_values(records, "currency"),
        "price_basis_counts": count_values(records, "price_basis"),
        "records": records,
    }


def export_from_dashboard(dashboard_url: str) -> dict[str, Any]:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError(
            "DASHBOARD_USER and DASHBOARD_PASSWORD are required for dashboard export"
        )
    response = requests.get(
        f"{dashboard_url.rstrip('/')}/api/exports/meissen-references",
        auth=(username, password),
        timeout=90,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Dashboard export did not return JSON") from exc
    if not isinstance(payload, dict) or payload.get("category") != "meissen_porcelain":
        raise RuntimeError("Dashboard export is not a Meissen reference corpus")
    if not isinstance(payload.get("records"), list):
        raise RuntimeError("Dashboard export has no records list")
    return payload


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite frozen export: {args.output}")

    if args.dashboard_url:
        payload = export_from_dashboard(args.dashboard_url)
    else:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise SystemExit("DATABASE_URL is required without --dashboard-url")
        payload = export_corpus(database_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    args.output.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "record_count": payload["record_count"],
                "sha256": digest,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
