"""summarize_messages + extract_facts route through llm_triage slot.

These two functions are pure summarization / fact-extraction tasks with
no persona involvement — they should hit the small fast llm_triage model,
not the chat-quality llm_autonomy model. Patches ``requests.post`` and
asserts the captured URL is the triage one.
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
    """Create a configs dir with llm_autonomy and llm_triage pointing at
    DIFFERENT URLs so the assertion 'URL is triage's, not autonomy's'
    actually distinguishes the two slots. Loads the global cfg from this
    dir explicitly — passing ``configs_dir`` to ``cfg.load`` is the only
    way to override the env-resolved path on the already-initialised
    singleton."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_autonomy:\n"
        "  url: http://wrong-autonomy:11434/v1/chat/completions\n"
        "  model: should-not-be-used\n"
        "llm_triage:\n"
        "  url: http://triage-host:11434/v1/chat/completions\n"
        "  model: llama-3.2-1b-instruct\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)


def test_summarize_messages_hits_triage_url(tmp_path) -> None:
    """The POST URL must be the llm_triage slot's URL, not llm_autonomy."""
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
    assert any("triage-host" in u for u in seen["urls"]), seen
    assert not any("wrong-autonomy" in u for u in seen["urls"]), seen


def test_extract_facts_hits_triage_url(tmp_path) -> None:
    """extract_facts must also route to llm_triage, not llm_autonomy."""
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
    assert any("triage-host" in u for u in seen["urls"]), seen
    assert not any("wrong-autonomy" in u for u in seen["urls"]), seen
