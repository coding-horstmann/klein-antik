from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests


ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FORBIDDEN_MARKERS = ("ebay", "serpapi")
USER_AGENT = "KleinAntikMeissenScout/1.0 (auditable marketplace export)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Railway-collected Meissen marketplace offers as a frozen deal batch."
    )
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--run-id", type=int, action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id-label")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_rates(session: requests.Session) -> tuple[str, dict[str, Decimal]]:
    response = session.get(ECB_RATES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    snapshot_date = ""
    rates = {"EUR": Decimal("1")}
    for node in root.iter():
        if node.attrib.get("time") and not node.attrib.get("currency"):
            snapshot_date = str(node.attrib["time"])
        currency = node.attrib.get("currency")
        rate = node.attrib.get("rate")
        if currency and rate:
            try:
                rates[currency.upper()] = Decimal(rate)
            except InvalidOperation:
                continue
    if not snapshot_date or len(rates) < 2:
        raise RuntimeError("ECB exchange-rate snapshot was incomplete")
    return snapshot_date, rates


def price_eur(value: Any, currency: str, rates: dict[str, Decimal]) -> tuple[str | None, str | None]:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    rate = rates.get(currency.upper())
    if amount <= 0 or rate is None or rate <= 0:
        return None, None
    return format(amount / rate, ".2f"), format(rate, "f")


def export_payload(
    session: requests.Session,
    *,
    dashboard_url: str,
    run_ids: list[int],
    run_id_label: str | None,
) -> dict[str, Any]:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASSWORD are required")
    response = session.get(
        f"{dashboard_url.rstrip('/')}/api/exports/meissen-deal-pilot",
        params=[("run_id", str(run_id)) for run_id in run_ids],
        auth=(username, password),
        headers={"User-Agent": USER_AGENT},
        timeout=90,
    )
    response.raise_for_status()
    try:
        exported = response.json()
    except ValueError as exc:
        raise RuntimeError("Marketplace export did not return JSON") from exc
    listings = exported.get("listings") if isinstance(exported, dict) else None
    if not isinstance(listings, list):
        raise RuntimeError("Marketplace export has no listings list")
    snapshot_date, rates = fetch_rates(session)
    normalized: list[dict[str, Any]] = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        copied = dict(listing)
        converted, rate = price_eur(
            copied.get("price_value"), str(copied.get("currency") or ""), rates
        )
        if converted is None or rate is None:
            continue
        copied["price_eur"] = converted
        copied["fx_rate"] = rate
        normalized.append(copied)
    discovery_by_scope: dict[str, set[str]] = {}
    for listing in normalized:
        queries = listing.get("discovery_queries")
        scopes = listing.get("discovery_scopes")
        if not isinstance(queries, list) or not isinstance(scopes, list):
            continue
        for scope in scopes:
            discovery_by_scope.setdefault(str(scope), set()).update(
                str(query) for query in queries if str(query).strip()
            )
    payload = {
        "run": {
            "run_id": run_id_label or "marketplace-" + "-".join(map(str, run_ids)),
            "collected_at": utc_now(),
            "sources": exported.get("sources", []),
            "source_run_ids": run_ids,
            "discovery": [
                {"scope": scope, "queries": sorted(queries)}
                for scope, queries in sorted(discovery_by_scope.items())
            ],
            "ecb_snapshot_date": snapshot_date,
            "forbidden_sources_checked": True,
        },
        "listings": normalized,
    }
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise RuntimeError(
            f"Marketplace export contains forbidden marker(s): {', '.join(found)}"
        )
    return payload


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite frozen export: {args.output}")
    payload = export_payload(
        requests.Session(),
        dashboard_url=args.dashboard_url,
        run_ids=list(dict.fromkeys(args.run_id)),
        run_id_label=args.run_id_label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.write_bytes(encoded)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "listing_count": len(payload["listings"]),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
