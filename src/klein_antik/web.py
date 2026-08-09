from __future__ import annotations

import hmac
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, Callable

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .catalog import EXPECTED_QUERY_COUNT, category_options, load_queries
from .db import connection, init_schema
from .ebay_active import importer_ready
from .market_sources import SOURCE_LABELS, sources_for_category


CONTENT_STATUSES = {
    "unreviewed": "Ungeprüft",
    "usable": "Brauchbar",
    "unclear": "Unklar",
    "unusable": "Unbrauchbar",
}
USE_STATUSES = {
    "price_image": "Preis und Bild",
    "price_only": "Nur Preis",
    "image_only": "Nur Bild",
    "do_not_use": "Nicht verwenden",
}
QUERY_STATUSES = {
    "unreviewed": "Ungeprüft",
    "good": "Gut",
    "refine": "Nachbessern",
    "discard": "Verwerfen",
}
REVIEW_TAGS = {
    "reproduction": "Reproduktion",
    "lot": "Konvolut",
    "damaged": "Beschädigt",
    "wrong_category": "Falsche Kategorie",
    "bad_image": "Schlechtes Foto",
    "price_missing": "Preis fehlt",
    "uncertain_attribution": "Zuschreibung unsicher",
    "duplicate": "Dublette",
}
DEAL_REVIEW_STATUSES = {
    "unreviewed": "Ungeprüft",
    "candidate": "Kandidat",
    "checked": "Geprüft",
    "skip": "Verwerfen",
}
DEAL_REVIEW_TAGS = {
    "needs_match": "Abgleich nötig",
    "condition": "Zustand prüfen",
    "bundle": "Konvolut",
    "unclear": "Unklare Zuschreibung",
    "duplicate": "Dublette",
}
MATCH_REVIEW_STATUSES = {
    "unreviewed": "Ungeprueft",
    "candidate": "Kandidat",
    "checked": "Geprueft",
    "skip": "Verwerfen",
}
DEAL_SOURCE_LABELS = {
    "ebay_active": "eBay DE · Privat",
}
PRICE_STATUSES = {
    "sold": "Verkauft",
    "ask": "Angebot",
    "current_bid": "Aktuelles Gebot",
    "estimate": "Schätzung",
    "unsold": "Unverkauft",
    "unknown": "Unbekannt",
}
PRICE_BASIS_LABELS = {
    "hammer": "Hammerpreis",
    "realised": "Realisierter Preis",
    "premium_included": "Inklusive Aufgeld",
    "reserve": "Reserve/Angebot",
    "current_bid": "Aktuelles Gebot",
    "estimate": "Schätzung",
    "unknown": "Preisgrundlage unbekannt",
}
RUN_LABELS = {
    "queued": "Wartet",
    "running": "Läuft",
    "completed": "Abgeschlossen",
    "completed_with_errors": "Mit Fehlern beendet",
    "cancel_requested": "Stopp angefordert",
    "cancelled": "Abgebrochen",
    "failed": "Fehlgeschlagen",
}


def init_with_retry() -> None:
    last_error: Exception | None = None
    for attempt in range(20):
        try:
            init_schema()
            return
        except Exception as exc:  # Postgres can still be starting during deployment
            last_error = exc
            if attempt == 19:
                raise
            time.sleep(2)
    if last_error:
        raise last_error


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    init_with_retry()

    @app.before_request
    def require_auth() -> Any:
        if request.path == "/health":
            return None
        username = os.environ.get("DASHBOARD_USER", "niklas")
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            return ("DASHBOARD_PASSWORD fehlt.", 503)
        auth = request.authorization
        if (
            not auth
            or not hmac.compare_digest(auth.username or "", username)
            or not hmac.compare_digest(auth.password or "", password)
        ):
            return (
                "Anmeldung erforderlich.",
                401,
                {"WWW-Authenticate": 'Basic realm="klein antik"'},
            )
        return None

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        return {
            "content_statuses": CONTENT_STATUSES,
            "use_statuses": USE_STATUSES,
            "query_statuses": QUERY_STATUSES,
            "review_tags": REVIEW_TAGS,
            "deal_review_statuses": DEAL_REVIEW_STATUSES,
            "deal_review_tags": DEAL_REVIEW_TAGS,
            "deal_source_labels": DEAL_SOURCE_LABELS,
            "price_statuses": PRICE_STATUSES,
            "price_basis_labels": PRICE_BASIS_LABELS,
            "source_labels": SOURCE_LABELS,
            "run_labels": RUN_LABELS,
            "categories": category_options(),
            "format_money": format_money,
            "format_time": format_time,
        }

    @app.get("/health")
    def health() -> Any:
        try:
            with connection() as conn:
                conn.execute("SELECT 1").fetchone()
            return jsonify({"status": "ok"})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)[:200]}), 503

    @app.get("/")
    def index() -> Any:
        return redirect(url_for("references"))

    @app.get("/references")
    def references() -> Any:
        category = request.args.get("category", "").strip()
        content_status = request.args.get("status", "").strip()
        use_status = request.args.get("use", "").strip()
        price_status = request.args.get("price_status", "").strip()
        source = request.args.get("source", "").strip()
        search = request.args.get("q", "").strip()
        page = max(1, min(10000, request.args.get("page", 1, type=int)))
        page_size = 36
        where = ["TRUE"]
        params: list[Any] = []

        if category:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM market_listing_query_matches fqm
                    JOIN search_queries fq ON fq.id = fqm.query_id
                    WHERE fqm.listing_id = l.id AND fq.category = %s
                )
                """
            )
            params.append(category)
        if content_status in CONTENT_STATUSES:
            where.append("COALESCE(r.content_status, 'unreviewed') = %s")
            params.append(content_status)
        if use_status in USE_STATUSES:
            where.append("COALESCE(r.use_status, 'price_image') = %s")
            params.append(use_status)
        if price_status in PRICE_STATUSES:
            where.append("l.price_status = %s")
            params.append(price_status)
        if source in SOURCE_LABELS:
            where.append("l.source = %s")
            params.append(source)
        if search:
            where.append("(l.title ILIKE %s OR l.source_item_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = " AND ".join(where)
        with connection() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM market_listings l
                LEFT JOIN market_listing_reviews r ON r.listing_id = l.id
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    l.*,
                    COALESCE(r.content_status, 'unreviewed') AS content_status,
                    COALESCE(r.use_status, 'price_image') AS use_status,
                    COALESCE(r.tags, ARRAY[]::TEXT[]) AS tags,
                    COALESCE(r.note, '') AS note,
                    r.updated_at AS review_updated_at,
                    array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts,
                    array_agg(DISTINCT q.category ORDER BY q.category) AS category_ids
                FROM market_listings l
                LEFT JOIN market_listing_reviews r ON r.listing_id = l.id
                JOIN market_listing_query_matches qm ON qm.listing_id = l.id
                JOIN search_queries q ON q.id = qm.query_id
                WHERE {where_sql}
                GROUP BY l.id, r.listing_id, r.content_status, r.use_status,
                    r.tags, r.note, r.updated_at
                ORDER BY l.last_seen_at DESC, l.id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            stats = market_stats(conn)

        total = int(total_row["count"]) if total_row else 0
        return render_template(
            "references.html",
            active_tab="references",
            listings=[dict(row) for row in rows],
            stats=stats,
            total=total,
            page=page,
            pages=max(1, (total + page_size - 1) // page_size),
            filters={
                "category": category,
                "status": content_status,
                "use": use_status,
                "price_status": price_status,
                "source": source,
                "q": search,
            },
        )

    @app.get("/queries")
    def queries() -> Any:
        category = request.args.get("category", "").strip()
        where = "WHERE q.category = %s" if category else ""
        params = [category] if category else []
        with connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    q.*,
                    COUNT(DISTINCT qm.listing_id) AS listing_count,
                    COUNT(DISTINCT qm.listing_id) FILTER (
                        WHERE l.price_status = 'sold'
                    ) AS sold_count,
                    COUNT(DISTINCT qm.listing_id) FILTER (
                        WHERE l.price_status = 'ask'
                    ) AS ask_count,
                    COUNT(DISTINCT qm.listing_id) FILTER (
                        WHERE l.price_status = 'unsold'
                    ) AS unsold_count,
                    COUNT(DISTINCT qm.listing_id) FILTER (
                        WHERE COALESCE(r.content_status, 'unreviewed') = 'usable'
                    ) AS usable_count,
                    array_remove(array_agg(DISTINCT l.source ORDER BY l.source), NULL)
                        AS source_ids
                FROM search_queries q
                LEFT JOIN market_listing_query_matches qm ON qm.query_id = q.id
                LEFT JOIN market_listings l ON l.id = qm.listing_id
                LEFT JOIN market_listing_reviews r ON r.listing_id = l.id
                {where}
                GROUP BY q.id
                ORDER BY q.position
                """,
                params,
            ).fetchall()
        return render_template(
            "queries.html",
            active_tab="queries",
            queries=[dict(row) for row in rows],
            selected_category=category,
        )

    @app.get("/runs")
    def runs() -> Any:
        with connection() as conn:
            run_rows = conn.execute(
                "SELECT * FROM market_runs ORDER BY id DESC LIMIT 30"
            ).fetchall()
            worker = conn.execute(
                """
                SELECT *, last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'market-importer'
                """
            ).fetchone()
            failed_tasks = conn.execute(
                """
                SELECT task.run_id, task.source, q.query_text, task.error
                FROM market_run_tasks task
                JOIN search_queries q ON q.id = task.query_id
                WHERE task.status = 'failed'
                ORDER BY task.id DESC
                LIMIT 30
                """
            ).fetchall()
        return render_template(
            "runs.html",
            active_tab="runs",
            runs=[dict(row) for row in run_rows],
            worker=dict(worker) if worker else None,
            failed_tasks=[dict(row) for row in failed_tasks],
            expected_queries=EXPECTED_QUERY_COUNT,
            expected_tasks=expected_market_tasks(),
        )

    @app.get("/deals")
    def deals() -> Any:
        category = request.args.get("category", "").strip()
        review_status = request.args.get("status", "").strip()
        search = request.args.get("q", "").strip()
        page = max(1, min(10000, request.args.get("page", 1, type=int)))
        page_size = 36
        where = ["TRUE"]
        params: list[Any] = []
        if category:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM deal_listing_query_matches dqm
                    JOIN search_queries dq ON dq.id = dqm.query_id
                    WHERE dqm.listing_id = d.id AND dq.category = %s
                )
                """
            )
            params.append(category)
        if review_status in DEAL_REVIEW_STATUSES:
            where.append("COALESCE(r.review_status, 'unreviewed') = %s")
            params.append(review_status)
        if search:
            where.append("(d.title ILIKE %s OR d.source_item_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        where_sql = " AND ".join(where)

        with connection() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM deal_listings d
                LEFT JOIN deal_listing_reviews r ON r.listing_id = d.id
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    d.*,
                    COALESCE(r.review_status, 'unreviewed') AS review_status,
                    COALESCE(r.tags, ARRAY[]::TEXT[]) AS tags,
                    COALESCE(r.note, '') AS note,
                    array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts
                FROM deal_listings d
                LEFT JOIN deal_listing_reviews r ON r.listing_id = d.id
                JOIN deal_listing_query_matches qm ON qm.listing_id = d.id
                JOIN search_queries q ON q.id = qm.query_id
                WHERE {where_sql}
                GROUP BY d.id, r.listing_id, r.review_status, r.tags, r.note
                ORDER BY d.price_value ASC NULLS LAST, d.last_seen_at DESC, d.id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            run_rows = conn.execute(
                "SELECT * FROM deal_runs ORDER BY id DESC LIMIT 8"
            ).fetchall()
            worker = conn.execute(
                """
                SELECT *, last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'market-importer'
                """
            ).fetchone()
            stats = deal_stats(conn)

        total = int(total_row["count"]) if total_row else 0
        return render_template(
            "deals.html",
            active_tab="deals",
            listings=[dict(row) for row in rows],
            stats=stats,
            total=total,
            page=page,
            pages=max(1, (total + page_size - 1) // page_size),
            filters={
                "category": category,
                "status": review_status,
                "q": search,
            },
            runs=[dict(row) for row in run_rows],
            worker=dict(worker) if worker else None,
            credentials_configured=importer_ready(),
            has_active_run=any(
                run["status"] in {"queued", "running", "cancel_requested"}
                for run in run_rows
            ),
        )

    @app.post("/api/deals/runs/start")
    @json_endpoint
    def start_deal_run() -> Any:
        if not importer_ready():
            return jsonify(
                {"error": "EBAY_CLIENT_ID und EBAY_CLIENT_SECRET sind im Klein-Antik-Importer nicht gesetzt."}
            ), 409
        with connection() as conn:
            worker = conn.execute(
                """
                SELECT last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'market-importer'
                """
            ).fetchone()
            if not worker or not worker["online"]:
                return jsonify({"error": "Der Importer ist nicht erreichbar."}), 409
            active = conn.execute(
                """
                SELECT id
                FROM market_runs
                WHERE status IN ('queued', 'running', 'cancel_requested')
                UNION ALL
                SELECT id
                FROM deal_runs
                WHERE status IN ('queued', 'running', 'cancel_requested')
                LIMIT 1
                """
            ).fetchone()
            if active:
                return jsonify({"error": "Es läuft bereits ein Import."}), 409
            query_rows = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE enabled
                ORDER BY position
                """
            ).fetchall()
            if len(query_rows) != EXPECTED_QUERY_COUNT:
                return jsonify(
                    {
                        "error": (
                            f"Start verweigert: erwartet werden {EXPECTED_QUERY_COUNT}, "
                            f"gefunden wurden {len(query_rows)} aktive Suchen."
                        )
                    }
                ), 409
            run = conn.execute(
                """
                INSERT INTO deal_runs (source, planned_tasks)
                VALUES ('ebay_active', %s)
                RETURNING id
                """,
                (len(query_rows),),
            ).fetchone()
            for query_row in query_rows:
                conn.execute(
                    """
                    INSERT INTO deal_run_tasks (run_id, query_id, source)
                    VALUES (%s, %s, 'ebay_active')
                    """,
                    (run["id"], query_row["id"]),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "planned_queries": len(query_rows),
                "planned_calls": len(query_rows),
            }
        ), 201

    @app.post("/api/deals/runs/<int:run_id>/cancel")
    @json_endpoint
    def cancel_deal_run(run_id: int) -> Any:
        with connection() as conn:
            row = conn.execute(
                """
                UPDATE deal_runs
                SET status = 'cancel_requested', updated_at = now()
                WHERE id = %s AND status IN ('queued', 'running')
                RETURNING id
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return jsonify({"error": "Lauf kann nicht gestoppt werden."}), 409
        return jsonify({"ok": True})

    @app.post("/api/deals/<int:listing_id>/review")
    @json_endpoint
    def update_deal_review(listing_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        review_status = str(body.get("review_status") or "")
        note = str(body.get("note") or "")[:4000]
        tags = body.get("tags") or []
        if review_status not in DEAL_REVIEW_STATUSES:
            return jsonify({"error": "Ungültiger Prüfstatus."}), 400
        if not isinstance(tags, list) or any(tag not in DEAL_REVIEW_TAGS for tag in tags):
            return jsonify({"error": "Ungültige Kennzeichnung."}), 400
        tags = list(dict.fromkeys(str(tag) for tag in tags))
        with connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM deal_listings WHERE id = %s",
                (listing_id,),
            ).fetchone()
            if not exists:
                return jsonify({"error": "Listing nicht gefunden."}), 404
            row = conn.execute(
                """
                INSERT INTO deal_listing_reviews (
                    listing_id, review_status, tags, note, updated_at
                )
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (listing_id) DO UPDATE SET
                    review_status = EXCLUDED.review_status,
                    tags = EXCLUDED.tags,
                    note = EXCLUDED.note,
                    updated_at = now()
                RETURNING updated_at
                """,
                (listing_id, review_status, tags, note),
            ).fetchone()
        return jsonify({"ok": True, "updated_at": row["updated_at"].isoformat()})

    @app.get("/image-review")
    def image_review() -> Any:
        category = request.args.get("category", "").strip()
        review_status = request.args.get("status", "").strip()
        minimum_score = request.args.get("min_score", "0.60").strip()
        page = max(1, min(10000, request.args.get("page", 1, type=int)))
        page_size = 24
        try:
            score_value = max(0.0, min(1.0, float(minimum_score)))
        except ValueError:
            score_value = 0.60
        where = ["match.score >= %s"]
        params: list[Any] = [score_value]
        if category:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM deal_listing_query_matches deal_query
                    JOIN search_queries deal_search ON deal_search.id = deal_query.query_id
                    JOIN market_listing_query_matches market_query
                        ON market_query.listing_id = match.market_listing_id
                    JOIN search_queries market_search ON market_search.id = market_query.query_id
                    WHERE deal_query.listing_id = match.deal_listing_id
                      AND deal_search.category = %s
                      AND market_search.category = deal_search.category
                )
                """
            )
            params.append(category)
        if review_status in MATCH_REVIEW_STATUSES:
            where.append("match.review_status = %s")
            params.append(review_status)
        where_sql = " AND ".join(where)
        with connection() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM image_matches match WHERE {where_sql}",
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    match.*,
                    deal.title AS deal_title,
                    deal.url AS deal_url,
                    deal.image_url AS deal_image_url,
                    deal.price_value AS deal_price_value,
                    deal.currency AS deal_currency,
                    deal.condition_text AS deal_condition_text,
                    market.title AS market_title,
                    market.url AS market_url,
                    market.image_url AS market_image_url,
                    market.price_value AS market_price_value,
                    market.currency AS market_currency,
                    market.price_status AS market_price_status,
                    market.price_basis AS market_price_basis,
                    market.source AS market_source,
                    array_agg(DISTINCT deal_search.category_label ORDER BY deal_search.category_label)
                        FILTER (WHERE deal_search.category = market_search.category) AS category_labels
                FROM image_matches match
                JOIN deal_listings deal ON deal.id = match.deal_listing_id
                JOIN market_listings market ON market.id = match.market_listing_id
                LEFT JOIN deal_listing_query_matches deal_query
                    ON deal_query.listing_id = deal.id
                LEFT JOIN search_queries deal_search ON deal_search.id = deal_query.query_id
                LEFT JOIN market_listing_query_matches market_query
                    ON market_query.listing_id = market.id
                LEFT JOIN search_queries market_search ON market_search.id = market_query.query_id
                WHERE {where_sql}
                GROUP BY match.id, deal.id, market.id
                ORDER BY match.score DESC, match.updated_at DESC, match.id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            run_rows = conn.execute(
                "SELECT * FROM image_match_runs ORDER BY id DESC LIMIT 8"
            ).fetchall()
            worker = conn.execute(
                """
                SELECT *, last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'image-matcher'
                """
            ).fetchone()
            stats = image_match_stats(conn)
        total = int(total_row["count"]) if total_row else 0
        return render_template(
            "image_review.html",
            active_tab="image_review",
            matches=[dict(row) for row in rows],
            stats=stats,
            total=total,
            page=page,
            pages=max(1, (total + page_size - 1) // page_size),
            categories=category_options(),
            match_review_statuses=MATCH_REVIEW_STATUSES,
            price_statuses=PRICE_STATUSES,
            price_basis_labels=PRICE_BASIS_LABELS,
            source_labels=SOURCE_LABELS,
            run_labels=RUN_LABELS,
            worker=dict(worker) if worker else None,
            runs=[dict(row) for row in run_rows],
            has_active_run=any(
                run["status"] in {"queued", "running", "cancel_requested"}
                for run in run_rows
            ),
            filters={
                "category": category,
                "status": review_status,
                "min_score": f"{score_value:.2f}",
            },
        )

    @app.post("/api/image-matches/runs/start")
    @json_endpoint
    def start_image_match_run() -> Any:
        with connection() as conn:
            worker = conn.execute(
                """
                SELECT last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'image-matcher'
                """
            ).fetchone()
            if not worker or not worker["online"]:
                return jsonify({"error": "Der Bildabgleich-Worker ist nicht erreichbar."}), 409
            active = conn.execute(
                """
                SELECT id FROM image_match_runs
                WHERE status IN ('queued', 'running', 'cancel_requested')
                LIMIT 1
                """
            ).fetchone()
            if active:
                return jsonify({"error": "Es laeuft bereits ein Bildabgleich."}), 409
            counts = conn.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE image_url <> '') AS deal_count,
                    (SELECT COUNT(*) FROM market_listings WHERE image_url <> '' AND price_value IS NOT NULL)
                        AS market_count
                FROM deal_listings
                """
            ).fetchone()
            if not counts or not int(counts["deal_count"]) or not int(counts["market_count"]):
                return jsonify({"error": "Deals oder Marktpreise mit Bild und Preis fehlen noch."}), 409
            run = conn.execute(
                """
                INSERT INTO image_match_runs (planned_tasks)
                VALUES (%s)
                RETURNING id
                """,
                (int(counts["deal_count"]),),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO image_match_tasks (run_id, deal_listing_id)
                SELECT %s, id
                FROM deal_listings
                WHERE image_url <> ''
                ORDER BY id
                """,
                (run["id"],),
            )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "planned_deals": int(counts["deal_count"]),
                "reference_pool": int(counts["market_count"]),
            }
        ), 201

    @app.post("/api/image-matches/runs/<int:run_id>/cancel")
    @json_endpoint
    def cancel_image_match_run(run_id: int) -> Any:
        with connection() as conn:
            row = conn.execute(
                """
                UPDATE image_match_runs
                SET status = 'cancel_requested', updated_at = now()
                WHERE id = %s AND status IN ('queued', 'running')
                RETURNING id
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return jsonify({"error": "Bildabgleich kann nicht gestoppt werden."}), 409
        return jsonify({"ok": True})

    @app.post("/api/image-matches/<int:match_id>/review")
    @json_endpoint
    def update_image_match_review(match_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        review_status = str(body.get("review_status") or "")
        note = str(body.get("note") or "")[:4000]
        if review_status not in MATCH_REVIEW_STATUSES:
            return jsonify({"error": "Ungueltiger Pruefstatus."}), 400
        with connection() as conn:
            row = conn.execute(
                """
                UPDATE image_matches
                SET review_status = %s, note = %s, updated_at = now()
                WHERE id = %s
                RETURNING updated_at
                """,
                (review_status, note, match_id),
            ).fetchone()
        if not row:
            return jsonify({"error": "Bildvergleich nicht gefunden."}), 404
        return jsonify({"ok": True, "updated_at": row["updated_at"].isoformat()})

    @app.post("/api/listings/<int:listing_id>/review")
    @json_endpoint
    def update_listing_review(listing_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        content_status = str(body.get("content_status") or "")
        use_status = str(body.get("use_status") or "")
        note = str(body.get("note") or "")[:4000]
        tags = body.get("tags") or []
        if content_status not in CONTENT_STATUSES:
            return jsonify({"error": "Ungültiger Prüfstatus."}), 400
        if use_status not in USE_STATUSES:
            return jsonify({"error": "Ungültige Verwendung."}), 400
        if not isinstance(tags, list) or any(tag not in REVIEW_TAGS for tag in tags):
            return jsonify({"error": "Ungültige Kennzeichnung."}), 400
        tags = list(dict.fromkeys(str(tag) for tag in tags))
        with connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM market_listings WHERE id = %s",
                (listing_id,),
            ).fetchone()
            if not exists:
                return jsonify({"error": "Listing nicht gefunden."}), 404
            row = conn.execute(
                """
                INSERT INTO market_listing_reviews (
                    listing_id, content_status, use_status, tags, note, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (listing_id) DO UPDATE SET
                    content_status = EXCLUDED.content_status,
                    use_status = EXCLUDED.use_status,
                    tags = EXCLUDED.tags,
                    note = EXCLUDED.note,
                    updated_at = now()
                RETURNING updated_at
                """,
                (listing_id, content_status, use_status, tags, note),
            ).fetchone()
        return jsonify({"ok": True, "updated_at": row["updated_at"].isoformat()})

    @app.post("/api/queries/<query_id>/review")
    @json_endpoint
    def update_query_review(query_id: str) -> Any:
        body = request.get_json(silent=True) or {}
        review_status = str(body.get("review_status") or "")
        note = str(body.get("note") or "")[:4000]
        if review_status not in QUERY_STATUSES:
            return jsonify({"error": "Ungültige Suchbewertung."}), 400
        with connection() as conn:
            row = conn.execute(
                """
                UPDATE search_queries
                SET review_status = %s, note = %s, updated_at = now()
                WHERE id = %s
                RETURNING updated_at
                """,
                (review_status, note, query_id),
            ).fetchone()
        if not row:
            return jsonify({"error": "Suchbegriff nicht gefunden."}), 404
        return jsonify({"ok": True, "updated_at": row["updated_at"].isoformat()})

    @app.post("/api/runs/start")
    @json_endpoint
    def start_run() -> Any:
        with connection() as conn:
            worker = conn.execute(
                """
                SELECT last_seen_at > now() - interval '90 seconds' AS online
                FROM worker_status
                WHERE name = 'market-importer'
                """
            ).fetchone()
            if not worker or not worker["online"]:
                return jsonify({"error": "Der Marktpreis-Importer ist nicht erreichbar."}), 409

            active = conn.execute(
                """
                SELECT id
                FROM market_runs
                WHERE status IN ('queued', 'running', 'cancel_requested')
                LIMIT 1
                """
            ).fetchone()
            if active:
                return jsonify({"error": f"Lauf {active['id']} ist bereits aktiv."}), 409

            query_rows = conn.execute(
                """
                SELECT id, category
                FROM search_queries
                WHERE enabled
                ORDER BY position
                """
            ).fetchall()
            if len(query_rows) != EXPECTED_QUERY_COUNT:
                return jsonify(
                    {
                        "error": (
                            f"Start verweigert: erwartet werden {EXPECTED_QUERY_COUNT}, "
                            f"gefunden wurden {len(query_rows)} aktive Suchen."
                        )
                    }
                ), 409

            tasks = [
                (query_row["id"], source)
                for query_row in query_rows
                for source in sources_for_category(query_row["category"])
            ]
            run = conn.execute(
                "INSERT INTO market_runs (planned_tasks) VALUES (%s) RETURNING id",
                (len(tasks),),
            ).fetchone()
            for query_id, source in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (run_id, query_id, source)
                    VALUES (%s, %s, %s)
                    """,
                    (run["id"], query_id, source),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "planned_queries": len(query_rows),
                "planned_tasks": len(tasks),
            }
        ), 201

    @app.post("/api/runs/<int:run_id>/cancel")
    @json_endpoint
    def cancel_run(run_id: int) -> Any:
        with connection() as conn:
            row = conn.execute(
                """
                UPDATE market_runs
                SET status = 'cancel_requested', updated_at = now()
                WHERE id = %s AND status IN ('queued', 'running')
                RETURNING id
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return jsonify({"error": "Lauf kann nicht gestoppt werden."}), 409
        return jsonify({"ok": True})

    return app


def json_endpoint(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not request.is_json:
            return jsonify({"error": "JSON erwartet."}), 415
        return function(*args, **kwargs)

    return wrapper


def market_stats(conn: Any) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE l.price_status = 'sold') AS sold,
            COUNT(*) FILTER (WHERE l.price_status = 'ask') AS ask,
            COUNT(*) FILTER (WHERE l.price_status = 'unsold') AS unsold,
            COUNT(*) FILTER (WHERE l.price_status = 'unknown') AS unknown,
            COUNT(*) FILTER (
                WHERE COALESCE(r.content_status, 'unreviewed') = 'usable'
            ) AS usable
        FROM market_listings l
        LEFT JOIN market_listing_reviews r ON r.listing_id = l.id
        """
    ).fetchone()
    return {key: int(row[key]) for key in row}


def deal_stats(conn: Any) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE l.image_url <> '') AS with_image,
            COUNT(*) FILTER (WHERE l.seller_account_type = 'individual') AS private_sellers,
            COUNT(*) FILTER (
                WHERE COALESCE(r.review_status, 'unreviewed') = 'candidate'
            ) AS candidates,
            COUNT(*) FILTER (
                WHERE COALESCE(r.review_status, 'unreviewed') = 'checked'
            ) AS checked
        FROM deal_listings l
        LEFT JOIN deal_listing_reviews r ON r.listing_id = l.id
        """
    ).fetchone()
    return {key: int(row[key]) for key in row}


def image_match_stats(conn: Any) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE score >= 0.70) AS strong,
            COUNT(DISTINCT deal_listing_id) AS deals,
            COUNT(*) FILTER (WHERE review_status = 'candidate') AS candidates,
            COUNT(*) FILTER (WHERE review_status = 'checked') AS checked,
            COUNT(*) FILTER (WHERE review_status = 'skip') AS skipped
        FROM image_matches
        """
    ).fetchone()
    return {key: int(row[key]) for key in row}


def expected_market_tasks() -> int:
    return sum(
        len(sources_for_category(query["category"])) for query in load_queries()
    )


def format_money(value: Decimal | float | None, currency: str = "") -> str:
    if value is None:
        return "Preis fehlt"
    amount = Decimal(str(value))
    rendered = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if rendered.endswith(",00"):
        rendered = rendered[:-3]
    return f"{rendered} {currency}".strip()


def format_time(value: datetime | None) -> str:
    if not value:
        return "–"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%d.%m.%Y · %H:%M")


app = create_app()
