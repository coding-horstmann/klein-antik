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
from PIL import Image, ImageDraw, ImageFont, ImageOps


FORBIDDEN_MARKERS = ("ebay", "serpapi")
USER_AGENT = "KleinAntikMeissenScout/1.0 (auditable evidence freezer)"
APPROVED_IMAGE_HOSTS = {
    "auctionet": {"auctionet.com", "cdn.auctionet.com", "images.auctionet.com"},
    "blocket": {"images.blocketcdn.se"},
    "dba": {"images.dbastatic.dk"},
    "tori": {"img.tori.net"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze one primary image per approved Meißen deal listing."
    )
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--contact-sheet-dir", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--per-sheet", type=int, default=12)
    parser.add_argument("--listing-id", action="append")
    parser.add_argument(
        "--all-images",
        action="store_true",
        help="Freeze every available image only for the explicitly selected listings.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read deal batch {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("listings"), list):
        raise RuntimeError("Deal batch must contain a listings list")
    return payload


def reject_forbidden(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in serialized]
    if found:
        raise RuntimeError(f"Deal batch contains forbidden source marker(s): {', '.join(found)}")


def image_extension(content_type: str, source_url: str) -> str:
    extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if extension == ".jpe":
        extension = ".jpg"
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return extension
    suffix = Path(urlparse(source_url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def safe_stem(listing_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", listing_id).strip("-")


def freeze_images(
    session: requests.Session,
    deal_payload: dict[str, Any],
    *,
    image_dir: Path,
    manifest_path: Path,
    limit: int | None,
    selected_listing_ids: list[str] | None,
    all_images: bool,
    fetched_at: str,
) -> dict[str, Any]:
    reject_forbidden(deal_payload)
    if manifest_path.exists():
        raise RuntimeError(f"Refusing to overwrite existing image manifest: {manifest_path}")
    if image_dir.exists() and any(image_dir.iterdir()):
        raise RuntimeError(f"Refusing to write into non-empty image directory: {image_dir}")
    listings = deal_payload["listings"]
    selected_ids = set(selected_listing_ids or [])
    if selected_ids:
        listings = [
            listing
            for listing in listings
            if isinstance(listing, dict) and str(listing.get("listing_id") or "") in selected_ids
        ]
        found_ids = {str(listing.get("listing_id") or "") for listing in listings if isinstance(listing, dict)}
        missing_ids = sorted(selected_ids - found_ids)
        if missing_ids:
            raise RuntimeError(f"Unknown listing IDs: {', '.join(missing_ids)}")
    if all_images and not selected_ids:
        raise ValueError("--all-images requires at least one --listing-id")
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        listings = listings[:limit]
    image_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        listing_id = str(listing.get("listing_id") or "").strip()
        source = str(listing.get("source") or "").strip().lower()
        source_images = listing.get("image_urls")
        image_urls = (
            [str(value).strip() for value in source_images if str(value).strip()]
            if isinstance(source_images, list)
            else []
        )
        if not all_images:
            image_urls = image_urls[:1]
        if source not in APPROVED_IMAGE_HOSTS or not listing_id or not image_urls:
            failures.append({"listing_id": listing_id or "unknown", "reason": "missing approved primary image"})
            continue
        for position, image_url in enumerate(image_urls, start=1):
            if not image_url.startswith("https://"):
                failures.append({"listing_id": listing_id, "reason": "image URL was not HTTPS"})
                continue
            host = (urlparse(image_url).hostname or "").lower()
            if host not in APPROVED_IMAGE_HOSTS[source]:
                failures.append({"listing_id": listing_id, "reason": "image host was not approved"})
                continue
            try:
                response = session.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
                response.raise_for_status()
            except requests.RequestException as exc:
                failures.append({"listing_id": listing_id, "reason": f"download failed: {type(exc).__name__}"})
                continue
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/") or not response.content:
                failures.append({"listing_id": listing_id, "reason": "response was not a non-empty image"})
                continue
            suffix = f"-{position:02d}" if all_images else ""
            filename = safe_stem(listing_id) + suffix + image_extension(content_type, image_url)
            target = image_dir / filename
            if target.exists():
                raise RuntimeError(f"Refusing to overwrite frozen image: {target}")
            target.write_bytes(response.content)
            images.append(
                {
                    "listing_id": listing_id,
                    "source": source,
                    "source_image_url": image_url,
                    "image_file": str(target.relative_to(manifest_path.parent)).replace("\\", "/"),
                    "image_position": position,
                    "primary": position == 1,
                    "content_type": content_type,
                    "byte_length": len(response.content),
                    "sha256": sha256_bytes(response.content),
                    "fetched_at": fetched_at,
                }
            )
    run = deal_payload.get("run") if isinstance(deal_payload.get("run"), dict) else {}
    return {
        "run": {
            "run_id": str(run.get("run_id") or ""),
            "frozen_at": fetched_at,
            "deal_batch_sha256": sha256_file(manifest_path.parent / "deal-listings.json"),
            "listing_count": len(listings),
            "image_count": len(images),
            "failure_count": len(failures),
            "primary_image_only": not all_images,
        },
        "images": images,
        "failures": failures,
    }


def create_contact_sheets(
    image_manifest: dict[str, Any], *, manifest_path: Path, output_dir: Path, per_sheet: int
) -> list[dict[str, Any]]:
    if per_sheet < 1:
        raise ValueError("per-sheet must be positive")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"Refusing to write into non-empty contact-sheet directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = image_manifest.get("images")
    if not isinstance(entries, list):
        raise RuntimeError("Image manifest has no images list")
    columns = 4
    rows_per_sheet = max(1, (per_sheet + columns - 1) // columns)
    cell_width, cell_height, label_height = 260, 220, 24
    font = ImageFont.load_default()
    sheets: list[dict[str, Any]] = []
    for start in range(0, len(entries), per_sheet):
        group = entries[start : start + per_sheet]
        rows = max(1, (len(group) + columns - 1) // columns)
        canvas = Image.new("RGB", (columns * cell_width, rows * (cell_height + label_height)), "white")
        draw = ImageDraw.Draw(canvas)
        listing_ids: list[str] = []
        for index, entry in enumerate(group):
            if not isinstance(entry, dict):
                continue
            listing_id = str(entry.get("listing_id") or "unknown")
            image_file = manifest_path.parent / str(entry.get("image_file") or "")
            try:
                with Image.open(image_file) as source:
                    preview = ImageOps.contain(source.convert("RGB"), (cell_width - 8, cell_height - 8))
            except (OSError, ValueError):
                continue
            row, column = divmod(index, columns)
            x, y = column * cell_width, row * (cell_height + label_height)
            canvas.paste(preview, (x + (cell_width - preview.width) // 2, y + (cell_height - preview.height) // 2))
            draw.text((x + 4, y + cell_height + 5), listing_id, fill="black", font=font)
            listing_ids.append(listing_id)
        target = output_dir / f"zero-shot-contact-{start // per_sheet + 1:03d}.jpg"
        canvas.save(target, format="JPEG", quality=88)
        sheets.append(
            {
                "file": str(target.relative_to(manifest_path.parent)).replace("\\", "/"),
                "listing_ids": listing_ids,
                "rows": rows,
                "columns": columns,
            }
        )
    return sheets


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    deal_payload = load_json(args.deals)
    fetched_at = utc_now()
    image_manifest = freeze_images(
        requests.Session(),
        deal_payload,
        image_dir=args.image_dir,
        manifest_path=args.output,
        limit=args.limit,
        selected_listing_ids=args.listing_id,
        all_images=args.all_images,
        fetched_at=fetched_at,
    )
    if args.contact_sheet_dir:
        image_manifest["contact_sheets"] = create_contact_sheets(
            image_manifest,
            manifest_path=args.output,
            output_dir=args.contact_sheet_dir,
            per_sheet=args.per_sheet,
        )
    write_json(args.output, image_manifest)
    print(
        json.dumps(
            {
                "image_count": image_manifest["run"]["image_count"],
                "failure_count": image_manifest["run"]["failure_count"],
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
