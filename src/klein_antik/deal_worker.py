from __future__ import annotations

import logging
import time
from typing import Any, Callable

from psycopg.types.json import Jsonb

from .db import connection
from .ebay_active import collect


LOG = logging.getLogger("klein_antik.deal_worker")


def claim_run() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, planned_tasks
            FROM deal_runs
            WHERE status IN ('queued', 'running')
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        if int(row["planned_tasks"]) > 110:
            conn.execute(
                """
                UPDATE deal_runs
                SET status = 'failed',
                    error = 'Der eBay-Pilot ist auf 110 Suchabfragen begrenzt.',
                    completed_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )
            return None
        conn.execute(
            """
            UPDATE deal_run_tasks
            SET status = 'queued', started_at = NULL
            WHERE run_id = %s AND status = 'running'
            """,
            (row["id"],),
        )
        conn.execute(
            """
            UPDATE deal_runs
            SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def process_run(
    run: dict[str, Any],
    *,
    interval: float,
    result_limit: int,
    heartbeat: Callable[..., None],
    should_stop: Callable[[], bool],
) -> None:
    run_id = int(run["id"])
    LOG.info("Starte eBay-Deal-Lauf %s mit %s Suchanfragen", run_id, run["planned_tasks"])
    while not should_stop():
        task = _next_task(run_id)
        if not task:
            _finish_run(run_id)
            return
        heartbeat(
            "running",
            current_deal_run_id=run_id,
            message=f"eBay DE: {task['query_text']}",
        )
        try:
            results = collect(
                task["query_text"],
                task.get("ebay_category_id"),
                limit=result_limit,
            )
            _save_results(run_id, task, results)
            LOG.info("eBay DE abgeschlossen: %s (%s Treffer)", task["query_text"], len(results))
        except Exception as exc:  # A failed keyword must not stop the complete pilot.
            error = f"{type(exc).__name__}: {exc}"
            LOG.exception("eBay DE fehlgeschlagen: %s", task["query_text"])
            _fail_task(run_id, int(task["id"]), error)
        if not should_stop():
            time.sleep(interval)


def _next_task(run_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status FROM deal_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            return None
        if run["status"] == "cancel_requested":
            conn.execute(
                """
                UPDATE deal_run_tasks
                SET status = 'cancelled', completed_at = now()
                WHERE run_id = %s AND status = 'queued'
                """,
                (run_id,),
            )
            conn.execute(
                """
                UPDATE deal_runs
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (run_id,),
            )
            return None
        row = conn.execute(
            """
            SELECT task.id, task.query_id, task.source, q.query_text, q.ebay_category_id
            FROM deal_run_tasks task
            JOIN search_queries q ON q.id = task.query_id
            WHERE task.run_id = %s AND task.status = 'queued'
            ORDER BY task.id
            FOR UPDATE OF task SKIP LOCKED
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE deal_run_tasks
            SET status = 'running', started_at = now(), error = '', api_calls = 0
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def _save_results(run_id: int, task: dict[str, Any], results: list[dict[str, Any]]) -> None:
    unique_ids: set[str] = set()
    with connection() as conn:
        for rank, item in enumerate(results, start=1):
            unique_ids.add(item["source_item_id"])
            listing = conn.execute(
                """
                INSERT INTO deal_listings (
                    source, source_item_id, title, url, image_url,
                    price_value, price_raw, currency, condition_text,
                    seller_account_type, seller_name, listing_end, raw_result
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (source, source_item_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    url = EXCLUDED.url,
                    image_url = CASE
                        WHEN EXCLUDED.image_url <> '' THEN EXCLUDED.image_url
                        ELSE deal_listings.image_url
                    END,
                    price_value = EXCLUDED.price_value,
                    price_raw = EXCLUDED.price_raw,
                    currency = EXCLUDED.currency,
                    condition_text = EXCLUDED.condition_text,
                    seller_account_type = EXCLUDED.seller_account_type,
                    seller_name = EXCLUDED.seller_name,
                    listing_end = EXCLUDED.listing_end,
                    raw_result = EXCLUDED.raw_result,
                    last_seen_at = now()
                RETURNING id
                """,
                (
                    item["source"],
                    item["source_item_id"],
                    item["title"],
                    item["url"],
                    item["image_url"],
                    item["price_value"],
                    item["price_raw"],
                    item["currency"],
                    item["condition_text"],
                    item["seller_account_type"],
                    item["seller_name"],
                    item["listing_end"],
                    Jsonb(item["raw_result"]),
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO deal_listing_query_matches (
                    listing_id, query_id, first_run_id, last_run_id, best_rank
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (listing_id, query_id) DO UPDATE SET
                    last_run_id = EXCLUDED.last_run_id,
                    best_rank = LEAST(
                        deal_listing_query_matches.best_rank,
                        EXCLUDED.best_rank
                    ),
                    last_seen_at = now()
                """,
                (listing["id"], task["query_id"], run_id, run_id, rank),
            )
        conn.execute(
            """
            UPDATE deal_run_tasks
            SET status = 'completed', result_count = %s, unique_count = %s,
                api_calls = 1, completed_at = now()
            WHERE id = %s
            """,
            (len(results), len(unique_ids), task["id"]),
        )
        _refresh_run_stats(conn, run_id)


def _fail_task(run_id: int, task_id: int, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE deal_run_tasks
            SET status = 'failed', error = %s, api_calls = 1, completed_at = now()
            WHERE id = %s
            """,
            (error[:1000], task_id),
        )
        _refresh_run_stats(conn, run_id)


def _refresh_run_stats(conn: Any, run_id: int) -> None:
    counts = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) AS completed,
            COUNT(*) FILTER (WHERE status = 'completed') AS successful,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COALESCE(SUM(api_calls), 0) AS api_calls,
            COALESCE(SUM(result_count), 0) AS imported
        FROM deal_run_tasks
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    unique_row = conn.execute(
        """
        SELECT COUNT(DISTINCT listing_id) AS count
        FROM deal_listing_query_matches
        WHERE last_run_id = %s OR first_run_id = %s
        """,
        (run_id, run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE deal_runs
        SET completed_tasks = %s,
            successful_tasks = %s,
            failed_tasks = %s,
            api_calls_used = %s,
            imported_results = %s,
            unique_listings = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            int(counts["completed"]),
            int(counts["successful"]),
            int(counts["failed"]),
            int(counts["api_calls"]),
            int(counts["imported"]),
            int(unique_row["count"]),
            run_id,
        ),
    )


def _finish_run(run_id: int) -> None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status, failed_tasks FROM deal_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if not run or run["status"] in {"cancelled", "failed"}:
            return
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM deal_run_tasks
            WHERE run_id = %s AND status IN ('queued', 'running')
            """,
            (run_id,),
        ).fetchone()
        if int(pending["count"]) > 0:
            return
        final_status = "completed_with_errors" if int(run["failed_tasks"]) else "completed"
        conn.execute(
            """
            UPDATE deal_runs
            SET status = %s, completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (final_status, run_id),
        )
