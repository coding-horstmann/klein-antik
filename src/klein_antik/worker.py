from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

from psycopg.types.json import Jsonb

from .config import env_float, env_int
from .db import connection, init_schema
from .market_sources import SOURCE_LABELS, build_session, collect


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("klein_antik.market_worker")
STOP = False


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def heartbeat(
    state: str,
    *,
    current_run_id: int | None = None,
    message: str = "",
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (
                name, state, api_key_configured, current_run_id,
                current_market_run_id, message, last_seen_at
            )
            VALUES ('market-importer', %s, TRUE, NULL, %s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                state = EXCLUDED.state,
                api_key_configured = TRUE,
                current_run_id = NULL,
                current_market_run_id = EXCLUDED.current_market_run_id,
                message = EXCLUDED.message,
                last_seen_at = now()
            """,
            (state, current_run_id, message[:500]),
        )


def claim_run() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT id, planned_tasks
            FROM market_runs
            WHERE status IN ('queued', 'running')
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        if int(row["planned_tasks"]) > 350:
            conn.execute(
                """
                UPDATE market_runs
                SET status = 'failed',
                    error = 'Lauf ueberschreitet die feste Obergrenze von 350 Aufgaben.',
                    completed_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )
            return None
        conn.execute(
            """
            UPDATE market_run_tasks
            SET status = 'queued', started_at = NULL
            WHERE run_id = %s AND status = 'running'
            """,
            (row["id"],),
        )
        conn.execute(
            """
            UPDATE market_runs
            SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def next_task(run_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status FROM market_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            return None
        if run["status"] == "cancel_requested":
            conn.execute(
                """
                UPDATE market_run_tasks
                SET status = 'cancelled', completed_at = now()
                WHERE run_id = %s AND status = 'queued'
                """,
                (run_id,),
            )
            conn.execute(
                """
                UPDATE market_runs
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (run_id,),
            )
            return None

        row = conn.execute(
            """
            SELECT
                task.id,
                task.query_id,
                task.source,
                q.query_text,
                q.category
            FROM market_run_tasks task
            JOIN search_queries q ON q.id = task.query_id
            WHERE task.run_id = %s AND task.status = 'queued'
            ORDER BY q.position, task.id
            FOR UPDATE OF task SKIP LOCKED
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE market_run_tasks
            SET status = 'running', started_at = now(), error = ''
            WHERE id = %s
            """,
            (row["id"],),
        )
        return dict(row)


def save_results(
    run_id: int,
    task: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    unique_ids: set[str] = set()
    with connection() as conn:
        for rank, item in enumerate(results, start=1):
            unique_ids.add(item["source_item_id"])
            listing = conn.execute(
                """
                INSERT INTO market_listings (
                    source, source_item_id, title, url, image_url,
                    price_status, price_value, price_raw, currency, price_basis,
                    estimate_raw, sale_date, attribution, raw_result
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (source, source_item_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    url = EXCLUDED.url,
                    image_url = CASE
                        WHEN EXCLUDED.image_url <> '' THEN EXCLUDED.image_url
                        ELSE market_listings.image_url
                    END,
                    price_status = EXCLUDED.price_status,
                    price_value = EXCLUDED.price_value,
                    price_raw = EXCLUDED.price_raw,
                    currency = EXCLUDED.currency,
                    price_basis = EXCLUDED.price_basis,
                    estimate_raw = EXCLUDED.estimate_raw,
                    sale_date = EXCLUDED.sale_date,
                    attribution = EXCLUDED.attribution,
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
                    item["price_status"],
                    item["price_value"],
                    item["price_raw"],
                    item["currency"],
                    item["price_basis"],
                    item["estimate_raw"],
                    item["sale_date"],
                    item["attribution"],
                    Jsonb(item["raw_result"]),
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO market_listing_query_matches (
                    listing_id, query_id, first_run_id, last_run_id, best_rank
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (listing_id, query_id) DO UPDATE SET
                    last_run_id = EXCLUDED.last_run_id,
                    best_rank = LEAST(
                        market_listing_query_matches.best_rank,
                        EXCLUDED.best_rank
                    ),
                    last_seen_at = now()
                """,
                (listing["id"], task["query_id"], run_id, run_id, rank),
            )

        conn.execute(
            """
            UPDATE market_run_tasks
            SET status = 'completed',
                result_count = %s,
                unique_count = %s,
                completed_at = now()
            WHERE id = %s
            """,
            (len(results), len(unique_ids), task["id"]),
        )
        refresh_run_stats(conn, run_id)


def fail_task(run_id: int, task_id: int, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE market_run_tasks
            SET status = 'failed', error = %s, completed_at = now()
            WHERE id = %s
            """,
            (error[:1000], task_id),
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
        FROM market_run_tasks
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    unique_row = conn.execute(
        """
        SELECT COUNT(DISTINCT listing_id) AS count
        FROM market_listing_query_matches
        WHERE last_run_id = %s OR first_run_id = %s
        """,
        (run_id, run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE market_runs
        SET completed_tasks = %s,
            successful_tasks = %s,
            failed_tasks = %s,
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
            "SELECT status, failed_tasks FROM market_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if not run or run["status"] in {"cancelled", "failed"}:
            return
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM market_run_tasks
            WHERE run_id = %s AND status IN ('queued', 'running')
            """,
            (run_id,),
        ).fetchone()
        if int(pending["count"]) > 0:
            return
        final_status = "completed_with_errors" if int(run["failed_tasks"]) else "completed"
        conn.execute(
            """
            UPDATE market_runs
            SET status = %s, completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (final_status, run_id),
        )


def process_run(run: dict[str, Any], interval: float, result_limit: int) -> None:
    run_id = int(run["id"])
    LOG.info(
        "Starte Marktpreislauf %s mit %s Quellenaufgaben",
        run_id,
        run["planned_tasks"],
    )
    session = build_session()
    try:
        while not STOP:
            task = next_task(run_id)
            if not task:
                finish_run(run_id)
                return
            source_label = SOURCE_LABELS.get(task["source"], task["source"])
            heartbeat(
                "running",
                current_run_id=run_id,
                message=f"{source_label}: {task['query_text']}",
            )
            try:
                results = collect(
                    task["source"],
                    task["query_text"],
                    limit=result_limit,
                    session=session,
                )
                save_results(run_id, task, results)
                LOG.info(
                    "%s abgeschlossen: %s (%s Treffer)",
                    source_label,
                    task["query_text"],
                    len(results),
                )
            except Exception as exc:  # one source failure must not stop the complete run
                error = f"{type(exc).__name__}: {exc}"
                LOG.exception("%s fehlgeschlagen: %s", source_label, task["query_text"])
                fail_task(run_id, int(task["id"]), error)
            if not STOP:
                time.sleep(interval)
    finally:
        session.close()


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
    interval = max(1.0, env_float("MARKET_REQUEST_INTERVAL_SECONDS", 2.0))
    result_limit = max(5, min(50, env_int("MARKET_RESULTS_PER_SOURCE", 30)))

    while not STOP:
        run = claim_run()
        if not run:
            heartbeat("idle", message="Bereit fuer Marktpreislauf")
            time.sleep(poll_seconds)
            continue
        process_run(run, interval, result_limit)

    heartbeat("stopped")


if __name__ == "__main__":
    main()
