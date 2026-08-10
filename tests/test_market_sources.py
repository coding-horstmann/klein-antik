from __future__ import annotations

import html
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from klein_antik.catalog import load_queries
from klein_antik.market_sources import (
    MEISSEN_ARCHIVE_RESULT_LIMIT,
    MEISSEN_ARCHIVE_SOURCE,
    MEISSEN_ARCHIVE_START_PAGE,
    MEISSEN_ARCHIVE_TARGET_PAGE,
    SOURCE_MAX_PAGES,
    SOURCE_PAGE_SIZES,
    collect,
    collect_batch,
    _external_request_headers,
    parse_money,
    relevant_to_query,
    search_query_for,
    sources_for_category,
)
from klein_antik.price_filters import format_price_filter, parse_price_filter


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs.get("params")))
        return FakeResponse(self.responses.pop(0))


class MarketSourceTests(unittest.TestCase):
    def test_price_filter_parses_german_decimal_input(self) -> None:
        self.assertEqual(parse_price_filter("125,50"), Decimal("125.50"))
        self.assertEqual(format_price_filter(Decimal("125.50")), "125.50")
        self.assertIsNone(parse_price_filter("-1"))
        self.assertIsNone(parse_price_filter("keine zahl"))

    def test_source_matrix_has_157_tasks(self) -> None:
        task_count = sum(
            len(sources_for_category(query["category"])) for query in load_queries()
        )
        self.assertEqual(task_count, 157)

    def test_meissen_archive_backfill_has_a_bounded_deep_range(self) -> None:
        self.assertEqual(MEISSEN_ARCHIVE_SOURCE, "auctionet")
        self.assertEqual(MEISSEN_ARCHIVE_START_PAGE, 6)
        self.assertEqual(MEISSEN_ARCHIVE_TARGET_PAGE, 100)
        self.assertEqual(SOURCE_MAX_PAGES[MEISSEN_ARCHIVE_SOURCE], 100)
        self.assertEqual(
            MEISSEN_ARCHIVE_RESULT_LIMIT,
            (MEISSEN_ARCHIVE_TARGET_PAGE - MEISSEN_ARCHIVE_START_PAGE + 1)
            * SOURCE_PAGE_SIZES[MEISSEN_ARCHIVE_SOURCE],
        )

    def test_money_formats(self) -> None:
        self.assertEqual(parse_money("EUR 1.234,50"), (Decimal("1234.50"), "EUR"))
        self.assertEqual(parse_money("DKK 14,000"), (Decimal("14000"), "DKK"))
        self.assertEqual(parse_money("SEK 1 200"), (Decimal("1200"), "SEK"))
        self.assertEqual(parse_money("EUR 1,250"), (Decimal("1250"), "EUR"))
        self.assertEqual(parse_money("Estimate only"), (None, ""))

    def test_external_source_collectors_keep_listing_and_price_details(self) -> None:
        fixtures = {
            "liveauctioneers": (
                '<article><a href="/item/101-georg-jensen-pendant">Georg Jensen silver pendant</a>'
                '<img src="/images/101.jpg"><span>Price Realized: USD 1,250</span></article>',
                "sold",
                Decimal("1250"),
                "USD",
            ),
            "invaluable": (
                '<article><a href="/auction-lot/georg-jensen-ring-7-c-a123">Georg Jensen silver ring</a>'
                '<img data-src="/images/102.jpg"><span>Estimate: EUR 700</span></article>',
                "estimate",
                Decimal("700"),
                "EUR",
            ),
            "christies": (
                '<article><a href="/en/lot/lot-555">Georg Jensen silver bowl</a>'
                '<span>Hammer Price: GBP 2,000</span></article>',
                "sold",
                Decimal("2000"),
                "GBP",
            ),
            "heritage": (
                '<article><a href="/itm/silver/georg-jensen-brooch/12345.s">Georg Jensen brooch</a>'
                '<span>Sold for: USD 900</span></article>',
                "sold",
                Decimal("900"),
                "USD",
            ),
        }
        for source, (page, status, amount, currency) in fixtures.items():
            with self.subTest(source=source):
                results = collect(
                    source,
                    "Georg Jensen Silber",
                    session=FakeSession([page]),
                    limit=30,
                )
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["price_status"], status)
                self.assertEqual(results[0]["price_value"], amount)
                self.assertEqual(results[0]["currency"], currency)
                self.assertTrue(results[0]["url"].startswith("https://"))

    def test_external_source_auth_headers_are_isolated_per_source(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MARKET_HERITAGE_COOKIE": "session=approved",
                "MARKET_HERITAGE_AUTHORIZATION": "Bearer approved-token",
                "MARKET_CHRISTIES_COOKIE": "session=christies",
            },
            clear=True,
        ):
            self.assertEqual(
                _external_request_headers("heritage"),
                {
                    "Cookie": "session=approved",
                    "Authorization": "Bearer approved-token",
                },
            )
            self.assertEqual(
                _external_request_headers("christies"),
                {"Cookie": "session=christies"},
            )
            self.assertEqual(_external_request_headers("invaluable"), {})

    def test_relevance_rejects_auction_search_noise(self) -> None:
        furniture = {
            "title": "Erik Chambert cabinet, Norrköping 1945",
            "raw_result": {},
        }
        jewelry = {
            "title": "Schreiner New York rhinestone brooch",
            "raw_result": {},
        }
        self.assertFalse(relevant_to_query("Schreiner New York Schmuck", furniture))
        self.assertTrue(relevant_to_query("Schreiner New York Schmuck", jewelry))
        self.assertTrue(
            relevant_to_query(
                "Crown Trifari",
                {"title": "Trifari vintage floral brooch", "raw_result": {}},
            )
        )

    def test_broad_signed_jewelry_requires_signature_or_designer_signal(self) -> None:
        query = "Signierter Vintage-Designerschmuck"
        self.assertFalse(
            relevant_to_query(
                query,
                {"title": "JEWELRY, 2 pcs. Brooch and necklace", "raw_result": {}},
            )
        )
        self.assertTrue(
            relevant_to_query(
                query,
                {"title": "Signed Trifari floral brooch", "raw_result": {}},
            )
        )
        self.assertTrue(
            relevant_to_query(
                query,
                {"title": "Bjorn Weckstrom bronze ring", "raw_result": {}},
            )
        )

    def test_relevance_handles_maker_and_designer_queries(self) -> None:
        result = {
            "title": "Tapio Wirkkala for Iittala art glass vase",
            "raw_result": {},
        }
        self.assertTrue(relevant_to_query("Iittala Tapio Wirkkala signed", result))
        self.assertFalse(
            relevant_to_query(
                "Iittala Tapio Wirkkala signed",
                {"title": "Unknown Swedish glass vase", "raw_result": {}},
            )
        )

    def test_source_search_strips_local_descriptors(self) -> None:
        self.assertEqual(search_query_for("WMF Ikora vintage"), "wmf ikora")
        self.assertEqual(
            search_query_for("Kay Bojesen Figur vintage"),
            "kay bojesen figure",
        )
        self.assertEqual(search_query_for("N.E. From Silber"), "N.E. From")
        self.assertEqual(
            search_query_for("Osiris Jugendstil Zinn"),
            "Osiris pewter",
        )
        self.assertEqual(
            search_query_for("Bing & Grøndahl Figur"),
            "Bing Grondahl",
        )
        self.assertEqual(
            search_query_for("WMF Jugendstil Karaffe"),
            "wmf art nouveau carafe",
        )
        self.assertEqual(
            search_query_for("WMF Jugendstil Karaffe", source="quittenbaum"),
            "wmf carafe",
        )

    def test_object_filter_keeps_query_intent(self) -> None:
        self.assertTrue(
            relevant_to_query(
                "Royal Copenhagen Figur",
                {"title": "Royal Copenhagen porcelain figurine", "raw_result": {}},
            )
        )
        self.assertFalse(
            relevant_to_query(
                "Royal Copenhagen Figur",
                {"title": "Royal Copenhagen dinner plate", "raw_result": {}},
            )
        )

    def test_collect_filters_irrelevant_auctionet_results(self) -> None:
        payload = {
            "items": [
                {
                    "id": 91,
                    "shortTitle": "Erik Chambert cabinet",
                    "url": "/en/91-cabinet",
                    "amountValue": "2,000 SEK",
                    "hasMetReserve": True,
                },
                {
                    "id": 92,
                    "shortTitle": "Schreiner New York brooch",
                    "url": "/en/92-brooch",
                    "amountValue": "600 SEK",
                    "hasMetReserve": True,
                },
            ]
        }
        page = (
            '<div data-react-props="'
            + html.escape(json.dumps(payload), quote=True)
            + '"></div>'
        )
        results = collect(
            "auctionet",
            "Schreiner New York Schmuck",
            session=FakeSession([page]),
            limit=30,
        )
        self.assertEqual([result["source_item_id"] for result in results], ["92"])

    def test_auctionet_sold_and_unsold_results(self) -> None:
        payload = {
            "items": [
                {
                    "id": 41,
                    "shortTitle": "WMF Jardiniere",
                    "url": "/en/41-wmf-jardiniere",
                    "mainImageUrl": "https://images.example/41.jpg",
                    "amountValue": "1,200 SEK",
                    "amountTitle": "Hammer price",
                    "hasMetReserve": True,
                    "auctionEndTime": "2026-08-01",
                },
                {
                    "id": 42,
                    "shortTitle": "WMF vase",
                    "url": "/en/42-wmf-vase",
                    "amountValue": "800 SEK",
                    "amountTitle": "Reserve not met",
                    "hasMetReserve": False,
                },
            ]
        }
        page = (
            '<div data-react-props="'
            + html.escape(json.dumps(payload), quote=True)
            + '"></div>'
        )
        results = collect(
            "auctionet", "WMF", session=FakeSession([page]), limit=30
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["price_status"], "sold")
        self.assertEqual(results[0]["price_value"], Decimal("1200"))
        self.assertEqual(results[1]["price_status"], "unsold")
        self.assertIsNone(results[1]["price_value"])

    def test_auctionet_can_collect_a_second_result_page(self) -> None:
        first_payload = {
            "items": [
                {
                    "id": item_id,
                    "shortTitle": f"Meissen plate {item_id}",
                    "url": f"/en/{item_id}-meissen-plate",
                    "amountValue": "100 SEK",
                    "hasMetReserve": True,
                }
                for item_id in range(1, 49)
            ]
        }
        second_payload = {
            "items": [
                {
                    "id": item_id,
                    "shortTitle": f"Meissen plate {item_id}",
                    "url": f"/en/{item_id}-meissen-plate",
                    "amountValue": "100 SEK",
                    "hasMetReserve": True,
                }
                for item_id in range(49, 53)
            ]
        }
        pages = [
            '<div data-react-props="'
            + html.escape(json.dumps(payload), quote=True)
            + '"></div>'
            for payload in (first_payload, second_payload)
        ]
        session = FakeSession(pages)

        results = collect(
            "auctionet",
            "Meissen",
            session=session,
            limit=96,
            max_pages=2,
        )

        self.assertEqual(len(results), 52)
        self.assertEqual([call[1]["page"] for call in session.calls], [1, 2])

    def test_backfill_batch_starts_at_cursor_and_marks_short_page_exhausted(self) -> None:
        full_payload = {
            "items": [
                {
                    "id": item_id,
                    "shortTitle": f"Meissen plate {item_id}",
                    "url": f"/en/{item_id}-meissen-plate",
                    "amountValue": "100 SEK",
                    "hasMetReserve": True,
                }
                for item_id in range(201, 249)
            ]
        }
        short_payload = {
            "items": [
                {
                    "id": item_id,
                    "shortTitle": f"Meissen plate {item_id}",
                    "url": f"/en/{item_id}-meissen-plate",
                    "amountValue": "100 SEK",
                    "hasMetReserve": True,
                }
                for item_id in range(249, 252)
            ]
        }
        pages = [
            '<div data-react-props="'
            + html.escape(json.dumps(payload), quote=True)
            + '"></div>'
            for payload in (full_payload, short_payload)
        ]
        session = FakeSession(pages)

        batch = collect_batch(
            "auctionet",
            "Meissen",
            session=session,
            limit=96,
            start_page=3,
            page_count=2,
        )

        self.assertEqual(len(batch.results), 51)
        self.assertEqual(batch.pages_fetched, 2)
        self.assertTrue(batch.exhausted)
        self.assertEqual([call[1]["page"] for call in session.calls], [3, 4])

    def test_quittenbaum_preserves_hammer_price(self) -> None:
        page = """
        <ul>
          <li class="auction-object" id="lot-52">
            <a href="/en/auction/lot/wmf-vase"><img src="/images/52.jpg"></a>
            <div class="manufacture">WMF</div>
            <h2 class="auction-object-title">Ikora vase</h2>
            <div class="auction-object-price">Hammer Price: EUR 850</div>
          </li>
        </ul>
        """
        results = collect(
            "quittenbaum", "WMF Ikora", session=FakeSession([page]), limit=30
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["price_status"], "sold")
        self.assertEqual(results[0]["price_basis"], "hammer")
        self.assertEqual(results[0]["price_value"], Decimal("850"))

    def test_bruun_rasmussen_preserves_realised_price(self) -> None:
        page = """
        <ul>
          <li id="lot_77">
            <a class="lot-list-item" href="/m/lots/77">
              <img src="//images.example/77.jpg">
              <p class="description">Kay Bojesen wooden figure</p>
              <p>Estimate <currency-amount amount="2000" currency="DKK"></currency-amount></p>
              <p>Price realised <currency-amount amount="3200" currency="DKK"></currency-amount></p>
            </a>
          </li>
        </ul>
        """
        results = collect(
            "bruun_rasmussen", "Kay Bojesen", session=FakeSession([page]), limit=30
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["price_status"], "sold")
        self.assertEqual(results[0]["price_basis"], "realised")
        self.assertEqual(results[0]["price_value"], Decimal("3200"))
        self.assertEqual(results[0]["image_url"], "https://images.example/77.jpg")

    def test_lempertz_marks_premium_included(self) -> None:
        search_page = """
        <article class="result-list-item-type-lempertz_lot">
          <div class="result-title"><a href="/en/catalogues/lot/1234.html">Lot 18 Meissen figure</a></div>
          <div class="result-preview-img"><img src="/images/1234.jpg"></div>
          <div>Estimate: EUR 1,000 - 1,500</div>
        </article>
        """
        detail_page = """
        <div class="lot-price-wrapper">
          <div class="lot-price-label">Result</div>
          <div class="lot-price">EUR 2,250 (incl. premium)</div>
        </div>
        """
        session = FakeSession([search_page, detail_page])
        results = collect("lempertz", "Meissen", session=session, limit=30)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Meissen figure")
        self.assertEqual(results[0]["price_value"], Decimal("2250"))
        self.assertEqual(results[0]["price_basis"], "premium_included")
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
