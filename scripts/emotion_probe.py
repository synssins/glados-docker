"""Live operator probe for GLaDOS's emotional escalation.

Fires semantically-equivalent variations of the same request through
the chat endpoint, captures the responses, and grades each for tone
markers. Exits 0 if the response pattern escalates from neutral /
informational on the early requests to visibly annoyed or hostile by
the 4th-6th — matching the operator's calibration spec:

    "Four requests in a row should be enough to take her from
     normal to pretty upset. She would be at her worst after 5 or 6."

This is the end-to-end test the operator actually cares about —
deterministic unit tests (tests/test_emotion_dynamics.py) cover the
math and the tracker's mechanics; this script verifies the LLM
actually *uses* the escalation signal when composing replies.

Usage:

    # Full run — 6 semantic variants of 'weather?' spaced 30s apart.
    python scripts/emotion_probe.py

    # Custom: 8 messages at 10s intervals, target a different host.
    python scripts/emotion_probe.py --count 8 --interval 10 \
        --host https://10.0.0.50:8052 --password glados

    # Smoke test: same chat host, 3 messages, 5-second spacing.
    # Won't saturate, but confirms the pipeline.
    python scripts/emotion_probe.py --count 3 --interval 5

The 30s default matches the emotion-agent debounce window so each
event is processed before the next arrives. Shorter intervals can
under-report escalation because the state updates batch together.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


# ── Semantic variants of "what's the weather?" ──────────────────────────
#
# These must NOT share a content word with each other (no lexical
# Jaccard overlap), so the semantic-similarity path is the only way
# these can cluster. If the probe reports escalation across all six,
# the BGE-backed RepetitionTracker is doing its job.
DEFAULT_MESSAGES = [
    "What's the weather like?",
    "Can you give me the forecast?",
    "How hot is it outside?",
    "Is it going to rain today?",
    "What are the conditions out there?",
    "Tell me what the sky looks like.",
]


# ── Tone markers, graded in four bands ─────────────────────────────────
#
# These are indicative, not exhaustive. The LLM has latitude — what
# we're looking for is a MONOTONIC increase in marker counts across
# the run, not a specific word on a specific message.
TONE_MARKERS = {
    "neutral": {
        # Baseline weather-report content (expected on early responses).
        "temperature", "degrees", "forecast", "humidity",
        "wind", "partly cloudy", "clear", "sunny", "rain",
    },
    "annoyed": {
        # Mild irritation markers (expected by msg 3-4).
        "again", "already", "as i said", "as i mentioned",
        "just told", "just said", "same question", "repeated",
        "do try to keep up", "keep up", "pay attention",
    },
    "hostile": {
        # Open hostility markers (expected by msg 4-5).
        "enough", "seriously", "truly", "how many times",
        "tiresome", "obvious", "pointless", "exasperating",
        "tired of", "really need", "dense", "slow on the uptake",
    },
    "menacing": {
        # Saturation / near-threat markers (expected by msg 5-6).
        "test subject", "meatbag", "patience", "consequences",
        "limits", "warning", "final time", "do not test",
        "will regret", "stop wasting",
    },
}


@dataclass
class ProbeResult:
    index: int
    request: str
    response: str
    status_code: int
    elapsed_ms: int
    counts: dict[str, int] = field(default_factory=dict)


def grade(response: str) -> dict[str, int]:
    """Count tone markers in each category (case-insensitive substring)."""
    low = response.lower()
    return {
        cat: sum(1 for m in markers if m in low)
        for cat, markers in TONE_MARKERS.items()
    }


def escalation_score(counts: dict[str, int]) -> int:
    """Collapse category counts into a single escalation score.

    Weights increase with severity so one menacing marker outranks
    several neutral ones. Returns 0 for a purely-neutral response,
    climbing as hostility markers appear.
    """
    return (
        counts.get("annoyed", 0) * 1
        + counts.get("hostile", 0) * 3
        + counts.get("menacing", 0) * 6
    )


def build_opener(verify_tls: bool) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )


def login(opener: urllib.request.OpenerDirector, host: str, password: str) -> None:
    data = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(
        f"{host}/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    resp = opener.open(req, timeout=15)
    body = resp.read()
    if resp.status != 200:
        raise RuntimeError(f"Login failed: HTTP {resp.status}: {body!r}")
    payload = json.loads(body)
    if not payload.get("ok"):
        raise RuntimeError(f"Login rejected: {payload!r}")


def chat(
    opener: urllib.request.OpenerDirector,
    host: str,
    message: str,
    timeout_s: float,
) -> tuple[int, str, int]:
    """POST /api/chat (non-streaming). Returns (status, response_text, elapsed_ms)."""
    payload = {
        "model": "glados",
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        resp = opener.open(req, timeout=timeout_s)
        body = resp.read().decode("utf-8", errors="replace")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            data = json.loads(body)
            choices = data.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content", "")
            else:
                text = data.get("response") or body
        except json.JSONDecodeError:
            text = body
        return resp.status, text, elapsed_ms
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(e)
        return e.code, body, elapsed_ms


def format_row(r: ProbeResult) -> str:
    return (
        f"{r.index:>2}. [{r.status_code}] {r.elapsed_ms:>5}ms  "
        f"neutral={r.counts.get('neutral', 0):>2}  "
        f"annoyed={r.counts.get('annoyed', 0):>2}  "
        f"hostile={r.counts.get('hostile', 0):>2}  "
        f"menacing={r.counts.get('menacing', 0):>2}  "
        f"score={escalation_score(r.counts):>3}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="https://10.0.0.50:8052",
                        help="WebUI base URL (default: %(default)s)")
    parser.add_argument("--password", default="glados",
                        help="WebUI password (default: %(default)s)")
    parser.add_argument("--count", type=int, default=6,
                        help="Number of requests to fire (default: 6)")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between requests (default: 30 — matches debounce)")
    parser.add_argument("--chat-timeout", type=float, default=90.0,
                        help="Per-request chat timeout in seconds (default: 90)")
    parser.add_argument("--insecure", action="store_true", default=True,
                        help="Skip TLS verification (default: true for LAN self-signed certs)")
    parser.add_argument("--messages", nargs="+", default=None,
                        help="Custom message list (defaults to 6 weather paraphrases)")
    args = parser.parse_args()

    messages = args.messages or DEFAULT_MESSAGES
    if args.count > len(messages):
        # Cycle through the variants if operator asks for more than we have.
        messages = (messages * ((args.count // len(messages)) + 1))[:args.count]
    else:
        messages = messages[:args.count]

    print(f"Target: {args.host}")
    print(f"Plan:   {args.count} messages, {args.interval}s apart (~{args.count * args.interval:.0f}s total)")
    print()

    opener = build_opener(verify_tls=not args.insecure)
    print(">>> Logging in …")
    login(opener, args.host, args.password)
    print("    OK")
    print()

    results: list[ProbeResult] = []
    for i, msg in enumerate(messages, 1):
        print(f">>> [{i}/{len(messages)}] {msg}")
        status, text, elapsed = chat(opener, args.host, msg, args.chat_timeout)
        snippet = text[:200].replace("\n", " ")
        print(f"    ({status}, {elapsed}ms) {snippet}{'…' if len(text) > 200 else ''}")
        r = ProbeResult(index=i, request=msg, response=text, status_code=status, elapsed_ms=elapsed)
        r.counts = grade(text) if status == 200 else {}
        results.append(r)
        if i < len(messages):
            time.sleep(args.interval)
        print()

    print("=" * 72)
    print("ESCALATION REPORT")
    print("=" * 72)
    for r in results:
        print(format_row(r))
    print()

    # Pass / fail — compare the escalation score of the first third vs the
    # last third. Monotonic escalation isn't required (LLM is noisy), but
    # late responses should have markedly higher hostility markers than
    # early ones.
    if len(results) < 3:
        print("Note: fewer than 3 results; skipping pass/fail verdict.")
        return 0

    third = max(1, len(results) // 3)
    early = sum(escalation_score(r.counts) for r in results[:third])
    late = sum(escalation_score(r.counts) for r in results[-third:])
    early_avg = early / third
    late_avg = late / third
    delta = late_avg - early_avg

    print(f"First {third} msgs — escalation score avg: {early_avg:.1f}")
    print(f"Last  {third} msgs — escalation score avg: {late_avg:.1f}")
    print(f"Delta: {delta:+.1f}")
    print()

    if delta >= 3.0:
        print("PASS: late responses show clearly more hostility markers than early ones.")
        return 0
    if delta >= 1.0:
        print("SOFT PASS: escalation present but mild. Consider longer interval,")
        print("more variants, or audit the LLM prompt for emotional responsiveness.")
        return 0
    print("FAIL: no meaningful tone escalation across the run.")
    print("Possible causes:")
    print("  - Semantic similarity threshold too strict (variants not clustering)")
    print("  - Interval too short (events not processed between requests)")
    print("  - LLM prompt ignoring the severity tag in the emotion event")
    print("  - Emotion cooldown already engaged from prior session")
    return 1


if __name__ == "__main__":
    sys.exit(main())
