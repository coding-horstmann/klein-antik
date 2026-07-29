from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from klein_antik.serpapi import (
    SerpApiError,
    normalized_result,
    parse_price,
    product_id_for,
    search_sold,
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
    def test_search_uses_numeric_domestic_location(self, get: Mock) -> None:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {"search_metadata": {"status": "Success"}}
        get.return_value = response

        search_sold(api_key="secret", query="Meissen", ebay_domain="ebay.de")

        self.assertEqual(get.call_args.kwargs["params"]["LH_PrefLoc"], "1")

    @patch("klein_antik.serpapi.requests.get")
    def test_http_error_does_not_include_api_key(self, get: Mock) -> None:
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.json.return_value = {"error": "Unsupported location."}
        get.return_value = response

        with self.assertRaises(SerpApiError) as error:
            search_sold(api_key="private-key", query="Meissen", ebay_domain="ebay.de")

        self.assertNotIn("private-key", str(error.exception))
        self.assertEqual(
            str(error.exception),
            "SerpApi HTTP 400: Unsupported location.",
        )


if __name__ == "__main__":
    unittest.main()
