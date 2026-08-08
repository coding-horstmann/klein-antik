from __future__ import annotations

import html
import json
import unittest
from decimal import Decimal

from klein_antik.catalog import load_queries
from klein_antik.market_sources import (
    collect,
    parse_money,
    relevant_to_query,
    search_query_for,
    sources_for_category,
)


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
    def test_source_matrix_has_238_tasks(self) -> None:
        task_count = sum(
            len(sources_for_category(query["category"])) for query in load_queries()
        )
        self.assertEqual(task_count, 238)

    def test_money_formats(self) -> None:
        self.assertEqual(parse_money("EUR 1.234,50"), (Decimal("1234.50"), "EUR"))
        self.assertEqual(parse_money("DKK 14,000"), (Decimal("14000"), "DKK"))
        self.assertEqual(parse_money("SEK 1 200"), (Decimal("1200"), "SEK"))
        self.assertEqual(parse_money("Estimate only"), (None, ""))

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
