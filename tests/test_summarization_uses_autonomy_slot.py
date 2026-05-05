"""summarize_messages + extract_facts route through llm_autonomy slot.

Compaction prompts can grow large (5-8k chars after truncation) and must
not poison the chat lane. They belong on whatever model the operator has
configured for autonomy — which lane that is depends on the operator's
deployment, but it must NOT be the chat (llm_interactive) lane. Patches
``requests.post`` and asserts the captured URL is the autonomy slot's.

Regression rationale: prior to 2026-05-05 these helpers hard-coded
``llm_triage``. Triage was running on a small llama.cpp instance with
a 1024-token per-slot context — fine for classifier prompts, far too
small for compaction. Compaction silently 400'd every minute, the
conversation grew unbounded, and eventually a 25k-token bomb hit the
chat 30B and stalled it for 90 seconds. Routing summarization through
the autonomy slot lets the operator configure compaction's lane
declaratively without code changes.
"""

from __future__ import annotations

from unittest.mock import patch


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _ok(content="ok"):
    return _Resp({"choices": [{"message": {"content": content}}]})


def _write_split_services_yaml(tmp_path):
    """Create a configs dir with llm_autonomy and llm_interactive pointing
    at DIFFERENT URLs so the assertion 'URL is autonomy's, not interactive'
    actually distinguishes the two slots. Loads the global cfg from this
    dir explicitly — passing ``configs_dir`` to ``cfg.load`` is the only
    way to override the env-resolved path on the already-initialised
    singleton."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_interactive:\n"
        "  url: http://wrong-chat:11434/v1/chat/completions\n"
        "  model: should-not-be-used\n"
        "llm_autonomy:\n"
        "  url: http://autonomy-host:11436/v1/chat/completions\n"
        "  model: Qwen3-4B-Instruct-2507-Q5_K_M.gguf\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)


def test_summarize_messages_hits_autonomy_url(tmp_path) -> None:
    """The POST URL must be the llm_autonomy slot's URL, not llm_interactive."""
    _write_split_services_yaml(tmp_path)

    from glados.autonomy.summarization import summarize_messages
    seen: dict = {}

    def _capture(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        return _ok("a short summary")

    with patch("requests.post", side_effect=_capture):
        summarize_messages([
            {"role": "user", "content": "hi there how are you"},
            {"role": "assistant", "content": "doing well thanks"},
        ])

    assert seen.get("urls"), "expected requests.post to be called"
    assert any("autonomy-host" in u for u in seen["urls"]), seen
    assert not any("wrong-chat" in u for u in seen["urls"]), seen


def test_extract_facts_hits_autonomy_url(tmp_path) -> None:
    """extract_facts must also route to llm_autonomy, not llm_interactive."""
    _write_split_services_yaml(tmp_path)

    from glados.autonomy.summarization import extract_facts
    seen: dict = {}

    def _capture(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        return _ok("Alex prefers tea over coffee")

    with patch("requests.post", side_effect=_capture):
        extract_facts([
            {"role": "user", "content": "I really prefer tea over coffee"},
            {"role": "assistant", "content": "Noted."},
        ])

    assert seen.get("urls"), "expected requests.post to be called"
    assert any("autonomy-host" in u for u in seen["urls"]), seen
    assert not any("wrong-chat" in u for u in seen["urls"]), seen
