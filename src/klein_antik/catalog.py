from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
QUERY_FILE = ROOT / "config" / "search_queries.json"
EXPECTED_QUERY_COUNT = 110


def load_queries() -> list[dict[str, Any]]:
    queries = json.loads(QUERY_FILE.read_text(encoding="utf-8"))
    if not isinstance(queries, list):
        raise ValueError("Die Suchmatrix muss eine Liste sein.")
    if len(queries) != EXPECTED_QUERY_COUNT:
        raise ValueError(
            f"Die Pilotmatrix muss genau {EXPECTED_QUERY_COUNT} Suchanfragen enthalten, "
            f"gefunden wurden {len(queries)}."
        )

    ids: set[str] = set()
    for position, query in enumerate(queries, start=1):
        required = {"id", "category", "category_label", "query", "ebay_domain"}
        missing = required.difference(query)
        if missing:
            raise ValueError(f"Suchanfrage {position} ohne Pflichtfelder: {sorted(missing)}")
        if query["id"] in ids:
            raise ValueError(f"Doppelte Such-ID: {query['id']}")
        ids.add(query["id"])
        query["position"] = position
    return queries


def category_options(queries: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    seen: set[str] = set()
    categories: list[dict[str, str]] = []
    for query in queries or load_queries():
        if query["category"] in seen:
            continue
        seen.add(query["category"])
        categories.append({"id": query["category"], "label": query["category_label"]})
    return categories

