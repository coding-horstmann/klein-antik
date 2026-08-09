from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from klein_antik.ebay_active import _search_params, collect, credentials_configured


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.post_kwargs: dict[str, object] = {}
        self.get_kwargs: dict[str, object] = {}

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, _url: str, **kwargs: object) -> FakeResponse:
        self.post_kwargs = kwargs
        return FakeResponse({"access_token": "test-token"})

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        self.get_kwargs = kwargs
        return FakeResponse(
            {
                "itemSummaries": [
                    {
                        "itemId": "v1|123456789|0",
                        "legacyItemId": "123456789",
                        "itemWebUrl": "https://www.ebay.de/itm/123456789",
                        "title": "Miriam Haskell Vintage Brosche signiert",
                        "price": {"value": "125.00", "currency": "EUR"},
                        "image": {"imageUrl": "https://i.ebayimg.com/images/g/test/s-l1600.jpg"},
                        "condition": "Gebraucht",
                        "seller": {"username": "private-seller", "feedbackScore": 12},
                        "buyingOptions": ["FIXED_PRICE"],
                    }
                ]
            }
        )


class EbayActiveTests(unittest.TestCase):
    def test_search_params_limit_and_private_seller_filter(self) -> None:
        self.assertEqual(
            _search_params("Miriam Haskell", "281", 200),
            {
                "q": "Miriam Haskell",
                "limit": "50",
                "filter": "sellerAccountTypes:{INDIVIDUAL}",
                "category_ids": "281",
            },
        )

    def test_credentials_require_both_values(self) -> None:
        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "", "EBAY_CLIENT_SECRET": "secret"}, clear=False):
            self.assertFalse(credentials_configured())

    def test_collect_uses_official_browse_api_and_keeps_listing_fields(self) -> None:
        session = FakeSession()
        with patch.dict(
            os.environ,
            {"EBAY_CLIENT_ID": "client", "EBAY_CLIENT_SECRET": "secret"},
            clear=False,
        ), patch("klein_antik.ebay_active.requests.Session", return_value=session):
            results = collect("Miriam Haskell", None, limit=80)

        self.assertEqual(session.post_kwargs["data"], {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        })
        self.assertEqual(session.get_kwargs["params"], {
            "q": "Miriam Haskell",
            "limit": "50",
            "filter": "sellerAccountTypes:{INDIVIDUAL}",
        })
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "ebay_active")
        self.assertEqual(results[0]["source_item_id"], "v1|123456789|0")
        self.assertEqual(str(results[0]["price_value"]), "125.00")
        self.assertEqual(results[0]["currency"], "EUR")
        self.assertEqual(results[0]["seller_account_type"], "individual")


if __name__ == "__main__":
    unittest.main()
