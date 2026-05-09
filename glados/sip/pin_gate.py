"""4-digit PIN gate for inbound SIP calls.

Accepts PIN entry from two parallel input sources:

1. **DTMF**: discrete digit events from ``ctrl_client`` subscriptions
   (RFC 2833 telephone-event packets baresip surfaces as JSON events
   on the ``ctrl_tcp`` channel). Buffers 4 digits, evaluates on the
   4th.
2. **STT**: full transcript strings from the speech-to-text pipeline.
   Parses digit sequences embedded in natural-language utterances:
   ``"eight three one six"``, ``"8316"``, ``"the pin is eight three
   one six please"``. Tolerates filler ("uh", "um", "the pin is").

First path to produce a valid 4-digit string against the expected PIN
returns ``PASS``. Failures increment a shared counter; at
``max_failures`` (default 3) the gate returns ``FAIL`` and the call
session is expected to play the rejection line + hang up.

The gate is a pure state machine — the caller (``call_session.py``)
subscribes to DTMF / STT events and calls ``feed_dtmf`` / ``feed_stt``
as they arrive. The gate doesn't own the event loop or the audio
bridge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class GateResult(str, Enum):
    """Outcome of a PIN-gate feed call."""
    PENDING = "pending"     # still waiting for more input
    PASS = "pass"           # PIN matched
    FAIL = "fail"           # max_failures reached
    INVALID = "invalid"     # this attempt was wrong, but more attempts allowed


# Map of English digit words to their numeric character.
# "oh" is a common substitute for zero in spoken phone-numbers.
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3",
    "four": "4", "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9",
}


def extract_digits(transcript: str) -> str:
    """Extract a digit sequence from a natural-language transcript.

    Recognises both numeric strings (``"8316"``) and English digit
    words (``"eight three one six"``). Tolerates surrounding filler.
    Concatenates all digits found in the transcript in order.
    """
    if not transcript:
        return ""
    text = transcript.lower()
    out: list[str] = []
    # Tokenise by non-alphanumeric to keep word and number boundaries.
    tokens = re.findall(r"[a-z0-9]+", text)
    for tok in tokens:
        if tok.isdigit():
            out.append(tok)
        elif tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok])
    return "".join(out)


@dataclass
class PinGate:
    """Stateful 4-digit PIN gate.

    Construction takes the expected PIN. Caller calls ``feed_dtmf``
    / ``feed_stt`` as inputs arrive; each returns a ``GateResult``:

    - ``PENDING`` — keep feeding
    - ``INVALID`` — this attempt was wrong but more attempts allowed
      (caller should play "wrong, try again" and continue)
    - ``PASS`` — PIN matched, advance to MENU/CONVERSATION
    - ``FAIL`` — max_failures reached, hang up
    """

    expected_pin: str
    max_failures: int = 3
    pin_length: int = 4

    # Internal state
    failures: int = 0
    _dtmf_buffer: str = ""
    _resolved: bool = False
    # STT inputs that didn't yet produce a full digit sequence are
    # buffered so partial transcripts (one-word-at-a-time STT) compose
    # into the full PIN. Reset on each evaluation.
    _stt_partial: str = field(default_factory=str)

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_failures - self.failures)

    @property
    def resolved(self) -> bool:
        """True once a terminal result (PASS or FAIL) has been issued."""
        return self._resolved

    def feed_dtmf(self, digit: str) -> GateResult:
        """Append a single DTMF digit to the buffer; evaluate when full."""
        if self._resolved:
            return GateResult.FAIL  # gate already closed
        if not digit or digit not in "0123456789*#":
            return GateResult.PENDING  # ignore bogus events
        self._dtmf_buffer += digit
        if len(self._dtmf_buffer) >= self.pin_length:
            attempt = self._dtmf_buffer[: self.pin_length]
            self._dtmf_buffer = ""  # reset for next attempt
            return self._evaluate(attempt)
        return GateResult.PENDING

    def feed_stt(self, transcript: str) -> GateResult:
        """Parse a transcript chunk, accumulate digits, evaluate when full.

        STT pipelines may emit one-word-at-a-time partials; we
        accumulate digits across calls until ``pin_length`` is reached,
        then evaluate.
        """
        if self._resolved:
            return GateResult.FAIL
        digits = extract_digits(transcript)
        if not digits:
            return GateResult.PENDING
        self._stt_partial += digits
        if len(self._stt_partial) >= self.pin_length:
            attempt = self._stt_partial[: self.pin_length]
            self._stt_partial = ""
            return self._evaluate(attempt)
        return GateResult.PENDING

    def reset_stt_buffer(self) -> None:
        """Clear the STT digit buffer.

        Useful when the caller knows the prior transcript was noise
        (long silence, garbled utterance) and wants a fresh attempt.
        """
        self._stt_partial = ""

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate(self, attempt: str) -> GateResult:
        if attempt == self.expected_pin:
            self._resolved = True
            return GateResult.PASS
        # Wrong PIN — increment failure counter
        self.failures += 1
        if self.failures >= self.max_failures:
            self._resolved = True
            return GateResult.FAIL
        return GateResult.INVALID
