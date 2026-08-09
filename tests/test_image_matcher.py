from __future__ import annotations

from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from klein_antik.match_worker import compare_features, extract_features


def image_payload(*, background: str, accent: str) -> bytes:
    image = Image.new("RGB", (160, 120), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((35, 22, 125, 108), fill=accent)
    draw.line((20, 100, 145, 25), fill="white", width=5)
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=90)
    return stream.getvalue()


class ImageMatcherTests(unittest.TestCase):
    def test_features_are_stable_for_the_same_image(self) -> None:
        payload = image_payload(background="#101820", accent="#d6612c")
        first = extract_features(payload)
        second = extract_features(payload)

        self.assertEqual(first["ahash"], second["ahash"])
        self.assertEqual(first["dhash"], second["dhash"])
        self.assertEqual(len(first["color_vector"]), 24)
        self.assertEqual(len(first["edge_vector"]), 8)

    def test_same_image_ranks_above_visually_different_image(self) -> None:
        deal = {
            **extract_features(image_payload(background="#101820", accent="#d6612c")),
            "title": "WMF Ikora Art Deco Vase",
        }
        same = {
            **extract_features(image_payload(background="#101820", accent="#d6612c")),
            "title": "WMF Ikora vase",
        }
        different = {
            **extract_features(image_payload(background="#edf3ee", accent="#3156a3")),
            "title": "Royal Copenhagen porcelain figure",
        }

        same_score = compare_features(deal, same)
        different_score = compare_features(deal, different)

        self.assertGreater(same_score["score"], 0.90)
        self.assertGreater(same_score["score"], different_score["score"])
        self.assertGreater(same_score["visual_score"], different_score["visual_score"])
        self.assertEqual(different_score["score"], 0.0)

    def test_incompatible_object_types_are_not_candidates(self) -> None:
        feature = extract_features(image_payload(background="#101820", accent="#d6612c"))
        earrings = {**feature, "title": "Miriam Haskell Ohrclips"}
        bracelet = {**feature, "title": "Miriam Haskell bangle bracelet"}

        score = compare_features(earrings, bracelet)

        self.assertGreater(score["visual_score"], 0.90)
        self.assertEqual(score["score"], 0.0)

    def test_object_filter_recognizes_compounds_and_plurals(self) -> None:
        feature = extract_features(image_payload(background="#101820", accent="#d6612c"))
        necklace = {**feature, "title": "Miriam Haskell Halskette"}
        bracelet = {**feature, "title": "Miriam Haskell bracelets"}

        score = compare_features(necklace, bracelet)

        self.assertEqual(score["score"], 0.0)

    def test_porcelain_object_types_and_non_porcelain_noise_are_rejected(self) -> None:
        feature = extract_features(image_payload(background="#101820", accent="#d6612c"))
        pot = {**feature, "title": "Meissen Schwanenservice Kaffeepot"}
        figure = {**feature, "title": "Meissen porcelain figure"}
        pen = {**feature, "title": "Meissen Kugelschreiber Gold"}

        self.assertEqual(compare_features(pot, figure)["score"], 0.0)
        self.assertEqual(compare_features(pen, figure)["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
