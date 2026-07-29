from __future__ import annotations

import unittest

from klein_antik.catalog import EXPECTED_QUERY_COUNT, category_options, load_queries


class CatalogTests(unittest.TestCase):
    def test_pilot_has_exactly_110_unique_queries(self) -> None:
        queries = load_queries()
        self.assertEqual(len(queries), EXPECTED_QUERY_COUNT)
        self.assertEqual(len({query["id"] for query in queries}), EXPECTED_QUERY_COUNT)

    def test_category_distribution_is_stable(self) -> None:
        queries = load_queries()
        counts = {
            category["label"]: sum(
                1 for query in queries if query["category"] == category["id"]
            )
            for category in category_options(queries)
        }
        self.assertEqual(
            counts,
            {
                "Meissen und Porzellan": 2,
                "Vintage-Designerschmuck": 16,
                "Jugendstil- und Art-déco-Metallwaren": 27,
                "Skandinavisches Designglas": 15,
                "Keramik": 14,
                "Silber und Schmuck": 8,
                "Kleine Lampen": 10,
                "Metallobjekte": 8,
                "Designobjekte und Figuren": 10,
            },
        )

    def test_meissen_is_limited_to_porcelain_category(self) -> None:
        queries = load_queries()
        meissen = [query for query in queries if query["category"] == "meissen_porcelain"]
        self.assertEqual({query["query"] for query in meissen}, {"Meissen", "Meißen"})
        self.assertEqual({query.get("category_id") for query in meissen}, {"870"})
        self.assertEqual({query["ebay_domain"] for query in meissen}, {"ebay.de"})


if __name__ == "__main__":
    unittest.main()
