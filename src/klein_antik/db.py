from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .catalog import EXPECTED_QUERY_COUNT, load_queries
from .config import database_url


@contextmanager
def connection() -> Iterator[Any]:
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        yield conn


def init_schema() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_queries (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                category_label TEXT NOT NULL,
                position INTEGER NOT NULL UNIQUE,
                query_text TEXT NOT NULL,
                ebay_domain TEXT NOT NULL,
                ebay_category_id TEXT,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                note TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (review_status IN ('unreviewed', 'good', 'refine', 'discard'))
            );

            CREATE TABLE IF NOT EXISTS reference_runs (
                id BIGSERIAL PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                planned_calls INTEGER NOT NULL,
                completed_calls INTEGER NOT NULL DEFAULT 0,
                successful_calls INTEGER NOT NULL DEFAULT 0,
                failed_calls INTEGER NOT NULL DEFAULT 0,
                imported_results INTEGER NOT NULL DEFAULT 0,
                unique_listings INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (status IN (
                    'queued', 'running', 'completed', 'completed_with_errors',
                    'blocked', 'cancel_requested', 'cancelled', 'failed'
                ))
            );

            CREATE TABLE IF NOT EXISTS reference_run_queries (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES reference_runs(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                page INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                result_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                reported_total_results BIGINT,
                serpapi_search_id TEXT,
                error TEXT NOT NULL DEFAULT '',
                raw_response JSONB,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                UNIQUE (run_id, query_id, page),
                CHECK (page = 1),
                CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
            );

            CREATE TABLE IF NOT EXISTS reference_listings (
                id BIGSERIAL PRIMARY KEY,
                product_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                price_value NUMERIC,
                price_raw TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                condition_text TEXT NOT NULL DEFAULT '',
                sold_date TEXT NOT NULL DEFAULT '',
                shipping_text TEXT NOT NULL DEFAULT '',
                seller JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS reference_listings_price_idx
                ON reference_listings (price_value);
            CREATE INDEX IF NOT EXISTS reference_listings_last_seen_idx
                ON reference_listings (last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS listing_query_matches (
                listing_id BIGINT NOT NULL REFERENCES reference_listings(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                first_run_id BIGINT NOT NULL REFERENCES reference_runs(id),
                last_run_id BIGINT NOT NULL REFERENCES reference_runs(id),
                best_rank INTEGER NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (listing_id, query_id)
            );

            CREATE INDEX IF NOT EXISTS listing_query_matches_query_idx
                ON listing_query_matches (query_id, listing_id);

            CREATE TABLE IF NOT EXISTS listing_reviews (
                listing_id BIGINT PRIMARY KEY REFERENCES reference_listings(id) ON DELETE CASCADE,
                content_status TEXT NOT NULL DEFAULT 'unreviewed',
                use_status TEXT NOT NULL DEFAULT 'price_image',
                tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (content_status IN ('unreviewed', 'usable', 'unclear', 'unusable')),
                CHECK (use_status IN ('price_image', 'price_only', 'image_only', 'do_not_use'))
            );

            CREATE TABLE IF NOT EXISTS worker_status (
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                api_key_configured BOOLEAN NOT NULL DEFAULT FALSE,
                current_run_id BIGINT REFERENCES reference_runs(id) ON DELETE SET NULL,
                message TEXT NOT NULL DEFAULT '',
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        seed_queries(conn)


def seed_queries(conn: Any) -> None:
    queries = load_queries()
    for item in queries:
        conn.execute(
            """
            INSERT INTO search_queries (
                id, category, category_label, position, query_text,
                ebay_domain, ebay_category_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                category = EXCLUDED.category,
                category_label = EXCLUDED.category_label,
                position = EXCLUDED.position,
                query_text = EXCLUDED.query_text,
                ebay_domain = EXCLUDED.ebay_domain,
                ebay_category_id = EXCLUDED.ebay_category_id,
                updated_at = now()
            """,
            (
                item["id"],
                item["category"],
                item["category_label"],
                item["position"],
                item["query"],
                item["ebay_domain"],
                item.get("category_id"),
            ),
        )
    row = conn.execute("SELECT COUNT(*) AS count FROM search_queries WHERE enabled").fetchone()
    if not row or int(row["count"]) != EXPECTED_QUERY_COUNT:
        raise RuntimeError(
            f"Es muessen genau {EXPECTED_QUERY_COUNT} aktive Pilot-Suchen vorhanden sein."
        )

