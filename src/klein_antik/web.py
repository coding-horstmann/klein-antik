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
from .config import env_int
from .db import connection, init_schema
from .ebay_active import importer_ready
from .market_sources import (
    EXTERNAL_PILOT_QUERY_IDS,
    EXTERNAL_PILOT_SOURCES,
    MEISSEN_ARCHIVE_QUERY_ID,
    MEISSEN_ARCHIVE_SOURCE,
    MEISSEN_ARCHIVE_START_PAGE,
    MEISSEN_ARCHIVE_TARGET_PAGE,
    MEISSEN_DEAL_PILOT_SOURCES,
    MEISSEN_PORCELAIN_BACKFILL_BATCH_PAGES,
    MEISSEN_PORCELAIN_BACKFILL_SOURCES,
    MEISSEN_PORCELAIN_PILOT_PAGE_COUNTS,
    MEISSEN_PORCELAIN_PILOT_SOURCES,
    SOURCE_MAX_PAGES,
    SOURCE_LABELS,
    build_session,
    enrich_private_marketplace_listing,
    sources_for_category,
)
from .price_filters import format_price_filter, parse_price_filter


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
RUN_KIND_LABELS = {
    "refresh": "Aktualisierung",
    "backfill": "Archiv-Backfill",
    "source_pilot": "Neue Quellen - Pilot",
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
            "run_kind_labels": RUN_KIND_LABELS,
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

    @app.get("/api/exports/meissen-references")
    def export_meissen_references() -> Any:
        """Return the sold Meissen corpus for an auditable scout run."""
        with connection() as conn:
            rows = conn.execute(
                """
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
            ).fetchall()

        records: list[dict[str, Any]] = []
        source_counts: dict[str, int] = {}
        currency_counts: dict[str, int] = {}
        price_basis_counts: dict[str, int] = {}
        for row in rows:
            record = dict(row)
            record["reference_id"] = (
                f"{record['source']}:{record['source_item_id']}"
            )
            records.append(record)
            for counts, key in (
                (source_counts, "source"),
                (currency_counts, "currency"),
                (price_basis_counts, "price_basis"),
            ):
                value = str(record.get(key) or "unknown")
                counts[value] = counts.get(value, 0) + 1

        return jsonify(
            {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "category": "meissen_porcelain",
                "filters": {
                    "price_status": "sold",
                    "priced_only": True,
                    "forbidden_deal_sources_excluded": True,
                },
                "record_count": len(records),
                "source_counts": dict(sorted(source_counts.items())),
                "currency_counts": dict(sorted(currency_counts.items())),
                "price_basis_counts": dict(sorted(price_basis_counts.items())),
                "records": records,
            }
        )

    @app.get("/api/exports/meissen-deal-pilot")
    def export_meissen_deal_pilot() -> Any:
        """Export frozen active marketplace offers from specified completed pilot runs."""
        run_ids: list[int] = []
        for raw_run_id in request.args.getlist("run_id"):
            try:
                run_id = int(raw_run_id)
            except ValueError:
                return jsonify({"error": "run_id muss eine ganze Zahl sein."}), 400
            if run_id > 0 and run_id not in run_ids:
                run_ids.append(run_id)
        if not run_ids:
            return jsonify({"error": "Mindestens eine run_id wird benoetigt."}), 400

        with connection() as conn:
            rows = conn.execute(
                """
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
                    l.attribution,
                    l.last_seen_at,
                    l.raw_result,
                    array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts
                FROM market_listings AS l
                JOIN market_listing_query_matches AS qm ON qm.listing_id = l.id
                JOIN search_queries AS q ON q.id = qm.query_id
                WHERE qm.last_run_id = ANY(%s)
                  AND q.id = %s
                  AND l.source = ANY(%s)
                  AND l.price_status = 'ask'
                  AND l.price_value IS NOT NULL
                GROUP BY l.id
                ORDER BY l.source, l.source_item_id
                """,
                (run_ids, MEISSEN_ARCHIVE_QUERY_ID, list(MEISSEN_DEAL_PILOT_SOURCES)),
            ).fetchall()

        listings: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            raw_result = record.get("raw_result")
            raw_result = raw_result if isinstance(raw_result, dict) else {}
            source = str(record["source"])
            external_id = str(record["source_item_id"])
            listings.append(
                {
                    "listing_id": f"{source}:{external_id}",
                    "source": source,
                    "external_id": external_id,
                    "title": str(record["title"] or ""),
                    "url": str(record["url"] or ""),
                    "image_urls": [str(record["image_url"])] if record["image_url"] else [],
                    "price_raw": str(record["price_raw"] or ""),
                    "price_value": str(record["price_value"]),
                    "currency": str(record["currency"] or ""),
                    "sale_mode": str(raw_result.get("sale_mode") or "fixed_price"),
                    "availability": str(raw_result.get("availability") or "active"),
                    "attribution_status": str(record["attribution"] or "stated"),
                    "discovery_queries": list(record["query_texts"] or []),
                    "discovery_scopes": ["explicit_query"],
                    "collected_at": record["last_seen_at"].isoformat(),
                }
            )

        return jsonify(
            {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_ids": run_ids,
                "record_count": len(listings),
                "sources": sorted({str(item["source"]) for item in listings}),
                "listings": listings,
            }
        )

    @app.post("/api/exports/meissen-deal-pilot/enrich")
    @json_endpoint
    def enrich_meissen_deal_pilot() -> Any:
        body = request.get_json(silent=True) or {}
        requested_ids = body.get("listing_ids")
        requested_runs = body.get("run_ids")
        if not isinstance(requested_ids, list) or not isinstance(requested_runs, list):
            return jsonify({"error": "listing_ids und run_ids muessen Listen sein."}), 400
        listing_ids = list(dict.fromkeys(str(value).strip() for value in requested_ids if str(value).strip()))
        if not listing_ids or len(listing_ids) > 25:
            return jsonify({"error": "Es sind ein bis 25 Listing-IDs erlaubt."}), 400
        try:
            run_ids = list(dict.fromkeys(int(value) for value in requested_runs if int(value) > 0))
        except (TypeError, ValueError):
            return jsonify({"error": "run_ids muessen positive ganze Zahlen sein."}), 400
        if not run_ids:
            return jsonify({"error": "Mindestens eine run_id wird benoetigt."}), 400

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT l.source, l.source_item_id, l.title, l.url
                FROM market_listings AS l
                JOIN market_listing_query_matches AS qm ON qm.listing_id = l.id
                JOIN search_queries AS q ON q.id = qm.query_id
                WHERE qm.last_run_id = ANY(%s)
                  AND q.id = %s
                  AND l.source = ANY(%s)
                  AND l.price_status = 'ask'
                """,
                (run_ids, MEISSEN_ARCHIVE_QUERY_ID, list(MEISSEN_DEAL_PILOT_SOURCES)),
            ).fetchall()
        listings = {
            f"{row['source']}:{row['source_item_id']}": dict(row)
            for row in rows
        }
        missing = [listing_id for listing_id in listing_ids if listing_id not in listings]
        if missing:
            return jsonify({"error": "Unbekannte Listing-IDs: " + ", ".join(missing)}), 400

        session = build_session()
        details: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        try:
            for listing_id in listing_ids:
                listing = listings[listing_id]
                try:
                    detail = enrich_private_marketplace_listing(
                        session,
                        source=str(listing["source"]),
                        url=str(listing["url"]),
                        title=str(listing["title"]),
                    )
                    detail["listing_id"] = listing_id
                    detail["fetched_at"] = datetime.now(timezone.utc).isoformat()
                    details.append(detail)
                except Exception as exc:
                    failures.append(
                        {"listing_id": listing_id, "error": f"{type(exc).__name__}: {exc}"[:500]}
                    )
        finally:
            session.close()
        return jsonify(
            {
                "schema_version": 1,
                "run_ids": run_ids,
                "requested_count": len(listing_ids),
                "detail_count": len(details),
                "failure_count": len(failures),
                "details": details,
                "failures": failures,
            }
        )

    @app.get("/references")
    def references() -> Any:
        category = request.args.get("category", "").strip()
        keyword = request.args.get("keyword", "").strip()
        price_status = request.args.get("price_status", "").strip()
        currency = request.args.get("currency", "").strip().upper()
        price_min = parse_price_filter(request.args.get("price_min", ""))
        price_max = parse_price_filter(request.args.get("price_max", ""))
        source = request.args.get("source", "").strip()
        search = request.args.get("q", "").strip()
        sort = request.args.get("sort", "newest").strip()
        page = max(1, min(10000, request.args.get("page", 1, type=int)))
        page_size = 100
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
        if keyword:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM market_listing_query_matches keyword_match
                    WHERE keyword_match.listing_id = l.id
                      AND keyword_match.query_id = %s
                )
                """
            )
            params.append(keyword)
        if price_status in PRICE_STATUSES:
            where.append("l.price_status = %s")
            params.append(price_status)
        if currency:
            where.append("l.currency = %s")
            params.append(currency)
        if price_min is not None:
            where.append("l.price_value >= %s")
            params.append(price_min)
        if price_max is not None:
            where.append("l.price_value <= %s")
            params.append(price_max)
        if source:
            where.append("l.source = %s")
            params.append(source)
        if search:
            where.append("(l.title ILIKE %s OR l.source_item_id ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = " AND ".join(where)
        sort_orders = {
            "newest": "l.last_seen_at DESC, l.id DESC",
            "price_asc": "l.price_value ASC NULLS LAST, l.last_seen_at DESC, l.id DESC",
            "price_desc": "l.price_value DESC NULLS LAST, l.last_seen_at DESC, l.id DESC",
        }
        if sort not in sort_orders:
            sort = "newest"
        with connection() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM market_listings l
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    l.*,
                    array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts,
                    array_agg(DISTINCT q.category ORDER BY q.category) AS category_ids
                FROM market_listings l
                JOIN market_listing_query_matches qm ON qm.listing_id = l.id
                JOIN search_queries q ON q.id = qm.query_id
                WHERE {where_sql}
                GROUP BY l.id
                ORDER BY {sort_orders[sort]}
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            keyword_rows = conn.execute(
                """
                SELECT
                    q.id,
                    q.category,
                    q.category_label,
                    q.query_text,
                    COUNT(DISTINCT qm.listing_id) AS listing_count
                FROM search_queries q
                LEFT JOIN market_listing_query_matches qm ON qm.query_id = q.id
                WHERE q.enabled = TRUE
                GROUP BY q.id
                ORDER BY q.position
                """
            ).fetchall()
            source_rows = conn.execute(
                "SELECT DISTINCT source FROM market_listings ORDER BY source"
            ).fetchall()
            currency_rows = conn.execute(
                """
                SELECT DISTINCT currency
                FROM market_listings
                WHERE currency <> ''
                ORDER BY currency
                """
            ).fetchall()
            stats = market_stats(conn, where_sql, params)

        total = int(total_row["count"]) if total_row else 0
        keywords_by_category: dict[str, list[dict[str, Any]]] = {
            item["id"]: [] for item in category_options()
        }
        for row in keyword_rows:
            keyword_row = dict(row)
            keywords_by_category.setdefault(keyword_row["category"], []).append(keyword_row)
        keyword_groups = [
            {
                "id": item["id"],
                "label": item["label"],
                "keywords": keywords_by_category.get(item["id"], []),
            }
            for item in category_options()
        ]
        return render_template(
            "references.html",
            active_tab="references",
            listings=[dict(row) for row in rows],
            stats=stats,
            keyword_groups=keyword_groups,
            source_options=[
                {
                    "id": row["source"],
                    "label": SOURCE_LABELS.get(row["source"], row["source"]),
                }
                for row in source_rows
            ],
            currencies=[row["currency"] for row in currency_rows],
            total=total,
            page=page,
            pages=max(1, (total + page_size - 1) // page_size),
            filters={
                "category": category,
                "keyword": keyword,
                "price_status": price_status,
                "currency": currency,
                "price_min": format_price_filter(price_min),
                "price_max": format_price_filter(price_max),
                "source": source,
                "q": search,
                "sort": sort,
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
                    stats.listing_count,
                    stats.sold_count,
                    stats.source_ids,
                    stats.price_summaries
                FROM search_queries q
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(DISTINCT qm.listing_id) AS listing_count,
                        COUNT(DISTINCT qm.listing_id) FILTER (
                            WHERE l.price_status = 'sold'
                        ) AS sold_count,
                        COALESCE(
                            array_remove(array_agg(DISTINCT l.source ORDER BY l.source), NULL),
                            ARRAY[]::text[]
                        ) AS source_ids,
                        COALESCE((
                            SELECT jsonb_agg(
                                jsonb_build_object(
                                    'currency', price.currency,
                                    'count', price.count,
                                    'minimum', price.minimum,
                                    'median', price.median,
                                    'maximum', price.maximum
                                )
                                ORDER BY price.currency
                            )
                            FROM (
                                SELECT
                                    priced_listing.currency,
                                    COUNT(*) AS count,
                                    MIN(priced_listing.price_value) AS minimum,
                                    percentile_cont(0.5) WITHIN GROUP (
                                        ORDER BY priced_listing.price_value
                                    ) AS median,
                                    MAX(priced_listing.price_value) AS maximum
                                FROM market_listing_query_matches priced_match
                                JOIN market_listings priced_listing
                                    ON priced_listing.id = priced_match.listing_id
                                WHERE priced_match.query_id = q.id
                                  AND priced_listing.price_status = 'sold'
                                  AND priced_listing.price_value IS NOT NULL
                                  AND priced_listing.currency <> ''
                                GROUP BY priced_listing.currency
                            ) price
                        ), '[]'::jsonb) AS price_summaries
                    FROM market_listing_query_matches qm
                    JOIN market_listings l ON l.id = qm.listing_id
                    WHERE qm.query_id = q.id
                ) stats ON TRUE
                {where}
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
            scheduled_sources=len(
                {
                    source
                    for query in load_queries()
                    for source in sources_for_category(query["category"])
                }
            ),
            market_pages=max(1, min(5, env_int("MARKET_PAGES_PER_SOURCE", 2))),
            backfill_pages=max(
                1,
                min(5, env_int("MARKET_BACKFILL_PAGES_PER_SOURCE", 2)),
            ),
        )

    @app.get("/deals")
    def deals_redirect() -> Any:
        return redirect(url_for("references"))

    @app.get("/archive/deals")
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
                    best_match.id AS best_match_id,
                    best_match.score AS best_match_score,
                    best_match.visual_score AS best_match_visual_score,
                    best_market.title AS best_market_title,
                    best_market.url AS best_market_url,
                    best_market.price_value AS best_market_price_value,
                    best_market.currency AS best_market_currency,
                    best_market.source AS best_market_source,
                    best_market.price_status AS best_market_price_status,
                    best_market.price_basis AS best_market_price_basis,
                    match_task.status AS image_match_status,
                    array_agg(DISTINCT q.query_text ORDER BY q.query_text) AS query_texts
                FROM deal_listings d
                LEFT JOIN deal_listing_reviews r ON r.listing_id = d.id
                LEFT JOIN image_matches best_match
                    ON best_match.deal_listing_id = d.id
                    AND best_match.rank = 1
                    AND best_match.last_run_id = (
                        SELECT id
                        FROM image_match_runs
                        WHERE status NOT IN ('cancelled', 'failed')
                        ORDER BY id DESC
                        LIMIT 1
                    )
                LEFT JOIN market_listings best_market ON best_market.id = best_match.market_listing_id
                LEFT JOIN image_match_tasks match_task
                    ON match_task.deal_listing_id = d.id
                    AND match_task.run_id = (
                        SELECT id
                        FROM image_match_runs
                        WHERE status NOT IN ('cancelled', 'failed')
                        ORDER BY id DESC
                        LIMIT 1
                    )
                JOIN deal_listing_query_matches qm ON qm.listing_id = d.id
                JOIN search_queries q ON q.id = qm.query_id
                WHERE {where_sql}
                GROUP BY
                    d.id, r.listing_id, r.review_status, r.tags, r.note,
                    best_match.id, best_match.score, best_match.visual_score,
                    best_market.id, match_task.status
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
    def image_review_redirect() -> Any:
        return redirect(url_for("references"))

    @app.get("/archive/image-review")
    def image_review() -> Any:
        category = request.args.get("category", "").strip()
        review_status = request.args.get("status", "").strip()
        minimum_score = request.args.get("min_score", "0.53").strip()
        page = max(1, min(10000, request.args.get("page", 1, type=int)))
        page_size = 24
        try:
            score_value = max(0.0, min(1.0, float(minimum_score)))
        except ValueError:
            score_value = 0.53
        with connection() as conn:
            current_run = conn.execute(
                """
                SELECT id
                FROM image_match_runs
                WHERE status NOT IN ('cancelled', 'failed')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        where = ["match.score >= %s"]
        params: list[Any] = [score_value]
        if current_run:
            where.append("match.last_run_id = %s")
            params.append(current_run["id"])
        else:
            where.append("FALSE")
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
            stats = image_match_stats(conn, int(current_run["id"]) if current_run else None)
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

    @app.post("/api/runs/source-pilot")
    @json_endpoint
    def start_source_pilot() -> Any:
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

            rows = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE enabled AND id = ANY(%s)
                """,
                (list(EXTERNAL_PILOT_QUERY_IDS),),
            ).fetchall()
            available_ids = {row["id"] for row in rows}
            missing_ids = [
                query_id
                for query_id in EXTERNAL_PILOT_QUERY_IDS
                if query_id not in available_ids
            ]
            if missing_ids:
                return jsonify(
                    {"error": f"Pilot-Suchen fehlen: {', '.join(missing_ids)}"}
                ), 409

            tasks = [
                (query_id, source)
                for query_id in EXTERNAL_PILOT_QUERY_IDS
                for source in EXTERNAL_PILOT_SOURCES
            ]
            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('source_pilot', %s)
                RETURNING id
                """,
                (len(tasks),),
            ).fetchone()
            for query_id, source in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, 1, 1)
                    """,
                    (run["id"], query_id, source),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "source_pilot",
                "planned_queries": len(EXTERNAL_PILOT_QUERY_IDS),
                "planned_tasks": len(tasks),
                "pages_per_task": 1,
            }
        ), 201

    @app.post("/api/runs/meissen-backfill")
    @json_endpoint
    def start_meissen_backfill() -> Any:
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

            query = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE id = %s AND enabled
                """,
                (MEISSEN_ARCHIVE_QUERY_ID,),
            ).fetchone()
            if not query:
                return jsonify({"error": "Die Meissen-Suche ist nicht aktiv."}), 409

            cursor = conn.execute(
                """
                SELECT next_page
                FROM market_backfill_cursors
                WHERE query_id = %s AND source = %s
                """,
                (MEISSEN_ARCHIVE_QUERY_ID, MEISSEN_ARCHIVE_SOURCE),
            ).fetchone()
            start_page = max(
                MEISSEN_ARCHIVE_START_PAGE,
                int(cursor["next_page"]) if cursor else MEISSEN_ARCHIVE_START_PAGE,
            )
            if start_page > MEISSEN_ARCHIVE_TARGET_PAGE:
                return jsonify({"error": "Das Meißen-Archiv bis Seite 200 ist bereits eingelesen."}), 409

            page_count = MEISSEN_ARCHIVE_TARGET_PAGE - start_page + 1
            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('backfill', 1)
                RETURNING id
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO market_run_tasks (
                    run_id, query_id, source, start_page, page_count
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    run["id"],
                    MEISSEN_ARCHIVE_QUERY_ID,
                    MEISSEN_ARCHIVE_SOURCE,
                    start_page,
                    page_count,
                ),
            )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "backfill",
                "planned_queries": 1,
                "planned_tasks": 1,
                "start_page": start_page,
                "end_page": MEISSEN_ARCHIVE_TARGET_PAGE,
            }
        ), 201

    @app.post("/api/runs/meissen-porcelain-pilot")
    @json_endpoint
    def start_meissen_porcelain_pilot() -> Any:
        force_refresh = request.args.get("refresh") == "1"
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

            query = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE id = %s AND enabled
                """,
                (MEISSEN_ARCHIVE_QUERY_ID,),
            ).fetchone()
            if not query:
                return jsonify({"error": "Die Meissen-Suche ist nicht aktiv."}), 409

            tasks: list[tuple[str, int, int]] = []
            for source in MEISSEN_PORCELAIN_PILOT_SOURCES:
                completed_result = conn.execute(
                    """
                    SELECT 1
                    FROM market_run_tasks
                    WHERE query_id = %s
                      AND source = %s
                      AND status = 'completed'
                      AND unique_count > 0
                    LIMIT 1
                    """,
                    (MEISSEN_ARCHIVE_QUERY_ID, source),
                ).fetchone()
                if completed_result and not force_refresh:
                    continue
                cursor = conn.execute(
                    """
                    SELECT next_page, exhausted
                    FROM market_backfill_cursors
                    WHERE query_id = %s AND source = %s
                    """,
                    (MEISSEN_ARCHIVE_QUERY_ID, source),
                ).fetchone()
                if cursor and cursor["exhausted"] and not force_refresh:
                    continue
                start_page = (
                    1
                    if force_refresh
                    else max(1, int(cursor["next_page"]) if cursor else 1)
                )
                page_count = min(
                    MEISSEN_PORCELAIN_PILOT_PAGE_COUNTS[source],
                    SOURCE_MAX_PAGES[source] - start_page + 1,
                )
                if page_count > 0:
                    tasks.append((source, start_page, page_count))

            if not tasks:
                return jsonify(
                    {"error": "Die neuen Meißen-Porzellanquellen sind bereits getestet."}
                ), 409

            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('source_pilot', %s)
                RETURNING id
                """,
                (len(tasks),),
            ).fetchone()
            for source, start_page, page_count in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        run["id"],
                        MEISSEN_ARCHIVE_QUERY_ID,
                        source,
                        start_page,
                        page_count,
                    ),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "source_pilot",
                "planned_queries": 1,
                "planned_tasks": len(tasks),
                "sources": [source for source, _start, _pages in tasks],
            }
        ), 201

    @app.post("/api/runs/meissen-deal-source-pilot")
    @json_endpoint
    def start_meissen_deal_source_pilot() -> Any:
        body = request.get_json(silent=True) or {}
        requested_sources = body.get("sources", list(MEISSEN_DEAL_PILOT_SOURCES))
        if not isinstance(requested_sources, list):
            return jsonify({"error": "sources muss eine Liste sein."}), 400
        sources = tuple(dict.fromkeys(str(source) for source in requested_sources))
        invalid_sources = [
            source for source in sources if source not in MEISSEN_DEAL_PILOT_SOURCES
        ]
        if not sources or invalid_sources:
            return jsonify({"error": "Unzulaessige Pilotquelle."}), 400
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

            query = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE id = %s AND enabled
                """,
                (MEISSEN_ARCHIVE_QUERY_ID,),
            ).fetchone()
            if not query:
                return jsonify({"error": "Die Meissen-Suche ist nicht aktiv."}), 409

            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('source_pilot', %s)
                RETURNING id
                """,
                (len(sources),),
            ).fetchone()
            for source in sources:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, 1, 1)
                    """,
                    (run["id"], MEISSEN_ARCHIVE_QUERY_ID, source),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "source_pilot",
                "planned_queries": 1,
                "planned_tasks": len(sources),
                "sources": list(sources),
            }
        ), 201

    @app.post("/api/runs/meissen-porcelain-backfill")
    @json_endpoint
    def start_meissen_porcelain_backfill() -> Any:
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

            query = conn.execute(
                """
                SELECT id
                FROM search_queries
                WHERE id = %s AND enabled
                """,
                (MEISSEN_ARCHIVE_QUERY_ID,),
            ).fetchone()
            if not query:
                return jsonify({"error": "Die Meissen-Suche ist nicht aktiv."}), 409

            tasks: list[tuple[str, int, int]] = []
            for source in MEISSEN_PORCELAIN_BACKFILL_SOURCES:
                cursor = conn.execute(
                    """
                    SELECT next_page
                    FROM market_backfill_cursors
                    WHERE query_id = %s AND source = %s
                    """,
                    (MEISSEN_ARCHIVE_QUERY_ID, source),
                ).fetchone()
                start_page = max(1, int(cursor["next_page"]) if cursor else 1)
                if start_page > SOURCE_MAX_PAGES[source]:
                    continue
                page_count = min(
                    MEISSEN_PORCELAIN_BACKFILL_BATCH_PAGES,
                    SOURCE_MAX_PAGES[source] - start_page + 1,
                )
                tasks.append((source, start_page, page_count))

            if not tasks:
                return jsonify({"error": "Die Meissen-Quellenarchive sind bereits eingelesen."}), 409

            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('backfill', %s)
                RETURNING id
                """,
                (len(tasks),),
            ).fetchone()
            for source, start_page, page_count in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        run["id"],
                        MEISSEN_ARCHIVE_QUERY_ID,
                        source,
                        start_page,
                        page_count,
                    ),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "backfill",
                "planned_queries": 1,
                "planned_tasks": len(tasks),
                "sources": [source for source, _start, _pages in tasks],
            }
        ), 201

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
            refresh_pages = max(
                1,
                min(5, env_int("MARKET_PAGES_PER_SOURCE", 2)),
            )
            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('refresh', %s)
                RETURNING id
                """,
                (len(tasks),),
            ).fetchone()
            for query_id, source in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, 1, %s)
                    """,
                    (run["id"], query_id, source, refresh_pages),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "refresh",
                "planned_queries": len(query_rows),
                "planned_tasks": len(tasks),
                "pages_per_task": refresh_pages,
            }
        ), 201

    @app.post("/api/runs/backfill")
    @json_endpoint
    def start_backfill_run() -> Any:
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

            refresh_pages = max(
                1,
                min(5, env_int("MARKET_PAGES_PER_SOURCE", 2)),
            )
            backfill_pages = max(
                1,
                min(5, env_int("MARKET_BACKFILL_PAGES_PER_SOURCE", 2)),
            )
            source_tasks = [
                (query_row["id"], source)
                for query_row in query_rows
                for source in sources_for_category(query_row["category"])
            ]
            for query_id, source in source_tasks:
                conn.execute(
                    """
                    INSERT INTO market_backfill_cursors (query_id, source, next_page)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (query_id, source) DO NOTHING
                    """,
                    (query_id, source, refresh_pages + 1),
                )
            cursor_rows = conn.execute(
                """
                SELECT query_id, source, next_page
                FROM market_backfill_cursors
                WHERE NOT exhausted
                """
            ).fetchall()
            cursors = {
                (row["query_id"], row["source"]): int(row["next_page"])
                for row in cursor_rows
            }
            tasks = [
                (query_id, source, cursors[(query_id, source)], backfill_pages)
                for query_id, source in source_tasks
                if (query_id, source) in cursors
            ]
            if not tasks:
                return jsonify({"error": "Alle freigegebenen Archive sind ausgeschöpft."}), 409

            run = conn.execute(
                """
                INSERT INTO market_runs (kind, planned_tasks)
                VALUES ('backfill', %s)
                RETURNING id
                """,
                (len(tasks),),
            ).fetchone()
            for query_id, source, start_page, page_count in tasks:
                conn.execute(
                    """
                    INSERT INTO market_run_tasks (
                        run_id, query_id, source, start_page, page_count
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (run["id"], query_id, source, start_page, page_count),
                )
        return jsonify(
            {
                "ok": True,
                "run_id": run["id"],
                "kind": "backfill",
                "planned_queries": len(query_rows),
                "planned_tasks": len(tasks),
                "pages_per_task": backfill_pages,
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


def market_stats(
    conn: Any,
    where_sql: str = "TRUE",
    params: list[Any] | None = None,
) -> dict[str, int]:
    active_params = params or []
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE l.price_status = 'sold') AS sold,
            COUNT(*) FILTER (WHERE l.price_status = 'ask') AS ask,
            COUNT(*) FILTER (WHERE l.price_status = 'current_bid') AS current_bid,
            COUNT(*) FILTER (WHERE l.price_status = 'estimate') AS estimate,
            COUNT(*) FILTER (WHERE l.price_status = 'unknown') AS unknown
        FROM market_listings l
        WHERE {where_sql}
        """,
        active_params,
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


def image_match_stats(conn: Any, run_id: int | None) -> dict[str, int]:
    where = "WHERE last_run_id = %s" if run_id is not None else "WHERE FALSE"
    params: list[Any] = [run_id] if run_id is not None else []
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE score >= 0.70) AS strong,
            COUNT(DISTINCT deal_listing_id) AS deals,
            COUNT(*) FILTER (WHERE review_status = 'candidate') AS candidates,
            COUNT(*) FILTER (WHERE review_status = 'checked') AS checked,
            COUNT(*) FILTER (WHERE review_status = 'skip') AS skipped
        FROM image_matches
        {where}
        """,
        params,
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
