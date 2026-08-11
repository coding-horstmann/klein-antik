from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "validate_scout_bundle.py"
)
COLLECTOR_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "collect_auctionet_deals.py"
)
IMAGE_FREEZER_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "freeze_listing_images.py"
)
ZERO_SHOT_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "build_zero_shot_triage.py"
)
REFERENCE_IMAGE_FREEZER_PATH = (
    ROOT
    / "skills"
    / "run-meissen-porcelain-scout-pipeline"
    / "scripts"
    / "freeze_selected_reference_images.py"
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


def load_collector() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "collect_meissen_auctionet_deals", COLLECTOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Meissen Auctionet collector")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_image_freezer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "freeze_meissen_listing_images", IMAGE_FREEZER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Meissen image freezer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_zero_shot_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_meissen_zero_shot_triage", ZERO_SHOT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Meissen zero-shot triage builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_reference_image_freezer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "freeze_meissen_reference_images", REFERENCE_IMAGE_FREEZER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Meissen reference image freezer")
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
        cls.collector = load_collector()
        cls.image_freezer = load_image_freezer()
        cls.zero_shot_builder = load_zero_shot_builder()
        cls.reference_image_freezer = load_reference_image_freezer()

    def test_auctionet_collector_preserves_active_price_images_and_risks(self) -> None:
        payload = {
            "items": [
                {
                    "id": 501,
                    "shortTitle": "Meissen porcelain figurine",
                    "url": "/en/501-meissen-figurine",
                    "mainImageUrl": "https://images.example.test/501-main.jpg",
                    "imageUrls": [
                        "https://images.example.test/501-main.jpg",
                        "https://images.example.test/501-detail.jpg",
                    ],
                    "amountValue": "176 EUR",
                    "currency": "GBP",
                    "auctionEndTime": "1 day",
                    "auctionEndsAtTitle": "12 Aug 2026 18:00",
                },
                {
                    "id": 502,
                    "shortTitle": "Meissen-style porcelain stand",
                    "url": "/en/502-style-stand",
                    "mainImageUrl": "https://images.example.test/502.jpg",
                    "amountValue": "60 EUR",
                    "currency": "EUR",
                    "auctionEndTime": "2 days",
                },
            ]
        }
        markup = '<div data-react-props="' + html.escape(json.dumps(payload), quote=True) + '"></div>'

        class Response:
            text = markup
            content = markup.encode("utf-8")

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *args: object, **kwargs: object) -> Response:
                return Response()

        listings, pages = self.collector.collect_active_listings(
            Session(),
            query="Meissen",
            max_pages=1,
            limit=48,
            collected_at="2026-08-11T12:00:00Z",
        )

        self.assertEqual(len(listings), 2)
        self.assertEqual(len(pages), 1)
        self.assertEqual(listings[0]["price_eur"], "176.00")
        self.assertEqual(listings[0]["source_currency_label"], "GBP")
        self.assertEqual(len(listings[0]["image_urls"]), 2)
        self.assertEqual(listings[1]["attribution_status"], "risk")
        self.assertEqual(listings[1]["risks"], ["meissen_style"])
        self.assertEqual(
            self.collector.title_risks("Meissen teapot, 3rd quality"),
            ["quality_or_seconds"],
        )
        self.assertEqual(
            self.collector.title_risks("Meissen plates, 5 pieces, 4th quality"),
            ["quality_or_seconds"],
        )
        self.assertEqual(
            self.collector.title_risks("Kaendler (Fischbach/Sachsen) figurine"),
            [],
        )

    def test_auctionet_collector_uses_ecb_rate_for_non_eur_listing(self) -> None:
        markup = '<div data-react-props="' + html.escape(json.dumps({"items": [{"id": 88, "shortTitle": "Meissen vase", "url": "/en/88", "mainImageUrl": "https://images.example.test/88.jpg", "amountValue": "1,000 SEK", "currency": "SEK"}]}), quote=True) + '"></div>'
        rates_xml = b'<?xml version="1.0"?><Envelope><Cube time="2026-08-10"><Cube currency="SEK" rate="11.0000"/></Cube></Envelope>'

        class Response:
            def __init__(self, text: str, content: bytes) -> None:
                self.text = text
                self.content = content

            def raise_for_status(self) -> None:
                return None

        class Session:
            calls = 0

            def get(self, *args: object, **kwargs: object) -> Response:
                self.calls += 1
                if self.calls == 1:
                    return Response(markup, markup.encode("utf-8"))
                return Response(rates_xml.decode("utf-8"), rates_xml)

        listings, _ = self.collector.collect_active_listings(
            Session(),
            query="Meissen",
            max_pages=1,
            limit=48,
            collected_at="2026-08-11T12:00:00Z",
        )

        self.assertEqual(listings[0]["price_eur"], "90.91")
        self.assertEqual(listings[0]["fx_rate"], "11.0000")

    def test_image_freezer_keeps_one_primary_image_and_audit_hash(self) -> None:
        class Response:
            content = b"image-bytes"
            headers = {"Content-Type": "image/jpeg"}

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *args: object, **kwargs: object) -> Response:
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            deals_path = directory / "deal-listings.json"
            write_json(
                deals_path,
                {
                    "run": {"run_id": "image-test"},
                    "listings": [
                        {
                            "listing_id": "auctionet:501",
                            "source": "auctionet",
                            "image_urls": [
                                "https://images.example.test/501.jpg",
                                "https://images.example.test/501-detail.jpg",
                            ],
                        }
                    ],
                },
            )
            manifest_path = directory / "image-manifest.json"
            manifest = self.image_freezer.freeze_images(
                Session(),
                json.loads(deals_path.read_text(encoding="utf-8")),
                image_dir=directory / "images",
                manifest_path=manifest_path,
                limit=None,
                selected_listing_ids=None,
                all_images=False,
                fetched_at="2026-08-11T12:00:00Z",
            )

        self.assertEqual(manifest["run"]["image_count"], 1)
        self.assertEqual(manifest["run"]["failure_count"], 0)
        self.assertEqual(manifest["images"][0]["image_file"], "images/auctionet-501.jpg")
        self.assertEqual(manifest["images"][0]["sha256"], hashlib.sha256(b"image-bytes").hexdigest())

    def test_image_freezer_all_images_requires_explicit_listing_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            manifest_path = directory / "image-manifest.json"
            with self.assertRaisesRegex(ValueError, "requires at least one"):
                self.image_freezer.freeze_images(
                    Mock(),
                    {"listings": []},
                    image_dir=directory / "images",
                    manifest_path=manifest_path,
                    limit=None,
                    selected_listing_ids=None,
                    all_images=True,
                    fetched_at="2026-08-11T12:00:00Z",
                )

    def test_zero_shot_triage_does_not_use_prices_and_rejects_other_makers(self) -> None:
        result = self.zero_shot_builder.build_zero_shot(
            {
                "listings": [
                    {
                        "listing_id": "auctionet:1",
                        "title": "Meissen figure, 4th quality",
                        "risks": [],
                        "price_eur": "5.00",
                    },
                    {
                        "listing_id": "auctionet:2",
                        "title": "Chelsea porcelain plate",
                        "risks": [],
                        "price_eur": "5000.00",
                    },
                    {
                        "listing_id": "auctionet:3",
                        "title": "Kaendler, Fischbach/Sachsen figurine",
                        "risks": [],
                    },
                ]
            },
            {
                "images": [
                    {"listing_id": "auctionet:1", "image_file": "images/1.jpg"},
                    {"listing_id": "auctionet:2", "image_file": "images/2.jpg"},
                    {"listing_id": "auctionet:3", "image_file": "images/3.jpg"},
                ]
            },
            deal_hash="a" * 64,
            image_hash="b" * 64,
        )

        self.assertFalse(result["summary"]["uses_reference_prices"])
        self.assertEqual(result["records"][0]["screening_status"], "restricted_comparables_only")
        self.assertEqual(result["records"][1]["screening_status"], "reject_before_reference_pass")
        self.assertEqual(result["records"][2]["risks"], [])

    def test_reference_image_freezer_requires_selected_known_references(self) -> None:
        class Response:
            content = b"reference-image"
            headers = {"Content-Type": "image/jpeg"}

            def raise_for_status(self) -> None:
                return None

        class Session:
            def get(self, *args: object, **kwargs: object) -> Response:
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            corpus_path = directory / "reference-corpus.json"
            write_json(corpus_path, {"records": []})
            output_path = directory / "reference-image-manifest.json"
            payload = self.reference_image_freezer.freeze_references(
                Session(),
                {
                    "auctionet:r1": {
                        "source": "auctionet",
                        "title": "Meissen vase",
                        "image_url": "https://images.example.test/r1.jpg",
                    }
                },
                reference_ids=["auctionet:r1"],
                image_dir=directory / "reference-images",
                output_path=output_path,
                fetched_at="2026-08-11T12:00:00Z",
                corpus_path=corpus_path,
            )

        self.assertEqual(payload["run"]["image_count"], 1)
        self.assertEqual(payload["images"][0]["reference_id"], "auctionet:r1")

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

    def test_dashboard_export_requires_the_meissen_category(self) -> None:
        response = Mock()
        response.json.return_value = {"category": "ceramics", "records": []}
        response.raise_for_status.return_value = None
        with patch.dict(
            "os.environ",
            {"DASHBOARD_USER": "niklas", "DASHBOARD_PASSWORD": "secret"},
            clear=False,
        ):
            exporter_spec = importlib.util.spec_from_file_location(
                "export_meissen_reference_corpus",
                ROOT
                / "skills"
                / "run-meissen-porcelain-scout-pipeline"
                / "scripts"
                / "export_reference_corpus.py",
            )
        if exporter_spec is None or exporter_spec.loader is None:
            self.fail("Cannot load Meissen reference exporter")
        exporter = importlib.util.module_from_spec(exporter_spec)
        exporter_spec.loader.exec_module(exporter)
        with patch.dict(
            "os.environ",
            {"DASHBOARD_USER": "niklas", "DASHBOARD_PASSWORD": "secret"},
            clear=False,
        ):
            with patch.object(exporter.requests, "get", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "not a Meissen"):
                    exporter.export_from_dashboard("https://dashboard.example.test")

    def test_reference_profile_marks_style_and_preserves_unknowns(self) -> None:
        profile_spec = importlib.util.spec_from_file_location(
            "build_meissen_reference_profile",
            ROOT
            / "skills"
            / "run-meissen-porcelain-scout-pipeline"
            / "scripts"
            / "build_reference_profile.py",
        )
        if profile_spec is None or profile_spec.loader is None:
            self.fail("Cannot load Meissen reference profiler")
        profiler = importlib.util.module_from_spec(profile_spec)
        profile_spec.loader.exec_module(profiler)
        profile = profiler.build_profile(
            {
                "category": "meissen_porcelain",
                "generated_at": "2026-08-11T00:00:00+00:00",
                "records": [
                    {
                        "reference_id": "auctionet:1",
                        "title": "Meissen-style vase, restored",
                        "price_value": "100",
                        "currency": "USD",
                        "price_basis": "hammer",
                    },
                    {
                        "reference_id": "auctionet:2",
                        "title": "Meissen porcelain object",
                        "price_value": "50",
                        "currency": "USD",
                        "price_basis": "hammer",
                    },
                ],
            }
        )

        self.assertEqual(profile["object_type_counts"], {"unknown": 1, "vase": 1})
        self.assertEqual(profile["records"][0]["risks"], ["meissen_style", "condition"])


if __name__ == "__main__":
    unittest.main()
