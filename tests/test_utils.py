"""Unit tests for the pure helpers in app.utils."""
from datetime import datetime, timezone

import pytest

from app import utils


class TestNormalizeName:
    def test_lowercases_and_strips(self):
        assert utils.normalize_name("  Milk  ") == "milk"

    def test_collapses_internal_whitespace(self):
        assert utils.normalize_name("Foo   Bar") == "foo bar"

    def test_strips_accents(self):
        assert utils.normalize_name("Café") == "cafe"
        assert utils.normalize_name("ÄÖÜ") == "aou"

    def test_none_and_empty(self):
        assert utils.normalize_name(None) == ""
        assert utils.normalize_name("") == ""


class TestParseQuantity:
    @pytest.mark.parametrize(
        "text,qty,remainder",
        [
            ("10 Eier", 10.0, "Eier"),
            ("2.5 kg", 2.5, "kg"),
            ("2,5 kg", 2.5, "kg"),
            ("1/2 cup", 0.5, "cup"),
            ("1 1/2 cups", 1.5, "cups"),
            ("½", 0.5, ""),
            ("1½ l", 1.5, "l"),
            ("Milk", None, "Milk"),
            ("", None, ""),
            (None, None, ""),
        ],
    )
    def test_parse(self, text, qty, remainder):
        got_qty, got_rem = utils.parse_quantity(text)
        assert got_qty == qty
        assert got_rem == remainder


class TestFormatQuantity:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, ""),
            (0, ""),
            (1, "1"),
            (2.0, "2"),
            (2.5, "2.5"),
            (0.125, "0.125"),
        ],
    )
    def test_format(self, value, expected):
        assert utils.format_quantity(value) == expected


class TestStableHash:
    def test_deterministic(self):
        a = utils.stable_hash({"x": 1, "y": "two"})
        b = utils.stable_hash({"x": 1, "y": "two"})
        assert a == b

    def test_key_order_independent(self):
        a = utils.stable_hash({"x": 1, "y": 2})
        b = utils.stable_hash({"y": 2, "x": 1})
        assert a == b

    def test_changes_with_value(self):
        a = utils.stable_hash({"checked": False})
        b = utils.stable_hash({"checked": True})
        assert a != b


class TestIsQuietNow:
    def test_empty_disabled(self):
        assert utils.is_quiet_now("", "UTC") is False

    def test_invalid_disabled(self):
        assert utils.is_quiet_now("not-a-window", "UTC") is False

    def test_equal_bounds_disabled(self):
        assert utils.is_quiet_now("08:00-08:00", "UTC") is False

    def _freeze(self, monkeypatch, hour, minute=0):
        fixed = datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return fixed

        monkeypatch.setattr(utils, "datetime", FakeDatetime)

    def test_overnight_inside(self, monkeypatch):
        self._freeze(monkeypatch, 2)
        assert utils.is_quiet_now("23:00-07:00", "UTC") is True

    def test_overnight_outside(self, monkeypatch):
        self._freeze(monkeypatch, 12)
        assert utils.is_quiet_now("23:00-07:00", "UTC") is False

    def test_daytime_inside(self, monkeypatch):
        self._freeze(monkeypatch, 10)
        assert utils.is_quiet_now("09:00-17:00", "UTC") is True

    def test_daytime_outside(self, monkeypatch):
        self._freeze(monkeypatch, 20)
        assert utils.is_quiet_now("09:00-17:00", "UTC") is False
