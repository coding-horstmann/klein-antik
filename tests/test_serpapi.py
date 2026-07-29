from __future__ import annotations

import unittest
from decimal import Decimal

from klein_antik.serpapi import normalized_result, parse_price, product_id_for


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


if __name__ == "__main__":
    unittest.main()
