from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Any

from psycopg.types.json import Jsonb

from .catalog import EXPECTED_QUERY_COUNT
from .config import env_float, env_int, serpapi_key
from .db import connection, init_schema
from .serpapi import SerpApiError, normalized_result, search_sold


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("klein_antik.worker")
STOP = False


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def heartbeat(
    state: str,
    *,
    key_configured: bool,
    current_run_id: int | None = None,
    message: str = "",
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (
                name, state, api_key_configured, current_run_id, message, last_seen_at
            )
            VALUES ('reference-importer', %s, %s, %s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                state = EXCLUDED.state,
                api_key_configured = EXCLUDED.api_key_configured,
                current_run_id = EXCLUDED.current_run_id,
                message = EXCLUDED.message,
                last_seen_at = now()
            """,
            (state, key_configured, current_run_id, message[:500]),
        )


def claim_run() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, planned_calls
            FROM reference_runs
            WHERE status = 'queued'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        if int(row["planned_calls"]) > EXPECTED_QUERY_COUNT:
            conn.execute(
                """
                UPDATE reference_runs
                SET status = 'failed',
                    error = 'Lauf ueberschreitet das feste Pilotbudget.',
                    completed_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )
            return None
        conn.execute(
            """
            UPDATE reference_runs
            SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def next_query(run_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status FROM reference_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            return None
        if run["status"] == "cancel_requested":
            conn.execute(
                """
                UPDATE reference_run_queries
                SET status = 'cancelled', completed_at = now()
                WHERE run_id = %s AND status = 'queued'
                """,
                (run_id,),
            )
            conn.execute(
                """
                UPDATE reference_runs
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (run_id,),
            )
            return None

        row = conn.execute(
            """
            SELECT rq.id, rq.query_id, q.query_text, q.ebay_domain, q.ebay_category_id
            FROM reference_run_queries rq
            JOIN search_queries q ON q.id = rq.query_id
            WHERE rq.run_id = %s AND rq.status = 'queued'
            ORDER BY q.position
            FOR UPDATE OF rq SKIP LOCKED
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE reference_run_queries
            SET status = 'running', started_at = now(), error = ''
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def save_result(run_id: int, run_query: dict[str, Any], data: dict[str, Any]) -> None:
    results = data.get("organic_results") or []
    if not isinstance(results, list):
        results = []
    search_metadata = data.get("search_metadata") or {}
    search_information = data.get("search_information") or {}
    reported_total = search_information.get("total_results")
    unique_ids: set[str] = set()

    with connection() as conn:
        for rank, raw in enumerate(results, start=1):
            if not isinstance(raw, dict):
                continue
            item = normalized_result(raw)
            unique_ids.add(item["product_id"])
            listing = conn.execute(
                """
                INSERT INTO reference_listings (
                    product_id, title, url, image_url, price_value, price_raw, currency,
                    condition_text, sold_date, shipping_text, seller, raw_result
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (product_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    url = CASE WHEN EXCLUDED.url <> '' THEN EXCLUDED.url ELSE reference_listings.url END,
                    image_url = CASE
                        WHEN EXCLUDED.image_url <> '' THEN EXCLUDED.image_url
                        ELSE reference_listings.image_url
                    END,
                    price_value = COALESCE(EXCLUDED.price_value, reference_listings.price_value),
                    price_raw = CASE
                        WHEN EXCLUDED.price_raw <> '' THEN EXCLUDED.price_raw
                        ELSE reference_listings.price_raw
                    END,
                    currency = CASE
                        WHEN EXCLUDED.currency <> '' THEN EXCLUDED.currency
                        ELSE reference_listings.currency
                    END,
                    condition_text = EXCLUDED.condition_text,
                    sold_date = EXCLUDED.sold_date,
                    shipping_text = EXCLUDED.shipping_text,
                    seller = EXCLUDED.seller,
                    raw_result = EXCLUDED.raw_result,
                    last_seen_at = now()
                RETURNING id
                """,
                (
                    item["product_id"],
                    item["title"],
                    item["url"],
                    item["image_url"],
                    item["price_value"],
                    item["price_raw"],
                    item["currency"],
                    item["condition_text"],
                    item["sold_date"],
                    item["shipping_text"],
                    Jsonb(item["seller"]),
                    Jsonb(item["raw_result"]),
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO listing_query_matches (
                    listing_id, query_id, first_run_id, last_run_id, best_rank
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (listing_id, query_id) DO UPDATE SET
                    last_run_id = EXCLUDED.last_run_id,
                    best_rank = LEAST(listing_query_matches.best_rank, EXCLUDED.best_rank),
                    last_seen_at = now()
                """,
                (listing["id"], run_query["query_id"], run_id, run_id, rank),
            )

        conn.execute(
            """
            UPDATE reference_run_queries
            SET status = 'completed',
                result_count = %s,
                unique_count = %s,
                reported_total_results = %s,
                serpapi_search_id = %s,
                raw_response = %s,
                completed_at = now()
            WHERE id = %s
            """,
            (
                len(results),
                len(unique_ids),
                reported_total if isinstance(reported_total, int) else None,
                str(search_metadata.get("id") or ""),
                Jsonb(data),
                run_query["id"],
            ),
        )
        refresh_run_stats(conn, run_id)


def fail_query(run_id: int, run_query_id: int, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE reference_run_queries
            SET status = 'failed', error = %s, completed_at = now()
            WHERE id = %s
            """,
            (error[:1000], run_query_id),
        )
        refresh_run_stats(conn, run_id)


def refresh_run_stats(conn: Any, run_id: int) -> None:
    counts = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) AS completed,
            COUNT(*) FILTER (WHERE status = 'completed') AS successful,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COALESCE(SUM(result_count), 0) AS imported
        FROM reference_run_queries
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    unique_row = conn.execute(
        """
        SELECT COUNT(DISTINCT listing_id) AS count
        FROM listing_query_matches
        WHERE last_run_id = %s OR first_run_id = %s
        """,
        (run_id, run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE reference_runs
        SET completed_calls = %s,
            successful_calls = %s,
            failed_calls = %s,
            imported_results = %s,
            unique_listings = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            int(counts["completed"]),
            int(counts["successful"]),
            int(counts["failed"]),
            int(counts["imported"]),
            int(unique_row["count"]),
            run_id,
        ),
    )


def finish_run(run_id: int) -> None:
    with connection() as conn:
        run = conn.execute(
            """
            SELECT status, failed_calls
            FROM reference_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
        if not run or run["status"] in {"cancelled", "failed"}:
            return
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reference_run_queries
            WHERE run_id = %s AND status IN ('queued', 'running')
            """,
            (run_id,),
        ).fetchone()
        if int(pending["count"]) > 0:
            return
        final_status = "completed_with_errors" if int(run["failed_calls"]) else "completed"
        conn.execute(
            """
            UPDATE reference_runs
            SET status = %s, completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (final_status, run_id),
        )


def process_run(run: dict[str, Any], key: str, interval: float) -> None:
    run_id = int(run["id"])
    LOG.info("Starte Referenzlauf %s mit %s geplanten Suchen", run_id, run["planned_calls"])
    while not STOP:
        run_query = next_query(run_id)
        if not run_query:
            finish_run(run_id)
            return
        heartbeat(
            "running",
            key_configured=True,
            current_run_id=run_id,
            message=run_query["query_text"],
        )
        try:
            data = search_sold(
                api_key=key,
                query=run_query["query_text"],
                ebay_domain=run_query["ebay_domain"],
                category_id=run_query.get("ebay_category_id"),
            )
            save_result(run_id, run_query, data)
            LOG.info("Suche abgeschlossen: %s", run_query["query_text"])
        except Exception as exc:  # a failed query is recorded and the run continues
            error = f"{type(exc).__name__}: {exc}"
            LOG.exception("Suche fehlgeschlagen: %s", run_query["query_text"])
            fail_query(run_id, int(run_query["id"]), error)
            if isinstance(exc, SerpApiError) and any(
                token in str(exc).lower() for token in ("credit", "quota", "run out", "limit")
            ):
                with connection() as conn:
                    conn.execute(
                        """
                        UPDATE reference_runs
                        SET status = 'blocked', error = %s, updated_at = now()
                        WHERE id = %s
                        """,
                        (error[:1000], run_id),
                    )
                return
        if not STOP:
            time.sleep(interval)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    for attempt in range(20):
        try:
            init_schema()
            break
        except Exception:
            if attempt == 19:
                raise
            LOG.warning("Postgres ist noch nicht bereit; neuer Versuch in 2 Sekunden.")
            time.sleep(2)
    poll_seconds = max(2, env_int("WORKER_POLL_SECONDS", 5))
    interval = max(72.0, env_float("SERPAPI_REQUEST_INTERVAL_SECONDS", 75.0))
    max_searches = env_int("MAX_SEARCHES_PER_RUN", EXPECTED_QUERY_COUNT)
    if max_searches != EXPECTED_QUERY_COUNT:
        raise RuntimeError(
            f"MAX_SEARCHES_PER_RUN muss fuer den Pilot {EXPECTED_QUERY_COUNT} sein."
        )

    while not STOP:
        key = serpapi_key()
        if not key:
            heartbeat(
                "waiting_for_key",
                key_configured=False,
                message="SERPAPI_API_KEY_PRIMARY fehlt",
            )
            time.sleep(poll_seconds)
            continue
        run = claim_run()
        if not run:
            heartbeat("idle", key_configured=True)
            time.sleep(poll_seconds)
            continue
        process_run(run, key, interval)

    heartbeat("stopped", key_configured=bool(serpapi_key()))


if __name__ == "__main__":
    main()
