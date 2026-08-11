from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


FORBIDDEN_MARKERS = ("ebay", "serpapi")
USER_AGENT = "KleinAntikMeissenScout/1.0 (reference evidence freezer)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze explicitly selected sold-reference images for manual comparison."
    )
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--reference-id", required=True, action="append")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_references(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read reference corpus {path}: {exc}") from exc
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise RuntimeError(f"Reference corpus contains forbidden source marker(s): {', '.join(found)}")
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("Reference corpus must contain a records list")
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        reference_id = str(record.get("reference_id") or "").strip()
        if reference_id:
            index[reference_id] = record
    return index


def image_extension(content_type: str, source_url: str) -> str:
    extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if extension == ".jpe":
        extension = ".jpg"
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return extension
    suffix = Path(urlparse(source_url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def freeze_references(
    session: requests.Session,
    references: dict[str, dict[str, Any]],
    *,
    reference_ids: list[str],
    image_dir: Path,
    output_path: Path,
    fetched_at: str,
    corpus_path: Path,
) -> dict[str, Any]:
    if output_path.exists():
        raise RuntimeError(f"Refusing to overwrite existing reference image manifest: {output_path}")
    if image_dir.exists() and any(image_dir.iterdir()):
        raise RuntimeError(f"Refusing to write into non-empty image directory: {image_dir}")
    unique_ids = list(dict.fromkeys(reference_ids))
    missing = [reference_id for reference_id in unique_ids if reference_id not in references]
    if missing:
        raise RuntimeError(f"Unknown reference IDs: {', '.join(missing)}")
    image_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for reference_id in unique_ids:
        record = references[reference_id]
        image_url = str(record.get("image_url") or "").strip()
        if not image_url.startswith("https://"):
            failures.append({"reference_id": reference_id, "reason": "missing HTTPS image URL"})
            continue
        try:
            response = session.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            failures.append({"reference_id": reference_id, "reason": f"download failed: {type(exc).__name__}"})
            continue
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("image/") or not response.content:
            failures.append({"reference_id": reference_id, "reason": "response was not a non-empty image"})
            continue
        target = image_dir / (safe_stem(reference_id) + image_extension(content_type, image_url))
        if target.exists():
            raise RuntimeError(f"Refusing to overwrite frozen reference image: {target}")
        target.write_bytes(response.content)
        images.append(
            {
                "reference_id": reference_id,
                "source": str(record.get("source") or ""),
                "title": str(record.get("title") or ""),
                "source_image_url": image_url,
                "image_file": str(target.relative_to(output_path.parent)).replace("\\", "/"),
                "content_type": content_type,
                "byte_length": len(response.content),
                "sha256": sha256_bytes(response.content),
                "fetched_at": fetched_at,
            }
        )
    return {
        "run": {
            "frozen_at": fetched_at,
            "reference_corpus_sha256": sha256_file(corpus_path),
            "requested_count": len(unique_ids),
            "image_count": len(images),
            "failure_count": len(failures),
        },
        "images": images,
        "failures": failures,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    references = load_references(args.references)
    payload = freeze_references(
        requests.Session(),
        references,
        reference_ids=args.reference_id,
        image_dir=args.image_dir,
        output_path=args.output,
        fetched_at=utc_now(),
        corpus_path=args.references,
    )
    write_json(args.output, payload)
    print(
        json.dumps(
            {
                "image_count": payload["run"]["image_count"],
                "failure_count": payload["run"]["failure_count"],
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
