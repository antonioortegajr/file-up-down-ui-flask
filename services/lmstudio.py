import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

LMS = "lms"

LMSTUDIO_BASE = os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1")
LMSTUDIO_API_KEY = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")
LMSTUDIO_MODEL = os.environ.get("LMSTUDIO_MODEL", "")


def describe_photo(image_path: str, base_url: str, model: str, api_key: str) -> str | None:
    """
    Send an image to LM Studio and get a description.
    Returns the description text, or None on failure.
    """
    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

    prompt = "Describe this photo in 1-2 sentences. Focus on people, setting, and activity."
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_data}"}}
                ]
            }
        ],
        "max_tokens": 200,
    }

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        return None


def describe_new_photos(job, image_paths: list[str], base_url: str, model: str, api_key: str) -> dict:
    """
    Background job to describe new photos.
    job: Job object with set_progress method
    image_paths: list of image file paths (full paths)
    """
    from services import sidecar

    total = len(image_paths)
    results = {"described": [], "failed": []}

    for idx, path in enumerate(image_paths):
        job.set_progress(int((idx / total) * 100), f"Describing {idx + 1}/{total}...")
        desc = describe_photo(path, base_url, model, api_key)
        if desc:
            sidecar.write_desc_cache(path, {"description": desc})
            results["described"].append(path)
        else:
            results["failed"].append(path)

    job.set_progress(100, f"Done: {len(results['described'])} described, {len(results['failed'])} failed")
    return results


def server_is_up(base_url: str = LMSTUDIO_BASE) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=3):
            return True
    except Exception:
        return False


def model_is_loaded(base_url: str = LMSTUDIO_BASE, model: str = LMSTUDIO_MODEL) -> bool:
    if not model:
        return False
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=5) as r:
            data = json.loads(r.read())
        return any(m.get("id") == model for m in data.get("data", []))
    except Exception:
        return False


def _run_lms(*args: str) -> bool:
    try:
        result = subprocess.run([LMS, *args])
        return result.returncode == 0
    except FileNotFoundError:
        return False


def ensure_ready(base_url: str = LMSTUDIO_BASE, model: str = LMSTUDIO_MODEL) -> None:
    if not server_is_up(base_url):
        if not _run_lms("server", "start"):
            raise RuntimeError("Could not start LM Studio server.")
        for _ in range(15):
            time.sleep(1)
            if server_is_up(base_url):
                break
        else:
            raise RuntimeError(f"LM Studio server did not respond at {base_url} after 15 s.")
    if not model_is_loaded(base_url, model):
        if not _run_lms("load", model):
            raise RuntimeError(f"Could not load model '{model}'.")


def _load_image_base64(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def _compare_two_photos(
    ref_path: str,
    candidate_path: str,
    person_name: str,
    base_url: str,
    model: str,
    api_key: str,
) -> str | None:
    ref_b64 = _load_image_base64(ref_path)
    candidate_b64 = _load_image_base64(candidate_path)
    if not ref_b64 or not candidate_b64:
        return None

    prompt = f"Image 1 is a reference photo of {person_name}. Is this same person visible in Image 2? Answer YES or NO then one sentence."
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{ref_b64}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{candidate_b64}"}},
                ]
            }
        ],
        "max_tokens": 100,
    }

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        return None


def match_person_in_library(
    job,
    person_id: str,
    people_folder: str,
    uploads_folder: str,
    base_url: str,
    model: str,
    api_key: str,
) -> dict:
    """
    Background job to find all photos of a person in the uploads library.
    job: Job object with set_progress method
    person_id: UUID of the person
    people_folder: Path to people directory
    uploads_folder: Path to uploads directory
    """
    person_file = os.path.join(people_folder, person_id, "person.json")
    if not os.path.isfile(person_file):
        raise FileNotFoundError(f"Person not found: {person_id}")

    with open(person_file, "r", encoding="utf-8") as f:
        person = json.load(f)

    name = person.get("name", "Unknown")
    reference_photos = person.get("reference_photos", [])
    if not reference_photos:
        raise ValueError(f"No reference photos for person: {person_id}")

    ref_path = os.path.join(people_folder, person_id, reference_photos[0])

    files = [
        f
        for f in os.listdir(uploads_folder)
        if os.path.isfile(os.path.join(uploads_folder, f))
        and not f.startswith(".")
        and not f.endswith(".meta.json")
        and f != "_set_unique.meta.json"
        and not f.endswith(".desc_cache")
    ]

    total = len(files)
    if total == 0:
        job.set_progress(100, "No files to scan.")
        return {"matches": [], "no_match": [], "failed": []}

    matches = []
    no_match = []
    failed = []

    for idx, filename in enumerate(files):
        ensure_ready(base_url, model)

        candidate_path = os.path.join(uploads_folder, filename)
        job.set_progress(
            int((idx / total) * 100),
            f"[{idx + 1}/{total}] {filename} ...",
            current_file=filename,
        )

        answer = _compare_two_photos(ref_path, candidate_path, name, base_url, model, api_key)

        if answer is None:
            failed.append(filename)
            job.set_progress(
                int((idx / total) * 100),
                f"[{idx + 1}/{total}] {filename} ... FAILED",
                current_file=filename,
                answer="FAILED",
            )
        elif answer.upper().startswith("YES"):
            matches.append(filename)
            job.set_progress(
                int((idx / total) * 100),
                f"[{idx + 1}/{total}] {filename} ... YES",
                current_file=filename,
                answer="YES",
            )
        else:
            no_match.append(filename)
            job.set_progress(
                int((idx / total) * 100),
                f"[{idx + 1}/{total}] {filename} ... NO",
                current_file=filename,
                answer="NO",
            )

        time.sleep(0.5)

    job.set_progress(100, f"Done: {len(matches)} matches, {len(no_match)} no match, {len(failed)} failed")
    return {"matches": matches, "no_match": no_match, "failed": failed}