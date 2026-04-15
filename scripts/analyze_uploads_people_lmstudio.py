#!/usr/bin/env python3
"""
Batch-send every image in `uploads/` to a running LM Studio OpenAI-compatible API,
ask a vision model for a people count per image, then summarize:

  - Count per file
  - Files with zero people
  - Whether every image has the *same* people count (and what that count is)

Prerequisites (run in order)
----------------------------
1. LM Studio: load a **vision** model (VLM), e.g. Qwen2-VL, LLaVA, Pixtral, etc.
2. Start the local server (port 1234 by default), e.g. in a terminal:
       lms server start
   or start the server from the LM Studio UI (Developer / Local Server).
3. Note the **model id** shown in LM Studio or from:
       curl -s http://127.0.0.1:1234/v1/models
4. From this repo root:
       python3 scripts/analyze_uploads_people_lmstudio.py --model YOUR_MODEL_ID

Each successful image gets a sidecar JSON next to it, e.g. `photo.jpg.meta.json`, with
`people_count`, `has_people`, `notes`, `generated_at`, and `model`. The Flask file detail
page shows this block when present.

Environment (optional)
----------------------
  LMSTUDIO_BASE   default http://127.0.0.1:1234/v1
  LMSTUDIO_API_KEY  default lm-studio (LM Studio ignores the value for local)

Example
-------
  export LMSTUDIO_BASE=http://127.0.0.1:1234/v1
  python3 scripts/analyze_uploads_people_lmstudio.py \\
      --model qwen2-vl-7b-instruct \\
      --uploads ./uploads
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import sidecar

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

REPORT_LIMITATIONS = [
    "Counts are model estimates, not ground truth (no human verification).",
    "Crowds, occlusion, mirrors, and distant figures often produce wrong totals.",
    "Screenshots may show people in UI thumbnails, video frames, or photos-within-photos — the model may count or skip those inconsistently.",
    "If the API or parsing failed for a file, that file has no reliable count in this report.",
    "\"Distinct\" people within one image is a guess; the model cannot biometrically verify identity.",
    "A single multi-image guess for \"unique people across the set\" is speculative — same person in different photos may be counted twice or merged incorrectly.",
]

PROMPT = """You are analyzing a single image for visible people.

Definitions:
- "people_count": approximate number of visible people instances (faces/bodies you count).
- "distinct_people": your best estimate of how many *different unique individuals* appear in THIS image only. If the same person is visible twice (e.g. mirror), count them once. In a crowd, approximate unique individuals if possible; if impossible, set distinct_people equal to people_count and explain in notes.

Rules:
- "People" means clearly visible humans (face or full/partial body). Do not count statues, photos-of-faces on screens, or drawings unless clearly intended as the subject.
- Respond with ONLY valid JSON, no markdown fences, no text before or after. Use this exact shape:
{"people_count": <non-negative integer>, "distinct_people": <non-negative integer>, "has_people": <true or false>, "notes": "<short string or empty>"}
- distinct_people must be <= people_count when both are meaningful; both 0 if nobody is visible.
- Set "has_people" to false if and only if people_count is 0.
- In "notes", mention ambiguity (mirrors, crowds, TV screens) if relevant; use "" if nothing to add."""

SET_UNIQUE_PROMPT = """You are shown multiple images in order (image 1 first, then image 2, …).
Estimate how many UNIQUE individuals appear **across all images combined**.

Rules:
- Do NOT simply add each image's body count. If the same person might appear in several photos, count them once.
- Cross-photo identity is uncertain: if you cannot match faces, say so in notes and use lower confidence.
- Respond with ONLY valid JSON, no markdown:
{"unique_people_across_set": <non-negative integer>, "confidence": "<low|medium|high>", "notes": "<short string>"}"""


def _mime_for(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "application/octet-stream")


def _post_chat_completions(
    base_v1: str,
    api_key: str,
    model: str,
    image_path: Path,
) -> str:
    """Return assistant message content text."""
    b64 = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    mime = _mime_for(image_path)
    data_url = f"data:{mime};base64,{b64}"

    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 384,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
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

    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected API response: {body!r}") from e


def _post_chat_completions_multi(
    base_v1: str,
    api_key: str,
    model: str,
    image_paths: list[Path],
    prompt_text: str,
    *,
    max_tokens: int = 512,
) -> str:
    """One chat message with text + multiple images (OpenAI-style content array)."""
    content: list = [{"type": "text", "text": prompt_text}]
    for path in image_paths:
        b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        mime = _mime_for(path)
        data_url = f"data:{mime};base64,{b64}"
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
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

    with urllib.request.urlopen(req, timeout=900) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected API response: {body!r}") from e


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    return text


def _parse_model_response(text: str) -> dict:
    """Parse JSON with people_count, distinct_people, has_people, notes from model output."""
    text = _strip_json_fence(text)

    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        pass

    if not isinstance(obj, dict):
        m = re.search(r'"people_count"\s*:\s*(\d+)', text)
        if m:
            n = int(m.group(1))
            obj = {
                "people_count": n,
                "distinct_people": n,
                "has_people": n > 0,
                "notes": "",
            }
        else:
            raise ValueError(
                f"Could not parse JSON from model output: {text[:500]!r}"
            )

    if "people_count" not in obj:
        raise ValueError(f"Missing people_count in: {text[:500]!r}")

    n = int(obj["people_count"])
    if n < 0:
        n = 0

    dp = obj.get("distinct_people")
    if dp is None:
        dp = n
    else:
        dp = int(dp)
        if dp < 0:
            dp = 0
        if n > 0 and dp > n:
            dp = n

    notes = obj.get("notes", "")
    if notes is None:
        notes = ""
    notes = str(notes).strip()

    hp = obj.get("has_people")
    if isinstance(hp, str):
        hp = hp.lower() in ("true", "1", "yes")
    elif hp is None:
        hp = n > 0
    else:
        hp = bool(hp)

    if n == 0:
        hp = False
        dp = 0
    elif n > 0:
        hp = True

    return {"people_count": n, "distinct_people": dp, "has_people": hp, "notes": notes}


def _parse_set_unique_response(text: str) -> dict:
    text = _strip_json_fence(text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Bad set-level JSON: {text[:600]!r}") from e
    if not isinstance(obj, dict) or "unique_people_across_set" not in obj:
        raise ValueError(f"Bad set-level JSON: {text[:600]!r}")
    u = int(obj["unique_people_across_set"])
    if u < 0:
        u = 0
    conf = str(obj.get("confidence", "medium")).lower()
    if conf not in ("low", "medium", "high"):
        conf = "medium"
    notes = str(obj.get("notes", "") or "").strip()
    return {
        "unique_people_across_set": u,
        "confidence": conf,
        "notes": notes,
    }


def sidecar_path_for_image(image_path: Path) -> Path:
    """`photo.jpg` → `photo.jpg.meta.json` next to the image."""
    return sidecar.meta_path(image_path)


def write_sidecar_record(
    image_path: Path,
    parsed: dict,
    *,
    lm_base: str,
    model: str,
) -> dict:
    """Write sidecar JSON and return the full stored record."""
    record = {
        "people_count": parsed["people_count"],
        "distinct_people": parsed["distinct_people"],
        "has_people": parsed["has_people"],
        "notes": parsed["notes"],
        "model": model,
        "lm_studio_base": lm_base,
        "source": "analyze_uploads_people_lmstudio.py",
    }
    sidecar.write(image_path, record)
    return sidecar.read(image_path)


def _build_report_payload(
    *,
    base: str,
    model: str,
    uploads: Path,
    outcomes: list[dict],
    set_level: dict | None = None,
) -> dict:
    rows = []
    for o in outcomes:
        if o.get("error"):
            rows.append({"file": o["file"], "error": o["error"]})
        else:
            rows.append(
                {
                    "file": o["file"],
                    "people_count": o["people_count"],
                    "distinct_people": o.get("distinct_people"),
                    "has_people": o["has_people"],
                    "notes": o.get("notes", ""),
                    "sidecar_file": o.get("sidecar_file"),
                    "recorded_at": o.get("generated_at"),
                }
            )

    counts = {
        r["file"]: r["people_count"] for r in rows if r.get("people_count") is not None
    }
    failed = [{"file": r["file"], "error": r["error"]} for r in rows if r.get("error")]

    zeros = sorted(n for n, c in counts.items() if c == 0)
    vals = list(counts.values()) if counts else []
    unique = sorted(set(vals)) if vals else []

    same_all = None
    shared_count = None
    if len(unique) == 1 and vals:
        same_all = True
        shared_count = unique[0]
    elif len(unique) > 1:
        same_all = False

    breakdown = {}
    for k in sorted(set(vals)):
        breakdown[str(k)] = sorted(nm for nm, v in counts.items() if v == k)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lm_studio_base": base,
        "model": model,
        "uploads_dir": str(uploads.resolve()),
        "results": rows,
        "set_level_unique_people": set_level,
        "summary": {
            "images_attempted": len(outcomes),
            "images_with_count": len(counts),
            "images_failed": len(failed),
            "files_with_zero_people": zeros,
            "same_people_count_in_every_successful_image": same_all,
            "that_count_if_all_same": shared_count,
            "distinct_counts_observed": unique,
            "count_breakdown": breakdown,
            "failed": failed,
        },
        "limitations": REPORT_LIMITATIONS,
    }


def _write_report_files(prefix: Path, payload: dict) -> tuple[Path, Path]:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    s = payload["summary"]
    lines = [
        "# People count report (LM Studio)",
        "",
        f"- **Generated (UTC):** {payload['generated_at']}",
        f"- **LM Studio base:** `{payload['lm_studio_base']}`",
        f"- **Model:** `{payload['model']}`",
        f"- **Uploads folder:** `{payload['uploads_dir']}`",
        "",
        "## Summary",
        "",
        f"- Images processed: **{s['images_attempted']}** (with count: **{s['images_with_count']}**, failed: **{s['images_failed']}**)",
    ]
    if s["same_people_count_in_every_successful_image"] is True:
        lines.append(
            f"- Same people-count in every successful image: **yes** ({s['that_count_if_all_same']} people each)."
        )
    elif s["same_people_count_in_every_successful_image"] is False:
        lines.append(
            "- Same people-count in every successful image: **no** "
            f"(counts seen: {s['distinct_counts_observed']})."
        )
    sl = payload.get("set_level_unique_people")
    if sl:
        lines.extend(
            [
                "",
                "## Unique people across the whole set (one multi-image guess)",
                "",
                f"- **Estimated unique individuals:** {sl.get('unique_people_across_set', '?')}",
                f"- **Confidence:** {sl.get('confidence', '?')}",
                f"- **Notes:** {sl.get('notes', '—')}",
            ]
        )
    lines.extend(["", "## Per image", ""])

    for row in payload["results"]:
        if row["error"]:
            lines.append(f"- **{row['file']}:** ERROR — {row['error'][:500]}")
        else:
            hp = "yes" if row.get("has_people") else "no"
            notes = row.get("notes") or ""
            dp = row.get("distinct_people")
            extra = f", distinct_in_frame≈**{dp}**, has_people={hp}"
            if notes:
                extra += f' — notes: "{notes[:120]}{"…" if len(notes) > 120 else ""}"'
            lines.append(
                f"- **{row['file']}:** instances **{row['people_count']}**{extra}"
            )

    lines.extend(["", "## Photos with no people (count = 0)", ""])
    if s["files_with_zero_people"]:
        for n in s["files_with_zero_people"]:
            lines.append(f"- `{n}`")
    else:
        lines.append("- *(none)*")

    lines.extend(["", "## Count breakdown (successful images only)", ""])
    for k, names in sorted(s["count_breakdown"].items(), key=lambda x: int(x[0])):
        lines.append(f"- **{k} people:** {', '.join(f'`{x}`' for x in names)}")

    lines.extend(["", "## Could not get a count (API or parse errors)", ""])
    if s["failed"]:
        for f in s["failed"]:
            lines.append(f"- `{f['file']}`: {f['error'][:400]}")
    else:
        lines.append("- *(none)*")

    lines.extend(
        [
            "",
            "## What you cannot rely on (limitations)",
            "",
        ]
    )
    for item in payload["limitations"]:
        lines.append(f"- {item}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send all images in uploads/ to LM Studio and summarize people counts."
    )
    ap.add_argument(
        "--uploads",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "uploads",
        help="Directory containing images (default: project uploads/)",
    )
    ap.add_argument(
        "--model",
        required=True,
        help="Vision model id exactly as LM Studio exposes it (see GET /v1/models).",
    )
    ap.add_argument(
        "--base",
        default=os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1"),
        help="OpenAI-compatible base URL ending in /v1",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),
        help="Bearer token (LM Studio local usually accepts any)",
    )
    ap.add_argument(
        "--report",
        type=str,
        metavar="PREFIX",
        help="Write PREFIX.md and PREFIX.json (e.g. ./reports/people_latest)",
    )
    ap.add_argument(
        "--estimate-unique-across-set",
        action="store_true",
        help="After per-image analysis, send ALL images in one request to estimate "
        "unique individuals across the entire set (heavy; needs a capable VLM).",
    )
    ap.add_argument(
        "--max-images-set-analysis",
        type=int,
        default=16,
        metavar="N",
        help="Max images to include in the cross-set request (default: 16).",
    )
    args = ap.parse_args()

    base = args.base.rstrip("/")
    uploads: Path = args.uploads
    if not uploads.is_dir():
        print(f"Not a directory: {uploads}", file=sys.stderr)
        return 1

    files = sorted(
        p
        for p in uploads.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")
    )
    if not files:
        print(f"No images found in {uploads} ({', '.join(sorted(IMAGE_SUFFIXES))}).")
        return 0

    print(f"LM Studio base: {base}")
    print(f"Model: {args.model}")
    print(f"Images: {len(files)}\n")

    outcomes: list[dict] = []
    for i, path in enumerate(files, 1):
        rel = path.name
        print(f"[{i}/{len(files)}] {rel} …", flush=True)
        try:
            content = _post_chat_completions(base, args.api_key, args.model, path)
            parsed = _parse_model_response(content)
            full = write_sidecar_record(path, parsed, lm_base=base, model=args.model)
            sc = sidecar_path_for_image(path)
            outcomes.append(
                {
                    "file": rel,
                    "people_count": full["people_count"],
                    "distinct_people": full["distinct_people"],
                    "has_people": full["has_people"],
                    "notes": full["notes"],
                    "generated_at": full["generated_at"],
                    "sidecar_file": sc.name,
                    "error": None,
                }
            )
            print(
                f"    → people_count={full['people_count']} "
                f"distinct_people={full['distinct_people']} "
                f"has_people={full['has_people']}"
            )
            if full.get("notes"):
                print(f"       notes: {full['notes'][:120]}")
            print(f"       meta:  {sc.name}")
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, ValueError) as e:
            err = str(e)
            if isinstance(e, urllib.error.HTTPError):
                try:
                    err = e.read().decode("utf-8", errors="replace")[:800]
                except Exception:
                    pass
            outcomes.append({"file": rel, "error": err})
            print(f"    → ERROR: {err[:300]}")

    set_level: dict | None = None
    ok_paths = [
        uploads / o["file"]
        for o in outcomes
        if not o.get("error") and o.get("people_count") is not None
    ]
    if args.estimate_unique_across_set and ok_paths:
        chunk = ok_paths[: max(1, args.max_images_set_analysis)]
        if len(chunk) < len(ok_paths):
            print(
                f"\n(set analysis) Using first {len(chunk)} of {len(ok_paths)} images "
                f"(see --max-images-set-analysis)."
            )
        print(
            f"\nMulti-image request: estimating unique people across {len(chunk)} image(s)…",
            flush=True,
        )
        try:
            raw = _post_chat_completions_multi(
                base, args.api_key, args.model, chunk, SET_UNIQUE_PROMPT
            )
            parsed_set = _parse_set_unique_response(raw)
            set_level = {
                **parsed_set,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": args.model,
                "images_included": [p.name for p in chunk],
            }
            set_file = uploads / "_set_unique.meta.json"
            set_file.write_text(json.dumps(set_level, indent=2), encoding="utf-8")
            print(
                f"    → unique_people_across_set≈{set_level['unique_people_across_set']} "
                f"({set_level['confidence']})  → wrote {set_file.name}"
            )
            if set_level.get("notes"):
                print(f"       notes: {set_level['notes'][:200]}")
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, ValueError) as e:
            err = str(e)
            if isinstance(e, urllib.error.HTTPError):
                try:
                    err = e.read().decode("utf-8", errors="replace")[:800]
                except Exception:
                    pass
            print(f"    → SET-LEVEL ERROR: {err[:400]}")

    payload = _build_report_payload(
        base=base,
        model=args.model,
        uploads=uploads,
        outcomes=outcomes,
        set_level=set_level,
    )
    if args.report:
        md_p, js_p = _write_report_files(Path(args.report), payload)
        print("\nReport written:")
        print(f"  {md_p}")
        print(f"  {js_p}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    failed = [o for o in outcomes if o.get("error")]

    if failed:
        print("\nFailed files:")
        for o in failed:
            print(f"  - {o['file']}: {o['error'][:200]}")

    counts = {
        o["file"]: o["people_count"]
        for o in outcomes
        if not o.get("error") and o.get("people_count") is not None
    }
    if not counts:
        print("\nNo successful counts; fix errors and retry.")
        return 2

    print("\nPer file (instances / distinct in frame):")
    for name in sorted(counts.keys()):
        o = next((x for x in outcomes if x.get("file") == name), {})
        dp = o.get("distinct_people", "?")
        print(f"  {name}: {counts[name]} instances, distinct≈{dp}")

    zeros = [n for n, c in counts.items() if c == 0]
    if zeros:
        print(f"\nPhotos with NO people ({len(zeros)}):")
        for n in sorted(zeros):
            print(f"  - {n}")
    else:
        print("\nPhotos with NO people: none")

    vals = list(counts.values())
    unique = set(vals)
    if len(unique) == 1:
        print(
            f"\nSame people-count in every photo: YES — {vals[0]} people in each "
            f"({len(vals)} photo(s))."
        )
    else:
        print(
            f"\nSame people-count in every photo: NO — counts vary: {sorted(unique)}"
        )
        by_count: Counter[int] = Counter(vals)
        print("Breakdown:")
        for k in sorted(by_count.keys()):
            names = sorted(nm for nm, v in counts.items() if v == k)
            print(f"  {k} people: {len(names)} file(s) — {', '.join(names)}")

    if set_level:
        print("\nAcross entire set (multi-image estimate):")
        print(
            f"  Unique people (estimated): {set_level['unique_people_across_set']} "
            f"[confidence: {set_level['confidence']}]"
        )
        if set_level.get("notes"):
            print(f"  Notes: {set_level['notes'][:300]}")

    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())
