import hashlib
import json
import re
import unicodedata
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_hm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def is_quiet_now(quiet_hours: str, tz_name: str) -> bool:
    """True if the current local time falls inside the quiet-hours window.

    ``quiet_hours`` is ``"HH:MM-HH:MM"``; empty/invalid disables (returns False).
    Handles overnight windows (e.g. ``"23:00-07:00"``).
    """
    if not quiet_hours.strip():
        return False
    try:
        start_s, end_s = quiet_hours.split("-")
        start, end = _parse_hm(start_s), _parse_hm(end_s)
    except (ValueError, TypeError):
        return False
    if start == end:
        return False
    try:
        now = datetime.now(ZoneInfo(tz_name)).time()
    except Exception:
        now = utcnow().time()
    if start < end:
        return start <= now < end
    return now >= start or now < end  # overnight window


def normalize_name(name: str | None) -> str:
    """Normalized food name used as a *fallback* matcher only.

    Lower-cased, accent-stripped, whitespace-collapsed.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode()
    ascii_name = ascii_name.lower().strip()
    return re.sub(r"\s+", " ", ascii_name)


def stable_hash(payload: dict) -> str:
    """Deterministic hash of a small dict, used to detect field-level changes."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
