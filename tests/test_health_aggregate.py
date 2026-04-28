"""Tests for /api/health/aggregate — feeds the sidebar status dot."""
from glados.webui.tts_ui import _build_health_aggregate


def test_aggregate_unauth_returns_unauth_only():
    result = _build_health_aggregate(authenticated=False, probes=None)
    assert result == {"overall": "unauth"}


def test_aggregate_all_ok():
    probes = [
        ("API", True), ("TTS", True), ("STT", True),
        ("HA", True), ("ChromaDB", True),
    ]
    result = _build_health_aggregate(authenticated=True, probes=probes)
    assert result["overall"] == "ok"
    assert len(result["services"]) == 5
    assert all(s["status"] == "ok" for s in result["services"])


def test_aggregate_one_degraded_overall_degraded():
    probes = [("API", True), ("TTS", "degraded"), ("STT", True)]
    result = _build_health_aggregate(authenticated=True, probes=probes)
    assert result["overall"] == "degraded"


def test_aggregate_one_down_overall_down():
    probes = [("API", True), ("Vision", False), ("STT", True)]
    result = _build_health_aggregate(authenticated=True, probes=probes)
    assert result["overall"] == "down"


def test_aggregate_down_dominates_degraded():
    """If any service is down, overall is 'down', not 'degraded'."""
    probes = [("API", "degraded"), ("Vision", False)]
    result = _build_health_aggregate(authenticated=True, probes=probes)
    assert result["overall"] == "down"
