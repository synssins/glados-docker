"""First-run wizard engine. Pluggable step registry; Phase 1 ships one
step (admin password). See docs/AUTH_DESIGN.md §5.1.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable, Protocol


class StepResult(str, Enum):
    DONE = "done"       # step completed successfully; wizard may advance
    NEXT = "next"       # step accepted but more work remains (rare)
    ERROR = "error"     # step re-renders with an error message


class WizardStep(Protocol):
    """Pluggable step contract. Concrete steps live under
    glados/webui/setup/steps/."""
    name: str
    order: int

    @property
    def title(self) -> str: ...

    def is_required(self, cfg) -> bool: ...

    def render(self, handler, error: str = "", sticky_form: dict | None = None) -> str: ...

    def process(self, handler, form: dict) -> StepResult: ...


def resolve_next_step(steps: Iterable[WizardStep], cfg) -> WizardStep | None:
    """Return the lowest-order step whose `is_required(cfg)` is True.

    Returns None when no required steps remain — the wizard is complete
    and the engine should flip auth.bootstrap_allowed=False and redirect
    the operator into the regular SPA shell.
    """
    ordered = sorted(steps, key=lambda s: s.order)
    for step in ordered:
        if step.is_required(cfg):
            return step
    return None
