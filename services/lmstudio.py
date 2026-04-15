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