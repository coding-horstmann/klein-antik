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


if __name__ == "__main__":
    unittest.main()
