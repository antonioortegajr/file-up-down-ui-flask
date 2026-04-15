"""
Sidecar data layer for file-up-down-ui-flask.

Consolidates all sidecar read/write operations into a single module with a consistent schema.

Schema:
{
    'notes': str,
    'people': list[str],
    'event': str,
    'date': str,
    'location': str,
    'subject': str,
    'source': str,
    'model': str,
    'generated_at': 'ISO8601',
    'last_updated': 'ISO8601'
}

Two sidecar files are maintained:
- .meta.json: user-visible metadata stored next to the image
- .desc_cache/photo.jpg.json: hidden cache for AI-generated descriptions
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def meta_path(image_path: Path) -> Path:
    """Return the path to the .meta.json sidecar for an image."""
    return image_path.parent / (image_path.name + ".meta.json")


def desc_cache_path(image_path: Path) -> Path:
    """Return the path to the hidden description cache for an image."""
    cache_dir = image_path.parent / ".desc_cache"
    return cache_dir / (image_path.name + ".json")


def read(image_path: Path) -> dict | None:
    """
    Read the user-visible .meta.json sidecar for an image.
    Returns None if no sidecar exists or if reading fails.
    """
    sidecar = meta_path(image_path)
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write(image_path: Path, data: dict) -> None:
    """
    Write data to the .meta.json sidecar for an image.
    Sets generated_at if not already present.
    """
    record = dict(data)
    if "generated_at" not in record:
        record["generated_at"] = datetime.now(timezone.utc).isoformat()
    meta_path(image_path).write_text(
        json.dumps(record, indent=2),
        encoding="utf-8",
    )


def merge(image_path: Path, updates: dict) -> dict:
    """
    Merge updates into the existing .meta.json sidecar.
    Sets last_updated to current time.
    Returns the full updated record.
    """
    existing = read(image_path) or {}
    existing.update(updates)
    existing["last_updated"] = datetime.now(timezone.utc).isoformat()
    write(image_path, existing)
    return existing


def _get_all_text_fields(data: dict) -> str:
    """Extract all searchable text from a sidecar dict."""
    parts: list[str] = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def search(uploads_dir: Path, query: str) -> list[tuple[Path, dict | None]]:
    """
    Search all images in uploads_dir for case-insensitive matches across all text fields.
    Returns list of (image_path, sidecar_dict_or_None) tuples, best match first.
    """
    keywords = query.lower().split()
    if not keywords:
        return []

    results: list[tuple[int, Path, dict | None]] = []

    for image_path in uploads_dir.iterdir():
        if not image_path.is_file():
            continue
        if image_path.name.startswith("."):
            continue
        if image_path.name.endswith(".meta.json"):
            continue

        score = 0
        text_parts = [image_path.name.lower()]

        sidecar = read(image_path)
        if sidecar:
            text_parts.append(_get_all_text_fields(sidecar))

        combined_text = " ".join(text_parts)
        for kw in keywords:
            if kw in combined_text:
                score += 1

        if score > 0:
            results.append((score, image_path, sidecar))

    results.sort(key=lambda x: (-x[0], x[1].name))
    return [(path, sidecar) for _, path, sidecar in results]


def delete(image_path: Path) -> bool:
    """
    Delete the .meta.json sidecar for an image if it exists.
    Returns True if deleted, False if no sidecar existed.
    """
    sidecar = meta_path(image_path)
    if sidecar.is_file():
        try:
            sidecar.unlink()
            return True
        except OSError:
            pass
    return False


def read_desc_cache(image_path: Path) -> dict | None:
    """
    Read the hidden description cache (.desc_cache/photo.jpg.json).
    Returns None if no cache exists or if reading fails.
    """
    cache = desc_cache_path(image_path)
    if not cache.is_file():
        return None
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_desc_cache(image_path: Path, data: dict) -> None:
    """
    Write data to the hidden description cache.
    Creates .desc_cache/ directory if needed.
    Sets generated_at if not present.
    """
    record = dict(data)
    if "generated_at" not in record:
        record["generated_at"] = datetime.now(timezone.utc).isoformat()
    path = desc_cache_path(image_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def read_with_desc_fallback(image_path: Path) -> dict | None:
    """
    Read the user-visible .meta.json sidecar, falling back to .desc_cache/ if needed.
    Used by search/indexing functions that want the most complete metadata.
    """
    meta = read(image_path)
    if meta:
        return meta
    return read_desc_cache(image_path)
