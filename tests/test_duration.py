"""Tests for the session_timeout duration parser."""
import pytest
from glados.core.duration import parse_duration, NEVER


@pytest.mark.parametrize("s, expected", [
    ("never", NEVER),
    ("0", 0),
    ("60", 60),
    ("30s", 30),
    ("5m", 5 * 60),
    ("2h", 2 * 60 * 60),
    ("30d", 30 * 24 * 60 * 60),
    ("1w", 7 * 24 * 60 * 60),
    ("2W", 14 * 24 * 60 * 60),
])
def test_parse_duration_valid(s, expected):
    assert parse_duration(s) == expected


@pytest.mark.parametrize("s", ["", "abc", "2x", "-1d", "1.5h"])
def test_parse_duration_invalid_raises(s):
    with pytest.raises(ValueError):
        parse_duration(s)


def test_never_sentinel():
    assert NEVER is None
