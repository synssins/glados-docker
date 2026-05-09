"""Tests for glados.sip.pin_gate."""
from __future__ import annotations

import pytest

from glados.sip.pin_gate import GateResult, PinGate, extract_digits


# ---------------------------------------------------------------------------
# extract_digits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transcript,expected", [
    ("8316", "8316"),
    ("eight three one six", "8316"),
    ("uh, eight three one six", "8316"),
    ("the pin is eight three one six please", "8316"),
    ("EIGHT three ONE six", "8316"),  # case-insensitive
    ("oh three one six", "0316"),     # "oh" → 0
    ("zero three one six", "0316"),
    ("0316", "0316"),
    ("eight, three, one, six!", "8316"),  # punctuation-tolerant
    ("ate three for nine", "8349"),  # homophones recognised
    ("two two two two", "2222"),
    ("five five five one two three four", "5551234"),
    ("", ""),
    ("hello world", ""),
    ("nothing useful here", ""),
    ("eight 3 one 6", "8316"),  # mixed words and digits
])
def test_extract_digits(transcript: str, expected: str) -> None:
    assert extract_digits(transcript) == expected


# ---------------------------------------------------------------------------
# DTMF entry
# ---------------------------------------------------------------------------

def test_dtmf_correct_pin_returns_pass() -> None:
    gate = PinGate(expected_pin="8316")
    assert gate.feed_dtmf("8") == GateResult.PENDING
    assert gate.feed_dtmf("3") == GateResult.PENDING
    assert gate.feed_dtmf("1") == GateResult.PENDING
    assert gate.feed_dtmf("6") == GateResult.PASS
    assert gate.resolved is True


def test_dtmf_wrong_pin_returns_invalid_first_two_attempts() -> None:
    gate = PinGate(expected_pin="8316", max_failures=3)
    # Attempt 1
    for d in "1234":
        result = gate.feed_dtmf(d)
    assert result == GateResult.INVALID
    assert gate.failures == 1
    assert gate.attempts_remaining == 2

    # Attempt 2
    for d in "5678":
        result = gate.feed_dtmf(d)
    assert result == GateResult.INVALID
    assert gate.failures == 2


def test_dtmf_third_failure_returns_fail() -> None:
    gate = PinGate(expected_pin="8316", max_failures=3)
    for _ in range(3):
        for d in "0000":
            result = gate.feed_dtmf(d)
    assert result == GateResult.FAIL
    assert gate.resolved is True


def test_dtmf_after_resolved_stays_failed() -> None:
    gate = PinGate(expected_pin="8316", max_failures=2)
    for _ in range(2):
        for d in "0000":
            gate.feed_dtmf(d)
    assert gate.resolved is True
    # Further feeds should not flip the verdict
    assert gate.feed_dtmf("8") == GateResult.FAIL
    assert gate.feed_dtmf("3") == GateResult.FAIL


def test_dtmf_ignores_bogus_chars() -> None:
    gate = PinGate(expected_pin="8316")
    # A, B, etc. shouldn't increment the buffer
    assert gate.feed_dtmf("A") == GateResult.PENDING
    assert gate.feed_dtmf("Z") == GateResult.PENDING
    assert gate.feed_dtmf("") == GateResult.PENDING
    # Real digits still work
    for d in "8316":
        result = gate.feed_dtmf(d)
    assert result == GateResult.PASS


def test_dtmf_buffer_resets_after_full_attempt() -> None:
    """After 4 wrong digits, next 4 should evaluate as a fresh attempt."""
    gate = PinGate(expected_pin="8316", max_failures=5)
    for d in "1234":
        gate.feed_dtmf(d)
    assert gate.failures == 1
    # Now feed the right PIN — should pass
    for d in "8316":
        result = gate.feed_dtmf(d)
    assert result == GateResult.PASS


# ---------------------------------------------------------------------------
# STT entry
# ---------------------------------------------------------------------------

def test_stt_correct_full_pin_returns_pass() -> None:
    gate = PinGate(expected_pin="8316")
    assert gate.feed_stt("eight three one six") == GateResult.PASS


def test_stt_wrong_full_pin_returns_invalid() -> None:
    gate = PinGate(expected_pin="8316", max_failures=3)
    assert gate.feed_stt("one two three four") == GateResult.INVALID
    assert gate.failures == 1


def test_stt_filler_ignored() -> None:
    gate = PinGate(expected_pin="8316")
    assert gate.feed_stt("uh, the pin is eight three one six please") == GateResult.PASS


def test_stt_partial_transcripts_accumulate() -> None:
    """Word-at-a-time STT should compose into the full PIN."""
    gate = PinGate(expected_pin="8316")
    assert gate.feed_stt("eight") == GateResult.PENDING
    assert gate.feed_stt("three") == GateResult.PENDING
    assert gate.feed_stt("one") == GateResult.PENDING
    assert gate.feed_stt("six") == GateResult.PASS


def test_stt_more_than_pin_length_truncates_to_first_n() -> None:
    """If a transcript yields more than 4 digits, only the first 4 evaluate."""
    gate = PinGate(expected_pin="8316")
    # "eight three one six and then some seven nine"
    # First 4 = 8316 → PASS
    assert gate.feed_stt("eight three one six and then some seven nine") == GateResult.PASS


def test_stt_empty_transcript_pending() -> None:
    gate = PinGate(expected_pin="8316")
    assert gate.feed_stt("") == GateResult.PENDING
    assert gate.feed_stt("hello world") == GateResult.PENDING


def test_stt_reset_buffer_clears_partial() -> None:
    gate = PinGate(expected_pin="8316")
    gate.feed_stt("eight three")  # 2 digits buffered
    gate.reset_stt_buffer()
    # Fresh attempt: only "8316" should be in the buffer now
    gate.feed_stt("eight three one six")
    # Partial buffer was cleared, so "8316" is the full attempt
    assert gate.resolved


# ---------------------------------------------------------------------------
# Mixed DTMF + STT
# ---------------------------------------------------------------------------

def test_mixed_dtmf_first_then_stt_doesnt_pollute_each_other() -> None:
    """DTMF buffer and STT buffer are independent."""
    gate = PinGate(expected_pin="8316", max_failures=3)
    # Partial DTMF (2 digits)
    gate.feed_dtmf("8")
    gate.feed_dtmf("3")
    # Now caller speaks the full pin via STT
    result = gate.feed_stt("eight three one six")
    assert result == GateResult.PASS
    # DTMF partial did NOT contribute to the STT attempt


def test_mixed_dtmf_completes_after_stt_fails() -> None:
    gate = PinGate(expected_pin="8316", max_failures=3)
    # Wrong STT attempt
    assert gate.feed_stt("one two three four") == GateResult.INVALID
    assert gate.failures == 1
    # Right via DTMF
    for d in "8316":
        result = gate.feed_dtmf(d)
    assert result == GateResult.PASS


def test_attempts_remaining_decrements_on_failure() -> None:
    gate = PinGate(expected_pin="8316", max_failures=3)
    assert gate.attempts_remaining == 3
    gate.feed_stt("one two three four")
    assert gate.attempts_remaining == 2
    gate.feed_stt("five six seven eight")
    assert gate.attempts_remaining == 1
    gate.feed_stt("nine zero one two")
    assert gate.attempts_remaining == 0
    assert gate.resolved


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------

def test_custom_pin_length_works() -> None:
    gate = PinGate(expected_pin="123456", pin_length=6)
    for d in "123456":
        result = gate.feed_dtmf(d)
    assert result == GateResult.PASS


def test_custom_max_failures() -> None:
    gate = PinGate(expected_pin="8316", max_failures=1)
    for d in "0000":
        result = gate.feed_dtmf(d)
    assert result == GateResult.FAIL  # one failure exhausts the budget
