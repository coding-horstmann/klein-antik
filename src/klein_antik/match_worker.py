from __future__ import annotations

import logging
import math
import os
import re
import signal
import time
import unicodedata
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import env_float, env_int
from .db import connection, init_schema


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("klein_antik.image_matcher")
STOP = False
FEATURE_VERSION = 1
MAX_MATCHES_PER_DEAL = 5
MIN_MATCH_SCORE = 0.53
GENERIC_TOKENS = {
    "antique", "antik", "art", "deco", "design", "designobjekt", "figur", "glass",
    "glas", "kaufen", "keramik", "lampe", "metall", "neu", "porzellan", "schmuck",
    "silber", "signed", "signiert", "vase", "vintage",
}


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _normalized_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", normalized.lower())
        if token not in GENERIC_TOKENS and not token.isdigit()
    }


def _bit_string(values: list[bool]) -> str:
    return "".join("1" if value else "0" for value in values)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    length = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return max(0.0, min(1.0, dot / length)) if length else 0.0


def _normalized(values: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in values))
    return [round(value / length, 6) for value in values] if length else values


def _bit_similarity(left: str, right: str) -> float:
    if not left or len(left) != len(right):
        return 0.0
    return sum(a == b for a, b in zip(left, right)) / len(left)


def extract_features(payload: bytes) -> dict[str, Any]:
    """Return cheap, deterministic visual features for one primary listing image."""
    with Image.open(BytesIO(payload)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.load()
    width, height = image.size
    if width < 32 or height < 32:
        raise ValueError("Bild ist zu klein fuer den Bildabgleich.")

    gray16 = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    gray_values = list(gray16.getdata())
    average = sum(gray_values) / len(gray_values)
    ahash = _bit_string([value >= average for value in gray_values])

    difference = image.convert("L").resize((17, 16), Image.Resampling.LANCZOS)
    difference_values = list(difference.getdata())
    dhash = _bit_string(
        [
            difference_values[row * 17 + column] >= difference_values[row * 17 + column + 1]
            for row in range(16)
            for column in range(16)
        ]
    )

    block = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    block_values = list(block.getdata())
    block_average = sum(block_values) / len(block_values)
    blockhash = _bit_string([value >= block_average for value in block_values])

    histogram = image.resize((96, 96), Image.Resampling.LANCZOS).histogram()
    color: list[float] = []
    for channel in range(3):
        channel_histogram = histogram[channel * 256 : (channel + 1) * 256]
        for start in range(0, 256, 32):
            color.append(float(sum(channel_histogram[start : start + 32])))

    edge = image.convert("L").resize((96, 96), Image.Resampling.LANCZOS)
    pixels = list(edge.getdata())
    edge_histogram = [0.0] * 8
    for row in range(1, 95, 2):
        for column in range(1, 95, 2):
            index = row * 96 + column
            horizontal = pixels[index + 1] - pixels[index - 1]
            vertical = pixels[index + 96] - pixels[index - 96]
            magnitude = abs(horizontal) + abs(vertical)
            if magnitude:
                angle = (math.atan2(vertical, horizontal) + math.pi) / (2 * math.pi)
                edge_histogram[min(7, int(angle * 8))] += magnitude

    return {
        "width": width,
        "height": height,
        "ahash": ahash,
        "dhash": dhash,
        "blockhash": blockhash,
        "color_vector": _normalized(color),
        "edge_vector": _normalized(edge_histogram),
    }


def compare_features(
    deal: dict[str, Any], reference: dict[str, Any],
) -> dict[str, float]:
    ahash_score = _bit_similarity(str(deal["ahash"]), str(reference["ahash"]))
    dhash_score = _bit_similarity(str(deal["dhash"]), str(reference["dhash"]))
    blockhash_score = _bit_similarity(str(deal["blockhash"]), str(reference["blockhash"]))
    color_score = _cosine(list(deal["color_vector"]), list(reference["color_vector"]))
    edge_score = _cosine(list(deal["edge_vector"]), list(reference["edge_vector"]))
    visual_score = (
        0.46 * (0.40 * dhash_score + 0.35 * ahash_score + 0.25 * blockhash_score)
        + 0.34 * color_score
        + 0.20 * edge_score
    )
    deal_tokens = _normalized_tokens(str(deal["title"]))
    reference_tokens = _normalized_tokens(str(reference["title"]))
    title_score = (
        len(deal_tokens & reference_tokens) / len(deal_tokens | reference_tokens)
        if deal_tokens and reference_tokens
        else 0.0
    )
    score = 0.78 * visual_score + 0.22 * title_score
    return {
        "score": round(max(0.0, min(1.0, score)), 6),
        "visual_score": round(visual_score, 6),
        "title_score": round(title_score, 6),
        "ahash_score": round(ahash_score, 6),
        "dhash_score": round(dhash_score, 6),
        "blockhash_score": round(blockhash_score, 6),
        "color_score": round(color_score, 6),
        "edge_score": round(edge_score, 6),
    }


def heartbeat(state: str, *, current_run_id: int | None = None, message: str = "") -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (
                name, state, api_key_configured, current_run_id,
                current_market_run_id, current_deal_run_id, current_match_run_id,
                message, last_seen_at
            )
            VALUES ('image-matcher', %s, TRUE, NULL, NULL, NULL, %s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                state = EXCLUDED.state,
                api_key_configured = TRUE,
                current_run_id = NULL,
                current_market_run_id = NULL,
                current_deal_run_id = NULL,
                current_match_run_id = EXCLUDED.current_match_run_id,
                message = EXCLUDED.message,
                last_seen_at = now()
            """,
            (state, current_run_id, message[:500]),
        )


def claim_run() -> dict[str, Any] | None:
    with connection() as conn:
        run = conn.execute(
            """
            SELECT id, planned_tasks
            FROM image_match_runs
            WHERE status IN ('queued', 'running')
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not run:
            return None
        conn.execute(
            """
            UPDATE image_match_tasks
            SET status = 'queued', started_at = NULL
            WHERE run_id = %s AND status = 'running'
            """,
            (run["id"],),
        )
        conn.execute(
            """
            UPDATE image_match_runs
            SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s
            """,
            (run["id"],),
        )
        return dict(run)


def next_task(run_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status FROM image_match_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            return None
        if run["status"] == "cancel_requested":
            conn.execute(
                """
                UPDATE image_match_tasks
                SET status = 'cancelled', completed_at = now()
                WHERE run_id = %s AND status = 'queued'
                """,
                (run_id,),
            )
            conn.execute(
                """
                UPDATE image_match_runs
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (run_id,),
            )
            return None
        task = conn.execute(
            """
            SELECT task.id, task.deal_listing_id, deal.title, deal.image_url
            FROM image_match_tasks task
            JOIN deal_listings deal ON deal.id = task.deal_listing_id
            WHERE task.run_id = %s AND task.status = 'queued'
            ORDER BY task.id
            FOR UPDATE OF task SKIP LOCKED
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if not task:
            return None
        conn.execute(
            """
            UPDATE image_match_tasks
            SET status = 'running', started_at = now(), error = ''
            WHERE id = %s
            """,
            (task["id"],),
        )
        return dict(task)


def _fetch_feature(
    session: requests.Session,
    listing_kind: str,
    listing_id: int,
    image_url: str,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, bool]:
    with connection() as conn:
        existing = conn.execute(
            """
            SELECT * FROM image_features
            WHERE listing_kind = %s AND listing_id = %s
            """,
            (listing_kind, listing_id),
        ).fetchone()
    if (
        existing
        and existing["status"] == "ok"
        and existing["image_url"] == image_url
        and int(existing["feature_version"]) == FEATURE_VERSION
    ):
        return dict(existing), False

    try:
        response = session.get(image_url, timeout=timeout)
        response.raise_for_status()
        if len(response.content) > 16_000_000:
            raise ValueError("Bilddatei ist groesser als 16 MB.")
        feature = extract_features(response.content)
    except (requests.RequestException, UnidentifiedImageError, OSError, ValueError) as exc:
        with connection() as conn:
            conn.execute(
                """
                INSERT INTO image_features (
                    listing_kind, listing_id, image_url, feature_version, status, error, updated_at
                ) VALUES (%s, %s, %s, %s, 'failed', %s, now())
                ON CONFLICT (listing_kind, listing_id) DO UPDATE SET
                    image_url = EXCLUDED.image_url,
                    feature_version = EXCLUDED.feature_version,
                    status = 'failed',
                    error = EXCLUDED.error,
                    updated_at = now()
                """,
                (listing_kind, listing_id, image_url, FEATURE_VERSION, str(exc)[:1000]),
            )
        return None, True

    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO image_features (
                listing_kind, listing_id, image_url, feature_version, status,
                width, height, ahash, dhash, blockhash, color_vector, edge_vector,
                error, processed_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, 'ok',
                %s, %s, %s, %s, %s, %s, %s,
                '', now(), now()
            )
            ON CONFLICT (listing_kind, listing_id) DO UPDATE SET
                image_url = EXCLUDED.image_url,
                feature_version = EXCLUDED.feature_version,
                status = 'ok',
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                ahash = EXCLUDED.ahash,
                dhash = EXCLUDED.dhash,
                blockhash = EXCLUDED.blockhash,
                color_vector = EXCLUDED.color_vector,
                edge_vector = EXCLUDED.edge_vector,
                error = '',
                processed_at = now(),
                updated_at = now()
            RETURNING *
            """,
            (
                listing_kind,
                listing_id,
                image_url,
                FEATURE_VERSION,
                feature["width"],
                feature["height"],
                feature["ahash"],
                feature["dhash"],
                feature["blockhash"],
                feature["color_vector"],
                feature["edge_vector"],
            ),
        ).fetchone()
    return dict(row), True


def candidate_references(deal_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT market.*
            FROM market_listings market
            JOIN market_listing_query_matches market_query
                ON market_query.listing_id = market.id
            JOIN search_queries market_search ON market_search.id = market_query.query_id
            JOIN deal_listing_query_matches deal_query
                ON deal_query.listing_id = %s
            JOIN search_queries deal_search ON deal_search.id = deal_query.query_id
            LEFT JOIN market_listing_reviews review ON review.listing_id = market.id
            WHERE market_search.category = deal_search.category
              AND market.image_url <> ''
              AND market.price_value IS NOT NULL
              AND COALESCE(review.content_status, 'unreviewed') <> 'unusable'
              AND COALESCE(review.use_status, 'price_image') <> 'do_not_use'
            """,
            (deal_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def complete_task(
    run_id: int,
    task_id: int,
    deal_listing_id: int,
    matches: list[tuple[dict[str, Any], dict[str, float]]],
    analysed_images: int,
    candidate_pairs: int,
) -> None:
    with connection() as conn:
        reference_ids = [int(reference["id"]) for reference, _scores in matches]
        if reference_ids:
            conn.execute(
                """
                DELETE FROM image_matches
                WHERE deal_listing_id = %s
                  AND market_listing_id <> ALL(%s)
                """,
                (deal_listing_id, reference_ids),
            )
        else:
            conn.execute("DELETE FROM image_matches WHERE deal_listing_id = %s", (deal_listing_id,))
        for rank, (reference, scores) in enumerate(matches, 1):
            conn.execute(
                """
                INSERT INTO image_matches (
                    deal_listing_id, market_listing_id, last_run_id, rank,
                    score, visual_score, title_score, ahash_score, dhash_score,
                    blockhash_score, color_score, edge_score
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (deal_listing_id, market_listing_id) DO UPDATE SET
                    last_run_id = EXCLUDED.last_run_id,
                    rank = EXCLUDED.rank,
                    score = EXCLUDED.score,
                    visual_score = EXCLUDED.visual_score,
                    title_score = EXCLUDED.title_score,
                    ahash_score = EXCLUDED.ahash_score,
                    dhash_score = EXCLUDED.dhash_score,
                    blockhash_score = EXCLUDED.blockhash_score,
                    color_score = EXCLUDED.color_score,
                    edge_score = EXCLUDED.edge_score,
                    updated_at = now()
                """,
                (
                    deal_listing_id,
                    reference["id"],
                    run_id,
                    rank,
                    scores["score"],
                    scores["visual_score"],
                    scores["title_score"],
                    scores["ahash_score"],
                    scores["dhash_score"],
                    scores["blockhash_score"],
                    scores["color_score"],
                    scores["edge_score"],
                ),
            )
        conn.execute(
            """
            UPDATE image_match_tasks
            SET status = 'completed', analysed_images = %s, candidate_pairs = %s,
                matches_written = %s, completed_at = now()
            WHERE id = %s
            """,
            (analysed_images, candidate_pairs, len(matches), task_id),
        )
        refresh_run_stats(conn, run_id)


def fail_task(run_id: int, task_id: int, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE image_match_tasks
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
            COALESCE(SUM(analysed_images), 0) AS analysed_images,
            COALESCE(SUM(candidate_pairs), 0) AS candidate_pairs,
            COALESCE(SUM(matches_written), 0) AS matches_written
        FROM image_match_tasks
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    conn.execute(
        """
        UPDATE image_match_runs
        SET completed_tasks = %s,
            successful_tasks = %s,
            failed_tasks = %s,
            analysed_images = %s,
            candidate_pairs = %s,
            matches_written = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            int(counts["completed"]),
            int(counts["successful"]),
            int(counts["failed"]),
            int(counts["analysed_images"]),
            int(counts["candidate_pairs"]),
            int(counts["matches_written"]),
            run_id,
        ),
    )


def finish_run(run_id: int) -> None:
    with connection() as conn:
        run = conn.execute(
            "SELECT status, failed_tasks FROM image_match_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if not run or run["status"] in {"cancelled", "failed"}:
            return
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM image_match_tasks
            WHERE run_id = %s AND status IN ('queued', 'running')
            """,
            (run_id,),
        ).fetchone()
        if int(pending["count"]):
            return
        status = "completed_with_errors" if int(run["failed_tasks"]) else "completed"
        conn.execute(
            """
            UPDATE image_match_runs
            SET status = %s, completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (status, run_id),
        )


def process_run(run: dict[str, Any], *, timeout: float, interval: float) -> None:
    run_id = int(run["id"])
    session = requests.Session()
    session.headers.update({"User-Agent": "klein-antik-image-matcher/1.0"})
    LOG.info("Starte Bildabgleich %s mit %s Deals", run_id, run["planned_tasks"])
    try:
        while not STOP:
            task = next_task(run_id)
            if not task:
                finish_run(run_id)
                return
            heartbeat("running", current_run_id=run_id, message=f"Bildabgleich: {task['title']}")
            try:
                analysed_images = 0
                deal_feature, fetched = _fetch_feature(
                    session,
                    "deal",
                    int(task["deal_listing_id"]),
                    str(task["image_url"]),
                    timeout=timeout,
                )
                analysed_images += int(fetched)
                if not deal_feature:
                    raise RuntimeError("Dealbild konnte nicht analysiert werden.")
                candidates = candidate_references(int(task["deal_listing_id"]))
                scored: list[tuple[dict[str, Any], dict[str, float]]] = []
                for reference in candidates:
                    if STOP:
                        return
                    feature, fetched = _fetch_feature(
                        session,
                        "market",
                        int(reference["id"]),
                        str(reference["image_url"]),
                        timeout=timeout,
                    )
                    analysed_images += int(fetched)
                    if feature:
                        scored.append((reference, compare_features({**deal_feature, "title": task["title"]}, {**feature, "title": reference["title"]})))
                    if fetched and interval:
                        time.sleep(interval)
                scored.sort(key=lambda item: item[1]["score"], reverse=True)
                accepted = [item for item in scored if item[1]["score"] >= MIN_MATCH_SCORE][:MAX_MATCHES_PER_DEAL]
                complete_task(
                    run_id,
                    int(task["id"]),
                    int(task["deal_listing_id"]),
                    accepted,
                    analysed_images,
                    len(scored),
                )
                LOG.info("Bildabgleich %s: %s Kandidaten, %s Treffer", task["deal_listing_id"], len(scored), len(accepted))
            except Exception as exc:  # One broken image must not stop the remaining deals.
                LOG.exception("Bildabgleich fehlgeschlagen: %s", task["deal_listing_id"])
                fail_task(run_id, int(task["id"]), f"{type(exc).__name__}: {exc}")
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
    poll_seconds = max(2, env_int("MATCHER_POLL_SECONDS", 5))
    request_timeout = max(5, env_int("IMAGE_REQUEST_TIMEOUT_SECONDS", 20))
    request_interval = max(0.0, env_float("IMAGE_REQUEST_INTERVAL_SECONDS", 0.08))
    while not STOP:
        run = claim_run()
        if run:
            process_run(run, timeout=float(request_timeout), interval=request_interval)
            continue
        heartbeat("idle", message="Bereit fuer Bildabgleich")
        time.sleep(poll_seconds)
    heartbeat("stopped")


if __name__ == "__main__":
    main()
