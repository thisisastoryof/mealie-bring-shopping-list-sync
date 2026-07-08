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


_UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}
_FRAC_CLASS = "".join(_UNICODE_FRACTIONS)


def parse_quantity(text: str | None) -> tuple[float | None, str]:
    """Split a leading quantity off a free-text spec.

    Returns ``(quantity, remainder)``. Handles integers, decimals (``2.5`` /
    ``2,5``), simple fractions (``1/2``), mixed numbers (``1 1/2``) and unicode
    fractions (``½`` / ``1½``). If no leading quantity is found, returns
    ``(None, <stripped original>)``. Used to turn a Bring ``spec`` into Mealie's
    structured ``quantity`` so items round-trip cleanly (no "1 10 Eier").
    """
    s = (text or "").strip()
    if not s:
        return None, ""
    # mixed number with ascii fraction: "1 1/2"
    m = re.match(r"^(\d+)\s+(\d+)\s*/\s*(\d+)\b\s*(.*)$", s)
    if m and int(m.group(3)):
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)), m.group(4).strip()
    # mixed number with unicode fraction: "1½"
    m = re.match(rf"^(\d+)\s*([{_FRAC_CLASS}])\s*(.*)$", s)
    if m:
        return int(m.group(1)) + _UNICODE_FRACTIONS[m.group(2)], m.group(3).strip()
    # lone unicode fraction: "½"
    m = re.match(rf"^([{_FRAC_CLASS}])\s*(.*)$", s)
    if m:
        return _UNICODE_FRACTIONS[m.group(1)], m.group(2).strip()
    # ascii fraction: "1/2"
    m = re.match(r"^(\d+)\s*/\s*(\d+)\b\s*(.*)$", s)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2)), m.group(3).strip()
    # decimal or integer: "2.5", "2,5", "10"
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(.*)$", s)
    if m:
        return float(m.group(1).replace(",", ".")), m.group(2).strip()
    return None, s


def stable_hash(payload: dict) -> str:
    """Deterministic hash of a small dict, used to detect field-level changes."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}
_UF = "".join(_UNICODE_FRACTIONS)


def parse_quantity(text: str | None) -> tuple[float | None, str]:
    """Split a leading numeric quantity from free text.

    Handles integers, decimals (``.`` or ``,``), simple fractions (``1/2``),
    mixed numbers (``1 1/2``) and unicode vulgar fractions (``½``, ``1½``).
    Returns ``(quantity_or_None, remainder_text)``. This intentionally only
    parses the *amount* — Bring already separates the item name from its spec,
    so no full-string ingredient parsing (the old HA approach) is needed.
    """
    if not text:
        return None, ""
    s = text.strip()
    # whole? + unicode fraction, e.g. "½" or "1½" / "1 ½"
    m = re.match(rf"^(\d+)?\s*([{_UF}])\s*(.*)$", s)
    if m:
        whole = float(m.group(1)) if m.group(1) else 0.0
        return whole + _UNICODE_FRACTIONS[m.group(2)], m.group(3).strip()
    # mixed number "1 1/2"
    m = re.match(r"^(\d+)\s+(\d+)\s*/\s*(\d+)\s*(.*)$", s)
    if m:
        return float(m.group(1)) + float(m.group(2)) / float(m.group(3)), m.group(4).strip()
    # simple fraction "1/2"
    m = re.match(r"^(\d+)\s*/\s*(\d+)\s*(.*)$", s)
    if m:
        return float(m.group(1)) / float(m.group(2)), m.group(3).strip()
    # decimal or integer "10", "0.5", "0,5"
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(.*)$", s)
    if m:
        return float(m.group(1).replace(",", ".")), m.group(2).strip()
    return None, s


def format_quantity(quantity: float | None) -> str:
    """Render a quantity for a Bring spec: drop trailing zeros, no ``.0``."""
    if not quantity:
        return ""
    if float(quantity).is_integer():
        return str(int(quantity))
    return f"{quantity:.3f}".rstrip("0").rstrip(".")

