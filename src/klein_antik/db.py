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
                api_calls_used INTEGER NOT NULL DEFAULT 0,
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
                serpapi_calls INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS market_runs (
                id BIGSERIAL PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'refresh',
                status TEXT NOT NULL DEFAULT 'queued',
                planned_tasks INTEGER NOT NULL,
                completed_tasks INTEGER NOT NULL DEFAULT 0,
                successful_tasks INTEGER NOT NULL DEFAULT 0,
                failed_tasks INTEGER NOT NULL DEFAULT 0,
                imported_results INTEGER NOT NULL DEFAULT 0,
                unique_listings INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (status IN (
                    'queued', 'running', 'completed', 'completed_with_errors',
                    'cancel_requested', 'cancelled', 'failed'
                )),
                CONSTRAINT market_runs_kind_check
                    CHECK (kind IN ('refresh', 'backfill', 'source_pilot'))
            );

            CREATE TABLE IF NOT EXISTS market_run_tasks (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES market_runs(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                source TEXT NOT NULL,
                start_page INTEGER NOT NULL DEFAULT 1,
                page_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                result_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                UNIQUE (run_id, query_id, source),
                CHECK (source IN (
                    'auctionet', 'blocket', 'quittenbaum', 'lempertz', 'bruun_rasmussen',
                    'mehlis', 'van_ham', 'dorotheum', 'liveauctioneers', 'invaluable',
                    'christies', 'heritage'
                )),
                CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
                CONSTRAINT market_run_tasks_start_page_check CHECK (start_page >= 1),
                CONSTRAINT market_run_tasks_page_count_check CHECK (page_count >= 1)
            );

            CREATE INDEX IF NOT EXISTS market_run_tasks_run_idx
                ON market_run_tasks (run_id, status, id);

            CREATE TABLE IF NOT EXISTS market_backfill_cursors (
                query_id TEXT NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                next_page INTEGER NOT NULL,
                exhausted BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (query_id, source),
                CHECK (source IN (
                    'auctionet', 'blocket', 'quittenbaum', 'lempertz', 'bruun_rasmussen',
                    'mehlis', 'van_ham', 'dorotheum', 'liveauctioneers', 'invaluable',
                    'christies', 'heritage'
                )),
                CHECK (next_page >= 1)
            );

            CREATE INDEX IF NOT EXISTS market_backfill_cursors_pending_idx
                ON market_backfill_cursors (exhausted, query_id);

            CREATE TABLE IF NOT EXISTS market_listings (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                price_status TEXT NOT NULL DEFAULT 'unknown',
                price_value NUMERIC,
                price_raw TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                price_basis TEXT NOT NULL DEFAULT 'unknown',
                estimate_raw TEXT NOT NULL DEFAULT '',
                sale_date TEXT NOT NULL DEFAULT '',
                attribution TEXT NOT NULL DEFAULT 'stated',
                raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (source, source_item_id),
                CHECK (price_status IN (
                    'sold', 'ask', 'current_bid', 'estimate', 'unsold', 'unknown'
                )),
                CHECK (price_basis IN (
                    'hammer', 'realised', 'premium_included', 'reserve',
                    'current_bid', 'estimate', 'unknown'
                )),
                CHECK (attribution IN ('stated', 'uncertain'))
            );

            CREATE INDEX IF NOT EXISTS market_listings_price_idx
                ON market_listings (price_status, currency, price_value);
            CREATE INDEX IF NOT EXISTS market_listings_source_idx
                ON market_listings (source, last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS market_listing_query_matches (
                listing_id BIGINT NOT NULL REFERENCES market_listings(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                first_run_id BIGINT NOT NULL REFERENCES market_runs(id),
                last_run_id BIGINT NOT NULL REFERENCES market_runs(id),
                best_rank INTEGER NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (listing_id, query_id)
            );

            CREATE INDEX IF NOT EXISTS market_listing_query_matches_query_idx
                ON market_listing_query_matches (query_id, listing_id);

            CREATE TABLE IF NOT EXISTS market_listing_reviews (
                listing_id BIGINT PRIMARY KEY REFERENCES market_listings(id) ON DELETE CASCADE,
                content_status TEXT NOT NULL DEFAULT 'unreviewed',
                use_status TEXT NOT NULL DEFAULT 'price_image',
                tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (content_status IN ('unreviewed', 'usable', 'unclear', 'unusable')),
                CHECK (use_status IN ('price_image', 'price_only', 'image_only', 'do_not_use'))
            );

            CREATE TABLE IF NOT EXISTS deal_runs (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'ebay_active',
                status TEXT NOT NULL DEFAULT 'queued',
                planned_tasks INTEGER NOT NULL,
                completed_tasks INTEGER NOT NULL DEFAULT 0,
                successful_tasks INTEGER NOT NULL DEFAULT 0,
                failed_tasks INTEGER NOT NULL DEFAULT 0,
                api_calls_used INTEGER NOT NULL DEFAULT 0,
                imported_results INTEGER NOT NULL DEFAULT 0,
                unique_listings INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (source IN ('ebay_active')),
                CHECK (status IN (
                    'queued', 'running', 'completed', 'completed_with_errors',
                    'cancel_requested', 'cancelled', 'failed'
                ))
            );

            CREATE TABLE IF NOT EXISTS deal_run_tasks (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES deal_runs(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                source TEXT NOT NULL DEFAULT 'ebay_active',
                status TEXT NOT NULL DEFAULT 'queued',
                result_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                api_calls INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                UNIQUE (run_id, query_id, source),
                CHECK (source IN ('ebay_active')),
                CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
            );

            CREATE INDEX IF NOT EXISTS deal_run_tasks_run_idx
                ON deal_run_tasks (run_id, status, id);

            CREATE TABLE IF NOT EXISTS deal_listings (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                price_value NUMERIC,
                price_raw TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                condition_text TEXT NOT NULL DEFAULT '',
                seller_account_type TEXT NOT NULL DEFAULT '',
                seller_name TEXT NOT NULL DEFAULT '',
                listing_end TEXT NOT NULL DEFAULT '',
                raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (source, source_item_id),
                CHECK (source IN ('ebay_active'))
            );

            CREATE INDEX IF NOT EXISTS deal_listings_price_idx
                ON deal_listings (currency, price_value);
            CREATE INDEX IF NOT EXISTS deal_listings_source_idx
                ON deal_listings (source, last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS deal_listing_query_matches (
                listing_id BIGINT NOT NULL REFERENCES deal_listings(id) ON DELETE CASCADE,
                query_id TEXT NOT NULL REFERENCES search_queries(id),
                first_run_id BIGINT NOT NULL REFERENCES deal_runs(id),
                last_run_id BIGINT NOT NULL REFERENCES deal_runs(id),
                best_rank INTEGER NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (listing_id, query_id)
            );

            CREATE INDEX IF NOT EXISTS deal_listing_query_matches_query_idx
                ON deal_listing_query_matches (query_id, listing_id);

            CREATE TABLE IF NOT EXISTS deal_listing_reviews (
                listing_id BIGINT PRIMARY KEY REFERENCES deal_listings(id) ON DELETE CASCADE,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (review_status IN ('unreviewed', 'candidate', 'checked', 'skip'))
            );

            CREATE TABLE IF NOT EXISTS image_features (
                listing_kind TEXT NOT NULL,
                listing_id BIGINT NOT NULL,
                image_url TEXT NOT NULL,
                feature_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                width INTEGER,
                height INTEGER,
                ahash TEXT NOT NULL DEFAULT '',
                dhash TEXT NOT NULL DEFAULT '',
                blockhash TEXT NOT NULL DEFAULT '',
                color_vector REAL[] NOT NULL DEFAULT ARRAY[]::REAL[],
                edge_vector REAL[] NOT NULL DEFAULT ARRAY[]::REAL[],
                error TEXT NOT NULL DEFAULT '',
                processed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (listing_kind, listing_id),
                CHECK (listing_kind IN ('deal', 'market')),
                CHECK (status IN ('queued', 'ok', 'failed'))
            );

            CREATE TABLE IF NOT EXISTS image_match_runs (
                id BIGSERIAL PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                planned_tasks INTEGER NOT NULL,
                completed_tasks INTEGER NOT NULL DEFAULT 0,
                successful_tasks INTEGER NOT NULL DEFAULT 0,
                failed_tasks INTEGER NOT NULL DEFAULT 0,
                analysed_images INTEGER NOT NULL DEFAULT 0,
                candidate_pairs INTEGER NOT NULL DEFAULT 0,
                matches_written INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (status IN (
                    'queued', 'running', 'completed', 'completed_with_errors',
                    'cancel_requested', 'cancelled', 'failed'
                ))
            );

            CREATE TABLE IF NOT EXISTS image_match_tasks (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES image_match_runs(id) ON DELETE CASCADE,
                deal_listing_id BIGINT NOT NULL REFERENCES deal_listings(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                analysed_images INTEGER NOT NULL DEFAULT 0,
                candidate_pairs INTEGER NOT NULL DEFAULT 0,
                matches_written INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                UNIQUE (run_id, deal_listing_id),
                CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
            );

            CREATE INDEX IF NOT EXISTS image_match_tasks_run_idx
                ON image_match_tasks (run_id, status, id);

            CREATE TABLE IF NOT EXISTS image_matches (
                id BIGSERIAL PRIMARY KEY,
                deal_listing_id BIGINT NOT NULL REFERENCES deal_listings(id) ON DELETE CASCADE,
                market_listing_id BIGINT NOT NULL REFERENCES market_listings(id) ON DELETE CASCADE,
                last_run_id BIGINT NOT NULL REFERENCES image_match_runs(id) ON DELETE CASCADE,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                visual_score REAL NOT NULL,
                title_score REAL NOT NULL,
                ahash_score REAL NOT NULL,
                dhash_score REAL NOT NULL,
                blockhash_score REAL NOT NULL,
                color_score REAL NOT NULL,
                edge_score REAL NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                note TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (deal_listing_id, market_listing_id),
                CHECK (rank BETWEEN 1 AND 5),
                CHECK (score >= 0 AND score <= 1),
                CHECK (review_status IN ('unreviewed', 'candidate', 'checked', 'skip'))
            );

            CREATE INDEX IF NOT EXISTS image_matches_review_idx
                ON image_matches (review_status, score DESC, updated_at DESC);
            CREATE INDEX IF NOT EXISTS image_matches_deal_idx
                ON image_matches (deal_listing_id, rank);

            ALTER TABLE reference_runs
                ADD COLUMN IF NOT EXISTS api_calls_used INTEGER NOT NULL DEFAULT 0;

            ALTER TABLE reference_run_queries
                ADD COLUMN IF NOT EXISTS serpapi_calls INTEGER NOT NULL DEFAULT 0;

            ALTER TABLE worker_status
                ADD COLUMN IF NOT EXISTS current_market_run_id BIGINT
                REFERENCES market_runs(id) ON DELETE SET NULL;

            ALTER TABLE worker_status
                ADD COLUMN IF NOT EXISTS current_deal_run_id BIGINT
                REFERENCES deal_runs(id) ON DELETE SET NULL;

            ALTER TABLE worker_status
                ADD COLUMN IF NOT EXISTS current_match_run_id BIGINT
                REFERENCES image_match_runs(id) ON DELETE SET NULL;

            ALTER TABLE market_runs
                ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'refresh';

            ALTER TABLE market_run_tasks
                ADD COLUMN IF NOT EXISTS start_page INTEGER NOT NULL DEFAULT 1;

            ALTER TABLE market_run_tasks
                ADD COLUMN IF NOT EXISTS page_count INTEGER NOT NULL DEFAULT 1;

            ALTER TABLE market_runs
                DROP CONSTRAINT IF EXISTS market_runs_kind_check;
            ALTER TABLE market_runs
                ADD CONSTRAINT market_runs_kind_check
                CHECK (kind IN ('refresh', 'backfill', 'source_pilot'));

            ALTER TABLE market_run_tasks
                DROP CONSTRAINT IF EXISTS market_run_tasks_source_check;
            ALTER TABLE market_run_tasks
                ADD CONSTRAINT market_run_tasks_source_check
                CHECK (source IN (
                    'auctionet', 'blocket', 'quittenbaum', 'lempertz', 'bruun_rasmussen',
                    'mehlis', 'van_ham', 'dorotheum', 'liveauctioneers', 'invaluable',
                    'christies', 'heritage'
                ));

            ALTER TABLE market_backfill_cursors
                DROP CONSTRAINT IF EXISTS market_backfill_cursors_source_check;
            ALTER TABLE market_backfill_cursors
                ADD CONSTRAINT market_backfill_cursors_source_check
                CHECK (source IN (
                    'auctionet', 'blocket', 'quittenbaum', 'lempertz', 'bruun_rasmussen',
                    'mehlis', 'van_ham', 'dorotheum', 'liveauctioneers', 'invaluable',
                    'christies', 'heritage'
                ));

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'market_run_tasks_start_page_check'
                      AND conrelid = 'market_run_tasks'::regclass
                ) THEN
                    ALTER TABLE market_run_tasks
                        ADD CONSTRAINT market_run_tasks_start_page_check
                        CHECK (start_page >= 1);
                END IF;
            END $$;

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'market_run_tasks_page_count_check'
                      AND conrelid = 'market_run_tasks'::regclass
                ) THEN
                    ALTER TABLE market_run_tasks
                        ADD CONSTRAINT market_run_tasks_page_count_check
                        CHECK (page_count >= 1);
                END IF;
            END $$;
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
