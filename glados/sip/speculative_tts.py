"""Speculative TTS pre-rendering for SIP latency hiding.

While a SIP call is in any "wait" state (PIN entry, menu prompt
playing, caller speaking), background workers pre-render the likely
next TTS responses so they're ready before they're needed.

Use case during PIN entry:

- ``pin_success``    — *"Acknowledged. So I'm in your phone now..."*
- ``pin_fail_1``     — *"Wrong. Try again. Two attempts remaining."*
- ``pin_fail_2``     — *"Wrong. One attempt remaining."*
- ``pin_fail_final`` — *"Authorization denied. Disconnecting."*

When the gate resolves, ``call_session`` calls ``consume`` for the
matching label and gets the cached audio without waiting for TTS.
Non-matching jobs are cancelled.

The cache is per-call. Construct one ``SpeculativeTtsCache``, register
branches as state transitions occur, consume to play, throw it away
when the call ends.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional

from loguru import logger


# Type alias for the injected TTS service: text → audio bytes.
# call_session wires this to the existing Piper pipeline.
TtsCallable = Callable[[str], Awaitable[bytes]]


class SpeculativeTtsCache:
    """Per-call speculative TTS cache.

    Construct with the TTS service callable + a concurrency cap. Call
    ``register_branch`` to fire off pre-renders for a state transition,
    ``consume`` to get the audio for the actual branch that resolved,
    and ``cancel_other`` (or ``cancel_all``) to free remaining workers.
    """

    def __init__(
        self,
        tts_callable: TtsCallable,
        *,
        max_concurrent: int = 4,
    ) -> None:
        self._tts = tts_callable
        self._max_concurrent = max_concurrent
        # Bound the background-TTS workforce per call. Beyond the cap,
        # additional renders defer until earlier ones finish.
        self._sem = asyncio.Semaphore(max_concurrent)
        # branch_name → { label: Task[bytes] }
        self._branches: dict[str, dict[str, asyncio.Task[bytes]]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_branch(self, branch: str, jobs: dict[str, str]) -> None:
        """Start background TTS tasks for each (label, text) pair.

        ``branch`` is a state-name (e.g. ``"pin_entry"``); ``jobs`` is
        ``{label: text_to_synthesize}``. If a branch with the same
        name already exists, its prior tasks are cancelled and replaced.
        """
        # Cancel any prior incarnation of this branch — replace, don't merge.
        if branch in self._branches:
            self._cancel_branch(branch)
        self._branches[branch] = {
            label: asyncio.create_task(self._render(text), name=f"spec-tts-{branch}-{label}")
            for label, text in jobs.items()
        }

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------

    async def consume(
        self,
        branch: str,
        label: str,
        fallback_text: Optional[str] = None,
    ) -> bytes:
        """Return the audio for the matching ``(branch, label)``.

        Lookup outcomes:
        - **Ready** — task is done, return cached bytes immediately.
        - **In flight** — await the task (faster than starting fresh).
        - **Not registered / cancelled** — synthesize ``fallback_text``
          synchronously. If ``fallback_text`` is None, raises
          ``KeyError``.

        After consumption, the task is removed from the branch's
        registry. Sibling tasks are NOT auto-cancelled — call
        ``cancel_other`` separately if that's the desired behaviour.
        """
        task = self._branches.get(branch, {}).get(label)
        if task is not None:
            try:
                audio = await task
                # Remove only on success — leave failed tasks in place
                # so they can be inspected if needed.
                self._branches.get(branch, {}).pop(label, None)
                return audio
            except asyncio.CancelledError:
                # Task was cancelled before consumption — fall through
                # to fallback rendering below.
                self._branches.get(branch, {}).pop(label, None)
            except Exception as e:
                logger.bind(group="sip").warning(
                    f"speculative_tts: cached render of {branch}/{label} failed ({e}); falling back"
                )
                self._branches.get(branch, {}).pop(label, None)

        if fallback_text is None:
            raise KeyError(f"no speculative job and no fallback for {branch}/{label}")
        # Synchronous fallback — bypass the semaphore (urgent path)
        return await self._tts(fallback_text)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_other(self, branch: str, kept_label: str) -> None:
        """Cancel every task in ``branch`` except the one named ``kept_label``."""
        if branch not in self._branches:
            return
        for label, task in list(self._branches[branch].items()):
            if label == kept_label:
                continue
            if not task.done():
                task.cancel()
            self._branches[branch].pop(label, None)

    def cancel_branch(self, branch: str) -> None:
        """Cancel all tasks in a branch (used when state moves on)."""
        self._cancel_branch(branch)

    def cancel_all(self) -> None:
        """Cancel every outstanding task across all branches."""
        for branch in list(self._branches):
            self._cancel_branch(branch)

    def _cancel_branch(self, branch: str) -> None:
        for _, task in list(self._branches.get(branch, {}).items()):
            if not task.done():
                task.cancel()
        self._branches.pop(branch, None)

    # ------------------------------------------------------------------
    # Internal — render with concurrency cap
    # ------------------------------------------------------------------

    async def _render(self, text: str) -> bytes:
        async with self._sem:
            return await self._tts(text)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, dict[str, str]]:
        """Snapshot of branch / label states. For debug logging."""
        out: dict[str, dict[str, str]] = {}
        for branch, jobs in self._branches.items():
            out[branch] = {}
            for label, task in jobs.items():
                if task.cancelled():
                    out[branch][label] = "cancelled"
                elif task.done():
                    out[branch][label] = "ready" if task.exception() is None else "failed"
                else:
                    out[branch][label] = "running"
        return out
