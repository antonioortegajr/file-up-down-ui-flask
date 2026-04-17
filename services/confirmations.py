"""
Confirmation data layer for file-up-down-ui-flask.

Pure-Python module — no Flask imports. Provides all read/write operations
for the interactive confirmation engine.

Sessions live at:   people/<pid>/sessions/<sid>.json
Confirmations at:   people/<pid>/confirmations.json
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constants (overridable via env vars)
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD: float = float(os.environ.get("CONFIRM_THRESHOLD", "0.6"))
MIN_VOTES: int = int(os.environ.get("CONFIRM_MIN_VOTES", "2"))
MAX_SESSIONS_PER_PHOTO: int = 50
SESSION_TTL_DAYS: int = 7

# ---------------------------------------------------------------------------
# Module-level dict of per-person write locks (same pattern as _jobs_lock)
# ---------------------------------------------------------------------------

_person_locks: dict[str, threading.Lock] = {}
_person_locks_lock = threading.Lock()


def _get_person_lock(person_id: str) -> threading.Lock:
    """Return (creating if needed) the threading.Lock for a given person_id."""
    with _person_locks_lock:
        if person_id not in _person_locks:
            _person_locks[person_id] = threading.Lock()
        return _person_locks[person_id]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _people_dir() -> Path:
    """Return the base people/ directory (sibling of uploads/)."""
    return Path(os.getcwd()) / "people"


def confirmations_path(person_id: str) -> Path:
    """Return the path to people/<pid>/confirmations.json."""
    return _people_dir() / person_id / "confirmations.json"


def _sessions_dir(person_id: str) -> Path:
    """Return the path to people/<pid>/sessions/."""
    return _people_dir() / person_id / "sessions"


def _session_path(person_id: str, session_id: str) -> Path:
    """Return the path to people/<pid>/sessions/<sid>.json."""
    return _sessions_dir(person_id) / f"{session_id}.json"


# ---------------------------------------------------------------------------
# Confirmations file read / write
# ---------------------------------------------------------------------------

def read_confirmations(person_id: str) -> dict:
    """
    Read people/<pid>/confirmations.json.
    Returns an empty scaffold if the file does not exist or cannot be parsed.
    """
    path = confirmations_path(person_id)
    if not path.is_file():
        return {"person_id": person_id, "updated_at": "", "photos": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"person_id": person_id, "updated_at": "", "photos": {}}


def write_confirmations(person_id: str, data: dict) -> None:
    """
    Write data to people/<pid>/confirmations.json.
    Uses a threading.Lock keyed on person_id to prevent concurrent write corruption.
    """
    lock = _get_person_lock(person_id)
    with lock:
        path = confirmations_path(person_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure confidence calculation
# ---------------------------------------------------------------------------

def get_confidence(yes_votes: int, no_votes: int) -> float:
    """
    Return yes / (yes + no). Returns 0.0 if both are 0.
    Pure function — no I/O.
    """
    total = yes_votes + no_votes
    if total == 0:
        return 0.0
    return yes_votes / total


# ---------------------------------------------------------------------------
# Vote recording
# ---------------------------------------------------------------------------

def record_vote(
    person_id: str,
    session_id: str,
    filename: str,
    vote: str,
) -> dict:
    """
    Append a vote to a photo's sessions list, recalculate confidence, and
    update the confirmed flag.  Returns the updated photo record.

    ``vote`` must be ``"yes"`` or ``"no"`` — callers must filter out ``"skip"``
    before calling this function.

    Sessions list is trimmed to MAX_SESSIONS_PER_PHOTO oldest entries; vote
    totals (yes_votes / no_votes) are preserved across the trim.
    """
    if vote not in ("yes", "no"):
        raise ValueError(f"vote must be 'yes' or 'no', got: {vote!r}")

    now_iso = datetime.now(timezone.utc).isoformat()

    lock = _get_person_lock(person_id)
    with lock:
        data = read_confirmations(person_id)
        photos: dict[str, Any] = data.setdefault("photos", {})

        photo = photos.setdefault(
            filename,
            {
                "yes_votes": 0,
                "no_votes": 0,
                "last_session": "",
                "confirmed": False,
                "confidence": 0.0,
                "sessions": [],
            },
        )

        # Accumulate vote totals
        if vote == "yes":
            photo["yes_votes"] = photo.get("yes_votes", 0) + 1
        else:
            photo["no_votes"] = photo.get("no_votes", 0) + 1

        # Append session entry
        session_entry = {
            "session_id": session_id,
            "vote": vote,
            "voted_at": now_iso,
            "source": "human",
        }
        sessions_list: list = photo.setdefault("sessions", [])
        sessions_list.append(session_entry)

        # Trim sessions list to MAX_SESSIONS_PER_PHOTO; vote totals already
        # accumulated above so trimming does not lose them.
        if len(sessions_list) > MAX_SESSIONS_PER_PHOTO:
            photo["sessions"] = sessions_list[-MAX_SESSIONS_PER_PHOTO:]

        # Recalculate confidence and confirmed flag
        yes_votes: int = photo["yes_votes"]
        no_votes: int = photo["no_votes"]
        confidence = get_confidence(yes_votes, no_votes)
        total_votes = yes_votes + no_votes

        photo["confidence"] = confidence
        photo["confirmed"] = (
            confidence >= CONFIDENCE_THRESHOLD and total_votes >= MIN_VOTES
        )
        photo["last_session"] = now_iso

        data["updated_at"] = now_iso

        # Write without re-acquiring the lock (already held)
        path = confirmations_path(person_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return photo


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(person_id: str, photo_queue: list[str]) -> str:
    """
    Create a new session file at people/<pid>/sessions/<new_uuid>.json.
    Returns the new session_id.
    """
    session_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    session_data = {
        "session_id": session_id,
        "person_id": person_id,
        "created_at": now_iso,
        "last_active": now_iso,
        "status": "active",
        "queue": list(photo_queue),
        "answered": {},
        "current_index": 0,
    }
    sessions_dir = _sessions_dir(person_id)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _session_path(person_id, session_id).write_text(
        json.dumps(session_data, indent=2), encoding="utf-8"
    )
    return session_id


def get_session(person_id: str, session_id: str) -> dict | None:
    """
    Read people/<pid>/sessions/<sid>.json.
    Returns None if the file does not exist or cannot be parsed.
    """
    path = _session_path(person_id, session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def save_session(person_id: str, session_id: str, session_data: dict) -> None:
    """
    Write session_data to people/<pid>/sessions/<sid>.json.
    Uses a threading.Lock keyed on person_id to prevent concurrent write corruption.
    """
    lock = _get_person_lock(person_id)
    with lock:
        path = _session_path(person_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session_data, indent=2), encoding="utf-8")


def advance_session(person_id: str, session_id: str) -> dict:
    """
    Increment current_index for the session.
    Sets status to "complete" if the queue is exhausted.
    Updates last_active to now.
    Returns the updated session dict.
    """
    lock = _get_person_lock(person_id)
    with lock:
        session = get_session(person_id, session_id)
        if session is None:
            raise FileNotFoundError(
                f"Session not found: person={person_id} session={session_id}"
            )

        session["current_index"] = session.get("current_index", 0) + 1
        session["last_active"] = datetime.now(timezone.utc).isoformat()

        queue_length = len(session.get("queue", []))
        if session["current_index"] >= queue_length:
            session["status"] = "complete"

        path = _session_path(person_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")

    return session


def find_active_session(person_id: str) -> str | None:
    """
    Scan people/<pid>/sessions/ for a session with status ``"active"`` whose
    last_active timestamp is within SESSION_TTL_DAYS.
    Returns the session_id string or None.
    """
    sessions_dir = _sessions_dir(person_id)
    if not sessions_dir.is_dir():
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)

    for session_file in sessions_dir.iterdir():
        if not session_file.is_file() or session_file.suffix != ".json":
            continue
        try:
            session = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue

        if session.get("status") != "active":
            continue

        last_active_str: str = session.get("last_active", "")
        if not last_active_str:
            continue

        try:
            last_active = datetime.fromisoformat(last_active_str)
            # Ensure timezone-aware for comparison
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if last_active >= cutoff:
            return session.get("session_id")

    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(person_id: str) -> dict:
    """
    Return a summary report for a person.

    Shape:
    {
        "person_id": str,
        "person_name": str,
        "generated_at": ISO8601,
        "total_photos": int,
        "confirmed_count": int,
        "photos": [
            {
                "filename": str,
                "yes_votes": int,
                "no_votes": int,
                "confidence": float,
                "confirmed": bool,
                "last_session": ISO8601,
            },
            ...   # sorted by confidence descending
        ]
    }
    """
    # Attempt to read person name from person.json
    person_name = ""
    person_file = _people_dir() / person_id / "person.json"
    if person_file.is_file():
        try:
            person_data = json.loads(person_file.read_text(encoding="utf-8"))
            person_name = person_data.get("name", "")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    data = read_confirmations(person_id)
    photos_dict: dict[str, Any] = data.get("photos", {})

    photos_list = []
    confirmed_count = 0
    for filename, photo in photos_dict.items():
        entry = {
            "filename": filename,
            "yes_votes": photo.get("yes_votes", 0),
            "no_votes": photo.get("no_votes", 0),
            "confidence": photo.get("confidence", 0.0),
            "confirmed": photo.get("confirmed", False),
            "last_session": photo.get("last_session", ""),
        }
        photos_list.append(entry)
        if entry["confirmed"]:
            confirmed_count += 1

    photos_list.sort(key=lambda p: p["confidence"], reverse=True)

    return {
        "person_id": person_id,
        "person_name": person_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_photos": len(photos_list),
        "confirmed_count": confirmed_count,
        "photos": photos_list,
    }
