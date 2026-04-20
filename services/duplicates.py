"""
Duplicate photo detection using SHA-256 for exact matches and pHash for near-duplicates.
"""

import hashlib
import os
from collections import defaultdict
from pathlib import Path

import imagehash
from PIL import Image

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}


def compute_sha256(filepath: Path) -> str:
    """Compute SHA-256 hash of file content."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(filepath: Path) -> str:
    """Compute perceptual hash of an image."""
    try:
        with Image.open(filepath) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def hamming_distance(hash1: str, hash2: str) -> int:
    """Calculate Hamming distance between two hex strings."""
    if hash1 is None or hash2 is None:
        return 100
    h1 = int(hash1, 16)
    h2 = int(hash2, 16)
    diff = h1 ^ h2
    return bin(diff).count("1")


def is_image_file(filename: str) -> bool:
    """Check if file is an image based on extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in IMAGE_EXTENSIONS


def scan_duplicates(job, upload_folder: str):
    """
    Scan upload folder for duplicate photos.
    Uses SHA-256 for exact duplicates and pHash for near-duplicates (Hamming ≤ 10).
    Returns list of duplicate groups: [[filename, filename, ...], ...]
    """
    job.set_progress(0, "Discovering files...")

    files = [
        f
        for f in os.listdir(upload_folder)
        if not f.startswith(".")
        and os.path.isfile(os.path.join(upload_folder, f))
        and not f.endswith(".meta.json")
        and f != "_set_unique.meta.json"
        and is_image_file(f)
    ]

    if not files:
        job.set_result([])
        return []

    total = len(files)
    processed = 0

    sha256_groups = defaultdict(list)
    phash_map = {}

    for filename in files:
        filepath = Path(upload_folder) / filename
        job.set_progress(
            int(processed / total * 50),
            f"Hashing {filename}...",
            current_file=filename,
        )

        sha = compute_sha256(filepath)
        sha256_groups[sha].append(filename)

        ph = compute_phash(filepath)
        if ph:
            phash_map[filename] = ph

        processed += 1

    exact_groups = [files for files in sha256_groups.values() if len(files) > 1]

    job.set_progress(50, "Finding near-duplicates via pHash...")

    filenames_with_hash = list(phash_map.keys())
    near_duplicate_groups = []
    used = set()

    for i, fname1 in enumerate(filenames_with_hash):
        if fname1 in used:
            continue
        hash1 = phash_map[fname1]
        group = [fname1]

        for fname2 in filenames_with_hash[i + 1 :]:
            if fname2 in used:
                continue
            hash2 = phash_map[fname2]
            dist = hamming_distance(hash1, hash2)
            if dist <= 10:
                group.append(fname2)
                used.add(fname2)

        if len(group) > 1:
            near_duplicate_groups.append(group)
            for f in group:
                used.add(f)

    merged_groups = []
    all_seen = set()

    for group in exact_groups:
        merged_groups.append(group)
        for f in group:
            all_seen.add(f)

    for group in near_duplicate_groups:
        new_group = [f for f in group if f not in all_seen]
        if len(new_group) > 1:
            merged_groups.append(new_group)
            for f in new_group:
                all_seen.add(f)

    job.set_progress(100, f"Found {len(merged_groups)} duplicate groups")

    job.set_result(merged_groups)
    return merged_groups