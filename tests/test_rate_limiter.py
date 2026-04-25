"""Tests for the per-IP token-bucket rate limiter."""
import time
import pytest

from glados.auth.rate_limit import TokenBucket


def test_bucket_allows_up_to_capacity():
    b = TokenBucket(capacity=3, window_seconds=60)
    assert b.allow("1.2.3.4")
    assert b.allow("1.2.3.4")
    assert b.allow("1.2.3.4")
    assert not b.allow("1.2.3.4")


def test_bucket_isolates_different_keys():
    b = TokenBucket(capacity=2, window_seconds=60)
    b.allow("a"); b.allow("a")
    assert not b.allow("a")
    assert b.allow("b")


def test_bucket_refills_after_window(monkeypatch):
    b = TokenBucket(capacity=1, window_seconds=1)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    assert b.allow("x")
    assert not b.allow("x")
    now[0] += 2.0
    assert b.allow("x")


def test_bucket_partial_refill(monkeypatch):
    """After half a window, half the capacity should be available."""
    b = TokenBucket(capacity=10, window_seconds=10)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    # Drain
    for _ in range(10):
        assert b.allow("x")
    assert not b.allow("x")
    # Half a window later → ~5 tokens refilled
    now[0] += 5.0
    consumed = 0
    while b.allow("x"):
        consumed += 1
        if consumed > 20:
            break  # safety
    assert 4 <= consumed <= 6, f"expected ~5 tokens after half a window, got {consumed}"


def test_reset_single_key():
    b = TokenBucket(capacity=1, window_seconds=60)
    b.allow("a")
    assert not b.allow("a")
    b.reset("a")
    assert b.allow("a")


def test_reset_all_keys():
    b = TokenBucket(capacity=1, window_seconds=60)
    b.allow("a"); b.allow("b")
    b.reset()
    assert b.allow("a")
    assert b.allow("b")


def test_thread_safe():
    """Concurrent allow() calls don't double-spend a token."""
    import threading
    b = TokenBucket(capacity=100, window_seconds=60)
    accepted = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            if b.allow("shared"):
                with lock:
                    accepted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    # At most capacity tokens consumed; no over-spending.
    assert sum(accepted) <= 100


# ── Wired into tts_ui ─────────────────────────────────────────

def test_service_limiter_returns_429_when_exhausted(monkeypatch):
    """Drain the service limiter, confirm next /api/voices request returns 429."""
    from glados.webui import tts_ui
    from unittest.mock import MagicMock

    # Reset bucket to a tight 1-request capacity for this test
    monkeypatch.setattr(tts_ui, "_service_limiter",
                        TokenBucket(capacity=1, window_seconds=60))

    def make_handler():
        h = MagicMock()
        h.client_address = ("9.9.9.9", 0)
        h._sent = []
        h.send_response = lambda c: h._sent.append(("status", c))
        h.send_header = lambda k, v: h._sent.append(("header", k, v))
        h.end_headers = lambda: None
        import io
        h.wfile = io.BytesIO()
        return h

    # First request consumes the token.
    h1 = make_handler()
    assert tts_ui._service_rate_limit_check(h1) is True

    # Second request hits the empty bucket and gets 429.
    h2 = make_handler()
    assert tts_ui._service_rate_limit_check(h2) is False
    statuses = [e[1] for e in h2._sent if e[0] == "status"]
    assert 429 in statuses
    headers = [(e[1], e[2]) for e in h2._sent if e[0] == "header"]
    assert ("Retry-After", "60") in headers
