from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from klein_antik.serpapi import (
    SerpApiError,
    is_sold_snippet,
    normalized_result,
    parse_price,
    product_id_for,
    relevant_to_query,
    search_sold,
    sold_candidates,
    sold_date_text,
    sold_price,
)


class SerpApiParsingTests(unittest.TestCase):
    def test_product_id_prefers_structured_value(self) -> None:
        self.assertEqual(product_id_for({"product_id": "12345"}), "12345")

    def test_product_id_falls_back_to_listing_url(self) -> None:
        self.assertEqual(
            product_id_for({"link": "https://www.ebay.de/itm/example/123456789012"}),
            "123456789012",
        )

    def test_german_price_is_parsed(self) -> None:
        amount, raw, currency = parse_price("EUR 1.234,50")
        self.assertEqual(amount, Decimal("1234.50"))
        self.assertEqual(raw, "EUR 1.234,50")
        self.assertEqual(currency, "EUR")

    def test_price_range_uses_lower_bound(self) -> None:
        amount, raw, _currency = parse_price(
            {"from": {"raw": "$10.00", "extracted": 10}, "to": {"raw": "$20.00", "extracted": 20}}
        )
        self.assertEqual(amount, Decimal("10"))
        self.assertEqual(raw, "$10.00")
        self.assertEqual(_currency, "USD")

    def test_result_normalization_keeps_review_fields_separate(self) -> None:
        item = normalized_result(
            {
                "product_id": "123",
                "title": "Orrefors Vase",
                "link": "https://example.test/123",
                "price": {"raw": "EUR 45,00", "extracted": 45},
                "condition": "Gebraucht",
                "thumbnail": "https://example.test/image.jpg",
            }
        )
        self.assertEqual(item["product_id"], "123")
        self.assertEqual(item["price_value"], Decimal("45"))
        self.assertNotIn("content_status", item)

    @patch("klein_antik.serpapi.requests.get")
    def test_search_discovers_sold_page_and_enriches_one_result(self, get: Mock) -> None:
        discovery = Mock(ok=True, status_code=200)
        discovery.json.return_value = {
            "search_metadata": {"status": "Success", "id": "search-1"},
            "organic_results": [
                {
                    "title": "Meissen Porzellanfigur | eBay.de",
                    "link": "https://www.ebay.de/itm/123456789012",
                    "snippet": (
                        "Dieses Angebot wurde verkauft am Fr, 10. Jul um 09:20. "
                        "Meissen Porzellanfigur Verkauft EUR 120,00"
                    ),
                }
            ],
        }
        detail = Mock(ok=True, status_code=200)
        detail.json.return_value = {
            "search_metadata": {"status": "Success"},
            "product_results": {
                "title": "Meissen Porzellanfigur",
                "condition": "Gebraucht",
                "shipping": {"from": "Dresden, Deutschland"},
                "media": [
                    {
                        "type": "image",
                        "image": [
                            {
                                "link": "https://i.ebayimg.com/image.jpg",
                                "size": {"width": 960},
                            }
                        ],
                    }
                ],
            },
        }
        get.side_effect = [discovery, detail]

        result = search_sold(api_key="secret", query="Meissen", ebay_domain="ebay.de")

        self.assertEqual(get.call_count, 2)
        discovery_params = get.call_args_list[0].kwargs["params"]
        self.assertEqual(discovery_params["engine"], "duckduckgo")
        self.assertIn('site:ebay.de/itm "Dieses Angebot wurde verkauft am"', discovery_params["q"])
        detail_params = get.call_args_list[1].kwargs["params"]
        self.assertEqual(detail_params["engine"], "ebay_product")
        self.assertEqual(detail_params["product_id"], "123456789012")
        self.assertEqual(result["_discovery_calls_used"], 1)
        self.assertEqual(result["_detail_calls_used"], 1)
        self.assertEqual(result["organic_results"][0]["condition"], "Gebraucht")
        self.assertEqual(
            result["organic_results"][0]["thumbnail"],
            "https://i.ebayimg.com/image.jpg",
        )

    @patch("klein_antik.serpapi.requests.get")
    def test_search_without_candidate_uses_only_discovery_call(self, get: Mock) -> None:
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "search_metadata": {"status": "Success"},
            "organic_results": [],
        }
        get.return_value = response

        result = search_sold(
            api_key="secret",
            query="Miriam Haskell",
            ebay_domain="ebay.com",
        )

        self.assertEqual(get.call_count, 1)
        self.assertIn(
            'site:ebay.com/itm "This listing sold on"',
            get.call_args.kwargs["params"]["q"],
        )
        self.assertEqual(result["_detail_calls_used"], 0)

    def test_sold_markers_and_prices_cover_pilot_domains(self) -> None:
        self.assertTrue(is_sold_snippet("This listing sold on Fri, Jul 10. US $43.99"))
        self.assertTrue(is_sold_snippet("Cet objet a été vendu le 10 juil. EUR 80,00"))
        self.assertEqual(sold_price("This listing sold on Fri. US $43.99"), "US $43.99")
        self.assertEqual(sold_price("This listing sold on Fri. £120.00"), "£120.00")
        self.assertEqual(sold_price("Verkauft EUR 1.234,50"), "EUR 1.234,50")
        self.assertEqual(
            sold_date_text("This listing sold on Fri, Jul 10 at 9:20 AM. Item"),
            "Fri, Jul 10 at 9:20 AM",
        )

    def test_candidate_filter_rejects_unrelated_search_result(self) -> None:
        results = sold_candidates(
            query="Meissen",
            ebay_domain="ebay.de",
            results=[
                {
                    "title": "Panini Sticker | eBay.de",
                    "link": "https://www.ebay.de/itm/111111111111",
                    "snippet": "Dieses Angebot wurde verkauft am Freitag. EUR 1,00",
                },
                {
                    "title": "Meissen Schale | eBay.de",
                    "link": "https://www.ebay.de/itm/222222222222",
                    "snippet": "Dieses Angebot wurde verkauft am Freitag. EUR 80,00",
                },
            ],
        )
        self.assertEqual([item["product_id"] for item in results], ["222222222222"])

    def test_relevance_requires_specific_brand_signal(self) -> None:
        self.assertFalse(
            relevant_to_query(
                "Orrefors signed numbered vase",
                "Signed numbered glass vase by unknown maker",
            )
        )
        self.assertTrue(
            relevant_to_query(
                "Orrefors signed numbered vase",
                "Orrefors signed glass vase",
            )
        )

    @patch("klein_antik.serpapi.requests.get")
    def test_http_error_does_not_include_api_key(self, get: Mock) -> None:
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.json.return_value = {"error": "Unsupported request."}
        get.return_value = response

        with self.assertRaises(SerpApiError) as error:
            search_sold(api_key="private-key", query="Meissen", ebay_domain="ebay.de")

        self.assertNotIn("private-key", str(error.exception))
        self.assertEqual(
            str(error.exception),
            "SerpApi HTTP 400: Unsupported request.",
        )


if __name__ == "__main__":
    unittest.main()
