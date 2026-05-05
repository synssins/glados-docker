"""Tier 3 — End-to-end voice pipeline.

Two tests:

- `stt_synth_roundtrip` is the default. TTS synthesises a known string,
  STT decodes it, the suite asserts the transcript is close enough to
  the original. No memory writes, no fixture dependencies.

- `e2e_voice_pipeline` (mutating, opt-in only) requires recorded audio
  fixtures and includes a chat-completions hop, which writes to GLaDOS's
  conversation store. Run with `--include-mutating` and only when
  fixtures are present.

Per TEST_PLAN.md §"Tier 3".
"""

from __future__ import annotations

import string
import time

import pytest
import requests

pytestmark = pytest.mark.tier3


def test_tier3_stt_synth_roundtrip(
    http_session, smoke_config, smoke_record
) -> None:
    """TTS synthesises a known string; STT decodes; transcript is close enough."""

    spoken = "the operator is testing the smoke suite"
    smoke_record.checked = "/v1/audio/speech -> /v1/audio/transcriptions round-trip"
    smoke_record.expected = (
        f'transcript Levenshtein distance <= 5 from {spoken!r}'
    )

    # 1) synth
    synth_url = smoke_config.url("api", "/v1/audio/speech")
    t0 = time.time()
    r = http_session.post(
        synth_url,
        json={"input": spoken, "voice": "glados", "response_format": "wav"},
        timeout=smoke_config.timeouts["tts"],
    )
    assert r.status_code == 200, f"synth failed: {r.status_code} {r.text[:200]}"
    wav_bytes = r.content
    assert len(wav_bytes) > 1024, f"synth audio too short: {len(wav_bytes)} bytes"
    synth_ms = int((time.time() - t0) * 1000)

    # 2) transcribe
    stt_url = smoke_config.url("api", "/v1/audio/transcriptions")
    t1 = time.time()
    r2 = http_session.post(
        stt_url,
        files={"file": ("smoke.wav", wav_bytes, "audio/wav")},
        timeout=max(smoke_config.timeouts["tts"], 8.0),
    )
    stt_ms = int((time.time() - t1) * 1000)
    assert r2.status_code == 200, f"stt failed: {r2.status_code} {r2.text[:200]}"

    body = r2.json()
    transcript = (body.get("text") or "").strip()
    smoke_record.extras["spoken"] = spoken
    smoke_record.extras["transcript"] = transcript
    smoke_record.extras["synth_ms"] = synth_ms
    smoke_record.extras["stt_ms"] = stt_ms
    smoke_record.actual = (
        f"transcript={transcript!r} synth_ms={synth_ms} stt_ms={stt_ms}"
    )

    assert transcript, "STT returned empty transcript"

    distance = _levenshtein(_normalise(spoken), _normalise(transcript))
    smoke_record.extras["levenshtein"] = distance

    if distance > 5:
        smoke_record.summary = (
            f"Transcript drift: distance={distance} (limit 5)"
        )
        pytest.fail(
            f"transcript {transcript!r} differs from {spoken!r} by {distance}"
        )

    smoke_record.summary = (
        f"Round-trip OK (synth {synth_ms} ms, stt {stt_ms} ms, distance {distance})"
    )


@pytest.mark.mutates
@pytest.mark.requires_audio_fixtures
@pytest.mark.slow
def test_tier3_e2e_voice_pipeline(
    http_session, smoke_config, smoke_record, audio_fixture
) -> None:
    """Full pipeline: WAV -> STT -> chat completions -> TTS -> audio out.

    MUTATES: writes one row to GLaDOS's conversation store. Opt-in only.
    Skipped without `tests/smoke/fixtures/query_what_time.wav`.
    """

    wav_bytes = audio_fixture("query_what_time.wav")

    # 1) STT
    smoke_record.checked = (
        "audio fixture -> /v1/audio/transcriptions -> /v1/chat/completions "
        "-> /v1/audio/speech"
    )
    smoke_record.expected = "non-empty at every stage; total under threshold"

    t0 = time.time()
    stt_url = smoke_config.url("api", "/v1/audio/transcriptions")
    r1 = http_session.post(
        stt_url,
        files={"file": ("query.wav", wav_bytes, "audio/wav")},
        timeout=15,
    )
    assert r1.status_code == 200, f"stt failed: {r1.status_code}"
    transcript = (r1.json().get("text") or "").strip()
    assert transcript, "STT returned empty transcript"
    stt_ms = int((time.time() - t0) * 1000)

    # 2) chat completions — temperature MUST NOT be 0.0 per OpenArc upstream gap.
    chat_url = smoke_config.url("api", "/v1/chat/completions")
    t1 = time.time()
    r2 = http_session.post(
        chat_url,
        json={
            "model": "glados",
            "messages": [{"role": "user", "content": transcript}],
            "max_tokens": 64,
            "temperature": 0.2,
            "stream": False,
        },
        timeout=30,
    )
    chat_ms = int((time.time() - t1) * 1000)
    assert r2.status_code == 200, f"chat failed: {r2.status_code} {r2.text[:200]}"
    chat_body = r2.json()
    choices = chat_body.get("choices") or []
    assert choices, f"no choices in chat response: {chat_body}"
    response_text = (
        (choices[0].get("message") or {}).get("content")
        or choices[0].get("text")
        or ""
    ).strip()
    assert response_text, "LLM response empty"
    assert "traceback" not in response_text.lower(), \
        f"LLM response looks like an error: {response_text[:200]}"

    # 3) TTS the response
    tts_url = smoke_config.url("api", "/v1/audio/speech")
    t2 = time.time()
    r3 = http_session.post(
        tts_url,
        json={"input": response_text, "voice": "glados", "response_format": "wav"},
        timeout=15,
    )
    tts_ms = int((time.time() - t2) * 1000)
    assert r3.status_code == 200, f"tts failed: {r3.status_code}"
    out_bytes = r3.content
    assert len(out_bytes) > 1024, f"tts output too short: {len(out_bytes)} bytes"

    total_ms = int((time.time() - t0) * 1000)
    smoke_record.extras.update(
        {
            "transcript": transcript,
            "response_preview": response_text[:200],
            "stt_ms": stt_ms,
            "chat_ms": chat_ms,
            "tts_ms": tts_ms,
            "tts_bytes": len(out_bytes),
        }
    )
    smoke_record.actual = (
        f"transcript -> response ({len(response_text)} ch) -> "
        f"{len(out_bytes)} audio bytes in {total_ms} ms"
    )
    threshold_s = 30  # configurable later if needed
    if total_ms > threshold_s * 1000:
        smoke_record.summary = (
            f"Pipeline succeeded but slow: {total_ms} ms > {threshold_s*1000}"
        )
        pytest.fail(f"pipeline took {total_ms} ms, threshold {threshold_s*1000}")
    smoke_record.summary = (
        f"E2E pipeline OK ({total_ms} ms total)"
    )


# ─── helpers ─────────────────────────────────────────────────────────────


def _normalise(s: str) -> str:
    s = s.lower()
    return "".join(ch for ch in s if ch not in string.punctuation).strip()


def _levenshtein(a: str, b: str) -> int:
    """Minimal pure-Python Levenshtein. Avoids a hard dependency on
    rapidfuzz / Levenshtein for the smoke suite — those packages are
    available in the project, but smoke should still work in a slim env."""

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(
                min(
                    prev[j] + 1,
                    curr[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = curr
    return prev[-1]
