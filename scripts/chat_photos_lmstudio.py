#!/usr/bin/env python3
"""
Interactive CLI chat about photos using LM Studio + gemma-4-e4b-it.

Usage
-----
  # Search your photos by description, then chat
  python3 scripts/chat_photos_lmstudio.py --query "boy with toy gun"

  # Interactive numbered picker
  python3 scripts/chat_photos_lmstudio.py

  # Pass photos explicitly
  python3 scripts/chat_photos_lmstudio.py --photos uploads/a.jpg uploads/b.jpg

  # Cap how many photos load into a session (default 5)
  python3 scripts/chat_photos_lmstudio.py --query "birthday" --limit 8

On first run (or when new photos are added), the script automatically
generates a one-sentence description for each image that doesn't have one
yet. These are cached as .meta.json sidecars next to each image, so
subsequent runs are instant. Press Ctrl+C during indexing to skip it and
fall back to filename-only search.

Commands during chat
--------------------
  /photos             list attached photos
  /search <query>     search sidecars and swap in new photos (clears history)
  /scan <question>    ask a yes/no question about every photo one at a time
                      and summarise (e.g. /scan is Antonio Ortega Jr in this photo?)
  /find <name>        find all photos containing a person by comparing each photo
                      visually against the source-of-truth photo tagged with that name
  /add <file>         add a photo (clears history so it's re-introduced)
  /remove <file>      remove a photo (clears history)
  /save               ask the model to extract what it learned and write it to
                      each photo's metadata (sidecar + EXIF for JPEGs)
  /tag <key> <value>  manually write a metadata field to all attached photos
  /clear              clear conversation history (keep photos)
  /reset              clear history AND photos
  /quit or /exit      exit
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import sidecar

LMS = "lms"  # LM Studio CLI binary name
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
DEFAULT_UPLOADS = Path(__file__).resolve().parent.parent / "uploads"
DEFAULT_LIMIT = 5

DESCRIBE_PROMPT = (
    "Describe this image in 1-2 sentences. "
    "Focus on who or what is in it, the setting, and any notable details. "
    "Be concise and factual."
)


# ── LM Studio startup ────────────────────────────────────────────────────────

def _server_is_up(base_v1: str) -> bool:
    try:
        with urllib.request.urlopen(base_v1.rstrip("/") + "/models", timeout=3):
            return True
    except Exception:
        return False


def _model_is_loaded(base_v1: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(base_v1.rstrip("/") + "/models", timeout=5) as r:
            data = json.loads(r.read())
        return any(m.get("id") == model for m in data.get("data", []))
    except Exception:
        return False


def _run_lms(*args: str) -> bool:
    try:
        result = subprocess.run([LMS, *args])
        return result.returncode == 0
    except FileNotFoundError:
        print(
            f"Error: '{LMS}' not found. Install LM Studio and make sure `lms` is on your PATH.",
            file=sys.stderr,
        )
        return False


def ensure_lmstudio_ready(base_v1: str, model: str) -> None:
    if not _server_is_up(base_v1):
        print("Starting LM Studio server…")
        if not _run_lms("server", "start"):
            raise SystemExit("Could not start LM Studio server.")
        for _ in range(15):
            time.sleep(1)
            if _server_is_up(base_v1):
                break
        else:
            raise SystemExit(f"LM Studio server did not respond at {base_v1} after 15 s.")
        print("Server is up.")
    else:
        print("LM Studio server already running.")

    if not _model_is_loaded(base_v1, model):
        print(f"Loading model {model}…")
        if not _run_lms("load", model):
            raise SystemExit(f"Could not load model '{model}'.")
        print(f"Model {model} loaded.")
    else:
        print(f"Model {model} already loaded.")


# ── image / sidecar helpers ───────────────────────────────────────────────────

def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def _encode_image(path: Path) -> dict:
    mime = MIME.get(path.suffix.lower(), "image/jpeg")
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _desc_sidecar_path(image_path: Path) -> Path:
    """Path to the general-description cache: uploads/.desc_cache/photo.jpg.json"""
    return sidecar.desc_cache_path(image_path)


def _load_sidecar(image_path: Path) -> dict | None:
    """Load the general description sidecar (.desc.json), falling back to .meta.json."""
    return sidecar.read_with_desc_fallback(image_path)


def _write_metadata(image_path: Path, updates: dict) -> None:
    """
    Merge `updates` into the image's .meta.json sidecar, then embed a
    summary into the EXIF UserComment tag for JPEG files.
    """
    # ── sidecar ──────────────────────────────────────────────────────────────
    existing = sidecar.merge(image_path, updates)

    # ── EXIF (JPEG only, via Pillow) ─────────────────────────────────────────
    if image_path.suffix.lower() in (".jpg", ".jpeg"):
        try:
            from PIL import Image

            # Build a human-readable summary from the updates
            parts = []
            if updates.get("people"):
                val = updates["people"]
                parts.append("People: " + (", ".join(val) if isinstance(val, list) else val))
            for field in ("event", "date", "location", "notes", "subject"):
                if updates.get(field):
                    parts.append(f"{field.capitalize()}: {updates[field]}")
            comment = " | ".join(parts)
            if not comment:
                return

            with Image.open(image_path) as img:
                exif = img.getexif()
                exif[0x010E] = comment          # ImageDescription
                exif[0x9286] = comment          # UserComment
                img.save(image_path, exif=exif.tobytes())
        except Exception as exc:
            print(f"  (EXIF write skipped for {image_path.name}: {exc})")


def _save_metadata_from_conversation(
    base_v1: str, api_key: str, model: str,
    conversation: list, photos: list[Path],
    system_prompt: str,
) -> None:
    """Ask the model to extract metadata from the conversation and write it."""
    if not conversation:
        print("Nothing in conversation history to save.")
        return

    photo_names = [p.name for p in photos]
    prompt = METADATA_PROMPT + f"\n\nPhotos in this conversation: {', '.join(photo_names)}"

    # One-shot request — don't add to the ongoing conversation
    messages = conversation + [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 512,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
    }
    url = base_v1.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    print("  Asking model to extract metadata…", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        raw = body["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"  Error contacting model: {exc}")
        return

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()

    try:
        metadata: dict = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  Model didn't return valid JSON:\n  {raw[:300]}")
        return

    photo_map = {p.name: p for p in photos}
    wrote = 0
    for filename, fields in metadata.items():
        if not isinstance(fields, dict):
            continue
        path = photo_map.get(filename)
        if path is None:
            print(f"  (skipping unknown file: {filename})")
            continue
        _write_metadata(path, fields)
        print(f"  Saved → {filename}")
        for k, v in fields.items():
            display = ", ".join(v) if isinstance(v, list) else str(v)
            print(f"    {k}: {display}")
        wrote += 1

    if wrote == 0:
        print("  Model returned no metadata to save.")


def _scan_photos(
    uploads: Path, question: str,
    base_v1: str, api_key: str, model: str,
    system_prompt: str,
) -> None:
    """
    Ask `question` about every image in uploads/ one at a time.
    Prints each answer and a yes/no tally at the end.
    """
    images = _list_upload_images(uploads)
    if not images:
        print("No images found.")
        return

    prompt = (
        f"{question}\n\n"
        "Answer with YES or NO on the first line, then one sentence of explanation."
    )

    yes_files: list[str] = []
    no_files: list[str] = []
    failed: list[str] = []

    print(f"\nScanning {len(images)} image(s)…\n")
    for i, img in enumerate(images, 1):
        print(f"  [{i}/{len(images)}] {img.name} … ", end="", flush=True)
        try:
            b64 = base64.standard_b64encode(img.read_bytes()).decode("ascii")
            mime = MIME.get(img.suffix.lower(), "image/jpeg")
            payload = {
                "model": model,
                "temperature": 0.1,
                "max_tokens": 80,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ]},
                ],
            }
            url = base_v1.rstrip("/") + "/chat/completions"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )

            # One retry after auto-recovering a crashed server
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception as exc:
                    if attempt == 0:
                        print(f"server error, recovering… ", end="", flush=True)
                        ensure_lmstudio_ready(base_v1, model)
                        # Rebuild req — urlopen consumed it
                        req = urllib.request.Request(
                            url,
                            data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json",
                                     "Authorization": f"Bearer {api_key}"},
                            method="POST",
                        )
                    else:
                        raise

            answer = body["choices"][0]["message"]["content"].strip()
            first_line = answer.splitlines()[0].upper()
            is_yes = first_line.startswith("YES")
            print(answer.splitlines()[0])
            if len(answer.splitlines()) > 1:
                print(f"     {' '.join(answer.splitlines()[1:]).strip()}")
            (yes_files if is_yes else no_files).append(img.name)

            # Brief pause — lets LM Studio free memory between images
            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\nScan interrupted.")
            break
        except Exception as exc:
            print(f"error — {exc}")
            failed.append(img.name)

    print(f"\n{'─'*50}")
    print(f"YES ({len(yes_files)}): {', '.join(yes_files) or '—'}")
    print(f"NO  ({len(no_files)}): {', '.join(no_files) or '—'}")
    if failed:
        print(f"FAILED ({len(failed)}): {', '.join(failed)}")
    print(f"{'─'*50}\n")


def _find_person(
    name: str, uploads: Path,
    base_v1: str, api_key: str, model: str,
    system_prompt: str,
) -> list[Path]:
    """
    Find all photos containing `name` by visually comparing each image against
    the source-of-truth photo tagged with subject=<name>.
    Returns matching paths.
    """
    images = _list_upload_images(uploads)

    # ── find source-of-truth photo ───────────────────────────────────────────
    name_lower = name.lower()
    reference: Path | None = None
    for img in images:
        meta = _load_sidecar(img)
        if not meta:
            continue
        subject = str(meta.get("subject") or "").lower()
        people = meta.get("people") or []
        people_str = (", ".join(people) if isinstance(people, list) else str(people)).lower()
        if name_lower in subject or name_lower in people_str:
            reference = img
            break

    if reference is None:
        print(
            f"No source-of-truth photo found for '{name}'.\n"
            f"Tag one with:  /tag subject {name}"
        )
        return []

    print(f"\nSource of truth: {reference.name}")
    print(f"Comparing against {len(images) - 1} other photo(s)…\n")

    ref_b64 = base64.standard_b64encode(reference.read_bytes()).decode("ascii")
    ref_mime = MIME.get(reference.suffix.lower(), "image/jpeg")

    matches: list[Path] = [reference]  # the reference photo always matches
    failed: list[str] = []

    for i, img in enumerate(images, 1):
        if img == reference:
            print(f"  [{i}/{len(images)}] {img.name} … REFERENCE (skipped)")
            continue

        print(f"  [{i}/{len(images)}] {img.name} … ", end="", flush=True)
        try:
            cand_b64 = base64.standard_b64encode(img.read_bytes()).decode("ascii")
            cand_mime = MIME.get(img.suffix.lower(), "image/jpeg")

            payload = {
                "model": model,
                "temperature": 0.1,
                "max_tokens": 80,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": (
                             f"Image 1 is a reference photo of {name}. "
                             f"Image 2 is a candidate photo. "
                             f"Is the same person ({name}) visible in Image 2? "
                             f"Answer YES or NO on the first line, then one sentence explaining why."
                         )},
                        {"type": "image_url", "image_url": {"url": f"data:{ref_mime};base64,{ref_b64}"}},
                        {"type": "image_url", "image_url": {"url": f"data:{cand_mime};base64,{cand_b64}"}},
                    ]},
                ],
            }

            url = base_v1.rstrip("/") + "/chat/completions"
            for attempt in range(2):
                try:
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {api_key}"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception:
                    if attempt == 0:
                        print("recovering… ", end="", flush=True)
                        ensure_lmstudio_ready(base_v1, model)
                    else:
                        raise

            answer = body["choices"][0]["message"]["content"].strip()
            first_line = answer.splitlines()[0].upper()
            is_yes = first_line.startswith("YES")
            print(answer.splitlines()[0])
            if len(answer.splitlines()) > 1:
                print(f"     {' '.join(answer.splitlines()[1:]).strip()}")
            if is_yes:
                matches.append(img)

            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\nSearch interrupted.")
            break
        except Exception as exc:
            print(f"error — {exc}")
            failed.append(img.name)

    print(f"\n{'─'*50}")
    print(f"Photos containing {name} ({len(matches)}):")
    for p in matches:
        print(f"  {p.name}")
    if failed:
        print(f"Could not check ({len(failed)}): {', '.join(failed)}")
    print(f"{'─'*50}\n")

    return matches


def _describe_image(base_v1: str, api_key: str, model: str, image_path: Path) -> str:
    """Ask the model for a rich description of the image."""
    b64 = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    mime = MIME.get(image_path.suffix.lower(), "image/jpeg")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 128,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    }
    url = base_v1.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


def ensure_sidecars(uploads: Path, base_v1: str, api_key: str, model: str) -> None:
    """
    Generate a .desc.json description sidecar for every image that doesn't
    have one yet. Images already described are skipped. The people-analyzer
    .meta.json sidecars are intentionally ignored here — those notes are
    people-count focused and too terse for general keyword search.
    Press Ctrl+C to stop early; already-written sidecars are kept.
    """
    from datetime import datetime, timezone

    images = _list_upload_images(uploads)
    missing = [img for img in images if not _desc_sidecar_path(img).is_file()]

    if not missing:
        return

    print(f"\n{len(missing)} image(s) not yet indexed. Describing… (Ctrl+C to skip)\n")

    for i, img in enumerate(missing, 1):
        print(f"  [{i}/{len(missing)}] {img.name} … ", end="", flush=True)
        try:
            notes = _describe_image(base_v1, api_key, model, img)
            sidecar.write_desc_cache(img, {
                "notes": notes,
                "model": model,
                "source": "chat_photos_lmstudio.py",
            })
            print(notes[:80] + ("…" if len(notes) > 80 else ""))
        except KeyboardInterrupt:
            print("\nIndexing interrupted. Descriptions written so far will be used.")
            break
        except Exception as exc:
            print(f"failed ({exc})")

    print()


def _list_upload_images(uploads: Path) -> list[Path]:
    if not uploads.is_dir():
        return []
    return sorted(p for p in uploads.iterdir() if p.is_file() and _is_image(p))


# ── search ────────────────────────────────────────────────────────────────────

def _score(image: Path, keywords: list[str]) -> int:
    """Return keyword hit count across filename + sidecar notes."""
    text = image.name.lower()
    meta = _load_sidecar(image)
    if meta:
        text += " " + (meta.get("notes") or "").lower()
    return sum(1 for kw in keywords if kw in text)


def search_photos(uploads: Path, query: str, limit: int) -> list[tuple[Path, dict | None]]:
    """
    Score every image in uploads/ against the query keywords.
    Returns up to `limit` (image, sidecar_or_None) pairs, best match first.
    Images with zero keyword hits are excluded.
    """
    keywords = query.lower().split()
    images = _list_upload_images(uploads)

    scored = []
    for img in images:
        s = _score(img, keywords)
        if s > 0:
            scored.append((s, img))

    scored.sort(key=lambda x: (-x[0], x[1].name))
    return [(img, _load_sidecar(img)) for _, img in scored[:limit]]


def _show_search_results(results: list[tuple[Path, dict | None]]) -> None:
    if not results:
        print("  (no matches)")
        return
    for i, (img, meta) in enumerate(results, 1):
        notes = ""
        if meta:
            notes = (meta.get("notes") or "").strip()
            if notes:
                notes = f"  — {notes[:90]}{'…' if len(notes) > 90 else ''}"
            else:
                people = meta.get("people_count")
                notes = f"  — {people} person(s)" if people is not None else ""
        print(f"  {i}. {img.name}{notes}")


# ── photo pickers ─────────────────────────────────────────────────────────────

def pick_by_query(uploads: Path, query: str, limit: int) -> list[Path]:
    """Search sidecars, show results, let user confirm or adjust."""
    results = search_photos(uploads, query, limit)

    if not results:
        no_sidecars = not any(
            (img.parent / (img.name + ".meta.json")).is_file()
            for img in _list_upload_images(uploads)
        )
        if no_sidecars:
            print(
                f"\nNo sidecar metadata found in {uploads}.\n"
                "Run the batch analyzer first to enable full-text search:\n"
                "  python3 scripts/analyze_uploads_people_lmstudio.py --model gemma-4-e4b-it\n"
                "Falling back to filename-only search…"
            )
            # retry with filename-only (sidecars already absent, same result but user was warned)
        print(f"No matches for '{query}'. Switching to manual picker.")
        return pick_interactively(uploads)

    print(f"\nTop {len(results)} match(es) for '{query}':")
    _show_search_results(results)

    raw = input(
        "\nPress Enter to use all, type numbers to pick a subset (e.g. 1 3), "
        "or 's' to search again: "
    ).strip().lower()

    if raw == "s":
        new_query = input("New search query: ").strip()
        return pick_by_query(uploads, new_query, limit) if new_query else []

    if not raw:
        chosen = [img for img, _ in results]
    else:
        chosen = []
        for token in raw.split():
            try:
                idx = int(token)
                if 1 <= idx <= len(results):
                    chosen.append(results[idx - 1][0])
                else:
                    print(f"  (skipping out-of-range: {token})")
            except ValueError:
                print(f"  (skipping non-number: {token})")

    if chosen:
        print(f"\nAttached: {', '.join(p.name for p in chosen)}")
    else:
        print("No photos selected.")
    return chosen


def pick_interactively(uploads: Path) -> list[Path]:
    """Numbered list picker — used when there's no query or no search hits."""
    images = _list_upload_images(uploads)
    if not images:
        print(f"No images found in {uploads}")
        return []

    print(f"\n{len(images)} image(s) in {uploads}:")
    for i, p in enumerate(images, 1):
        print(f"  {i:3}. {p.name}")

    raw = input("\nEnter numbers to include (e.g. 1 3 4), or press Enter for all: ").strip()
    if not raw:
        chosen = images
    else:
        chosen = []
        for token in raw.split():
            try:
                idx = int(token)
                if 1 <= idx <= len(images):
                    chosen.append(images[idx - 1])
                else:
                    print(f"  (skipping out-of-range: {token})")
            except ValueError:
                print(f"  (skipping non-number: {token})")

    if chosen:
        print(f"\nAttached: {', '.join(p.name for p in chosen)}")
    else:
        print("No photos selected.")
    return chosen


# ── LM Studio chat ────────────────────────────────────────────────────────────

METADATA_PROMPT = """Based on our conversation so far, extract any facts you have learned about each photo.
Return ONLY a JSON object — no explanation, no markdown fences.
Keys are the exact photo filenames. Values are objects with any of these fields (omit fields you don't know):
  "people"    : list of strings describing each person (name if known, otherwise description)
  "event"     : string — what occasion or event is shown
  "date"      : string — approximate date or year if mentioned
  "location"  : string — where the photo was taken
  "notes"     : string — anything else worth remembering

Example:
{
  "photo.jpg": {
    "people": ["Antonio (young boy)", "woman in red dress"],
    "event": "Christmas morning",
    "date": "approx 1995",
    "notes": "Boy is holding a toy gun"
  }
}

Only include photos that were part of this conversation. Return only the JSON object."""

_SYSTEM_BASE = (
    "You are a helpful assistant with full knowledge of the user's photo library. "
    "You have two sources of information:\n"
    "1. A TEXT INDEX of every photo in the library (filenames, descriptions, and any "
    "saved metadata) — provided below. Use this to answer questions about the whole "
    "collection, count photos, find people, etc.\n"
    "2. EMBEDDED IMAGES sent directly in this conversation — use these for detailed "
    "visual questions about specific photos.\n\n"
    "Never say you cannot access files or the filesystem. "
    "Never say you need to search uploads/. "
    "All the information you need is either in the index below or in the embedded images. "
    "Answer confidently from what you know.\n\n"
    "{library_index}"
)


def _build_library_index(uploads: Path) -> str:
    """Build a text summary of every image in uploads/ from its sidecar metadata."""
    images = _list_upload_images(uploads)
    if not images:
        return "PHOTO LIBRARY INDEX: (no photos found)"

    lines = [f"PHOTO LIBRARY INDEX ({len(images)} photo(s)):"]
    for img in images:
        parts = [f"- {img.name}"]
        # Prefer .desc.json (general description), fall back to .meta.json
        meta = _load_sidecar(img)
        if meta:
            if meta.get("notes"):
                parts.append(meta["notes"])
            if meta.get("people"):
                val = meta["people"]
                parts.append("People: " + (", ".join(val) if isinstance(val, list) else str(val)))
            for field in ("event", "date", "location", "subject"):
                if meta.get(field):
                    parts.append(f"{field.capitalize()}: {meta[field]}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def build_system_prompt(uploads: Path) -> str:
    return _SYSTEM_BASE.format(library_index=_build_library_index(uploads))


def _chat(base_v1: str, api_key: str, model: str, messages: list, system_prompt: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 1024,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
    }
    url = base_v1.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"\nCannot reach LM Studio at {base_v1}.\n"
            f"Make sure you ran:\n  lms server start\n  lms load {model}\n"
            f"({exc.reason})"
        ) from exc
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:600]
        raise SystemExit(f"\nLM Studio error: {err}") from exc


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Interactive CLI chat about photos via LM Studio."
    )
    ap.add_argument(
        "--query", "-q",
        metavar="TEXT",
        help="Natural-language search against sidecar metadata to pick photos automatically.",
    )
    ap.add_argument(
        "--limit", "-n",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"Max photos to load into one chat session (default: {DEFAULT_LIMIT}).",
    )
    ap.add_argument(
        "--photos",
        nargs="*",
        metavar="FILE",
        help="Explicit photo paths (skips picker entirely).",
    )
    ap.add_argument(
        "--uploads",
        type=Path,
        default=DEFAULT_UPLOADS,
        help=f"Uploads directory (default: {DEFAULT_UPLOADS})",
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("LMSTUDIO_MODEL", "gemma-4-e4b-it"),
        help="Model id (default: gemma-4-e4b-it or $LMSTUDIO_MODEL)",
    )
    ap.add_argument(
        "--base",
        default=os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1"),
        help="LM Studio API base URL (default: http://127.0.0.1:1234/v1)",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),
    )
    args = ap.parse_args()

    ensure_lmstudio_ready(args.base, args.model)

    # Auto-generate sidecar descriptions for any unanalyzed images so that
    # --query and /search have something to search against.
    if args.photos is None:
        ensure_sidecars(args.uploads, args.base, args.api_key, args.model)

    # ── pick photos ──────────────────────────────────────────────────────────
    if args.photos is not None:
        photos: list[Path] = []
        for f in args.photos:
            p = Path(f)
            if not p.is_file():
                print(f"Warning: not found, skipping — {f}", file=sys.stderr)
            elif not _is_image(p):
                print(f"Warning: not an image, skipping — {f}", file=sys.stderr)
            else:
                photos.append(p.resolve())
    elif args.query:
        photos = pick_by_query(args.uploads, args.query, args.limit)
    else:
        photos = pick_interactively(args.uploads)

    if not photos:
        print("No photos attached. You can add them with /add or search with /search.")

    # Build the system prompt with the full library index baked in.
    # Rebuild it whenever the library changes (new uploads, /save, /tag).
    def refresh_system_prompt() -> str:
        return build_system_prompt(args.uploads)

    system_prompt = refresh_system_prompt()

    print(f"\nModel  : {args.model}")
    print(f"Server : {args.base}")
    print(f"Photos : {len(photos)} attached")
    print(f"Index  : {len(_list_upload_images(args.uploads))} photo(s) in library")
    print("\nType your question. Commands: /photos /search /find /scan /add /remove /save /tag /clear /reset /quit\n")

    conversation: list[dict] = []
    images_introduced = False

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0

        if not user_input:
            continue

        # ── slash commands ───────────────────────────────────────────────────
        if user_input.startswith("/"):
            cmd, _, arg = user_input[1:].partition(" ")
            cmd = cmd.lower()

            if cmd in ("quit", "exit", "q"):
                print("Bye.")
                return 0

            elif cmd == "photos":
                if photos:
                    print("Attached photos:")
                    for p in photos:
                        meta = _load_sidecar(p)
                        notes = (meta.get("notes") or "") if meta else ""
                        suffix = f"  — {notes[:80]}" if notes else ""
                        print(f"  {p.name}{suffix}")
                else:
                    print("No photos attached.")

            elif cmd == "search":
                if not arg.strip():
                    print("Usage: /search <query>")
                else:
                    new_photos = pick_by_query(args.uploads, arg.strip(), args.limit)
                    if new_photos:
                        photos = new_photos
                        conversation = []
                        images_introduced = False
                        print("Photos updated. History cleared.")

            elif cmd == "add":
                p = Path(arg.strip())
                if not p.is_file():
                    print(f"Not found: {p}")
                elif not _is_image(p):
                    print(f"Not an image: {p}")
                elif p.resolve() in [x.resolve() for x in photos]:
                    print(f"Already attached: {p.name}")
                else:
                    photos.append(p.resolve())
                    conversation = []
                    images_introduced = False
                    print(f"Added: {p.name}  (history cleared so photos are re-introduced)")

            elif cmd == "remove":
                target = arg.strip()
                before = len(photos)
                photos = [p for p in photos if p.name != target and str(p) != target]
                if len(photos) < before:
                    conversation = []
                    images_introduced = False
                    print(f"Removed: {target}  (history cleared so photos are re-introduced)")
                else:
                    print(f"Not found in attached list: {target}")

            elif cmd == "find":
                if not arg.strip():
                    print("Usage: /find <name>  e.g. /find Antonio Ortega Jr")
                else:
                    matches = _find_person(
                        arg.strip(), args.uploads,
                        args.base, args.api_key, args.model,
                        system_prompt,
                    )
                    if matches:
                        # Load matches as the active photo set for follow-up chat
                        photos = matches
                        conversation = []
                        images_introduced = False
                        print(f"Loaded {len(matches)} matching photo(s) for follow-up chat.")

            elif cmd == "scan":
                if not arg.strip():
                    print("Usage: /scan <yes/no question>  e.g. /scan is Antonio Ortega Jr in this photo?")
                else:
                    _scan_photos(args.uploads, arg.strip(), args.base, args.api_key, args.model, system_prompt)

            elif cmd == "save":
                _save_metadata_from_conversation(
                    args.base, args.api_key, args.model, conversation, photos, system_prompt
                )
                system_prompt = refresh_system_prompt()

            elif cmd == "tag":
                # /tag <key> <value>
                parts = arg.strip().split(None, 1)
                if len(parts) < 2:
                    print("Usage: /tag <key> <value>  e.g. /tag event 'Christmas 1995'")
                else:
                    key, value = parts
                    for p in photos:
                        _write_metadata(p, {key: value})
                        print(f"  Tagged {p.name}  →  {key}: {value}")
                    system_prompt = refresh_system_prompt()

            elif cmd == "clear":
                conversation = []
                images_introduced = False
                print("Conversation history cleared.")

            elif cmd == "reset":
                conversation = []
                images_introduced = False
                photos = []
                print("History and photos cleared.")

            else:
                print(f"Unknown command: /{cmd}")

            continue

        # ── send message ─────────────────────────────────────────────────────
        # Images sent once in the first message only — keeps payload small.
        content: list = []
        if not images_introduced and photos:
            failed = []
            for p in photos:
                try:
                    content.append(_encode_image(p))
                except Exception as exc:
                    failed.append(f"{p.name}: {exc}")
            if failed:
                print("Warning: could not encode some photos:")
                for f in failed:
                    print(f"  {f}")
            attached = len(content)
            if attached:
                print(f"  [sending {attached} photo(s): {', '.join(p.name for p in photos)}]")
            else:
                print("  [no photos could be encoded — sending text only]")
        elif not images_introduced and not photos:
            print("  [no photos attached — use /add <file> or /search <query>]")

        content.append({"type": "text", "text": user_input})

        conversation.append({"role": "user", "content": content})

        print("gemma> ", end="", flush=True)
        try:
            reply = _chat(args.base, args.api_key, args.model, conversation, system_prompt)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"\nError: {exc}")
            conversation.pop()
            continue

        print(reply)
        images_introduced = True
        conversation.append({"role": "assistant", "content": reply})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
