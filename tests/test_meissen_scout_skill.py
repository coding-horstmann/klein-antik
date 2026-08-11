from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "validate_scout_bundle.py"
)


def load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "validate_meissen_scout_bundle", VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Meissen scout validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MeissenScoutValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_validator()

    def make_bundle(self, directory: Path) -> tuple[Path, Path, dict[str, object]]:
        reference_path = directory / "references.json"
        deal_path = directory / "deals.json"
        references = {
            "records": [
                {
                    "reference_id": f"auctionet:r{index}",
                    "source": "auctionet",
                    "url": f"https://auctionet.com/items/r{index}",
                }
                for index in range(1, 4)
            ]
        }
        deals = {
            "listings": [
                {
                    "listing_id": "auctionet:d1",
                    "source": "auctionet",
                    "external_id": "d1",
                    "url": "https://auctionet.com/items/d1",
                    "image_urls": ["https://images.example.test/d1.jpg"],
                }
            ]
        }
        write_json(reference_path, references)
        write_json(deal_path, deals)
        candidates: dict[str, object] = {
            "input_hashes": {
                "references": sha256(reference_path),
                "deals": sha256(deal_path),
            },
            "candidates": [
                {
                    "candidate_id": "M001",
                    "listing_id": "auctionet:d1",
                    "object_type": "figurine",
                    "deal_price_eur": "100.00",
                    "conservative_reference_eur": "500.00",
                    "median_reference_eur": "600.00",
                    "exact_comparable_count": 3,
                    "reference_ids": [
                        "auctionet:r1",
                        "auctionet:r2",
                        "auctionet:r3",
                    ],
                    "directional_spread_eur": "400.00",
                    "price_ratio": "0.20",
                    "priority": "A",
                    "confidence": "medium",
                    "risks": ["mark not legible"],
                    "manual_review_required": True,
                }
            ],
        }
        return reference_path, deal_path, candidates

    def test_accepts_a_consistent_non_ebay_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference_path, deal_path, candidates = self.make_bundle(
                Path(temp_dir)
            )
            report = self.validator.validate_bundle(
                json.loads(reference_path.read_text(encoding="utf-8")),
                json.loads(deal_path.read_text(encoding="utf-8")),
                candidates,
                reference_path,
                deal_path,
            )

        self.assertTrue(report["valid"])
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["priority_counts"]["A"], 1)

    def test_rejects_forbidden_marketplace_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference_path, deal_path, candidates = self.make_bundle(
                Path(temp_dir)
            )
            deals = json.loads(deal_path.read_text(encoding="utf-8"))
            deals["listings"][0]["source"] = "ebay_active"

            with self.assertRaisesRegex(
                self.validator.ValidationError, "forbidden source marker"
            ):
                self.validator.validate_bundle(
                    json.loads(reference_path.read_text(encoding="utf-8")),
                    deals,
                    candidates,
                    reference_path,
                    deal_path,
                )


if __name__ == "__main__":
    unittest.main()
