"""memory_writer.classify_and_extract routes through llm_triage slot.

The two LLM calls inside classify_and_extract — the yes/no classifier
and the single-sentence extractor — are pure classification work and
belong on the small fast llm_triage model, not the chat-quality
llm_autonomy model. Patches ``requests.post`` and asserts both POSTs
land on the triage URL.
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
    actually distinguishes the two slots. Passes ``configs_dir`` to
    ``cfg.load`` explicitly — the singleton's ``_configs_dir`` is set
    once at construction; ``reload()`` alone won't re-read env vars."""
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


def _enable_passive(monkeypatch):
    """memory_writer.should_classify_message gates on memory.yaml's
    passive.enabled flag. Force it on for the test."""
    # Patch the in-module config cache directly — _get_config reads
    # configs/memory.yaml relative to cwd, which we don't want to depend on.
    from glados.core import memory_writer
    monkeypatch.setattr(
        memory_writer,
        "_config",
        {
            "proactive_memory": {
                "passive": {
                    "enabled": True,
                    "min_message_length": 5,
                    "high_value_topics": ["prefer"],
                    "importance": 0.6,
                },
            },
        },
        raising=False,
    )


class _StubMemoryStore:
    """Minimal MemoryStore stub. classify_and_extract may call
    add_semantic and query — return safe no-op values."""

    def add_semantic(self, **kwargs) -> None:
        return None

    def query(self, **kwargs) -> list:
        return []

    def update(self, *args, **kwargs) -> bool:
        return True


def test_classify_and_extract_hits_triage_url(monkeypatch, tmp_path) -> None:
    """Both the classifier and the extractor POST must hit the triage URL.

    Even though llm_autonomy is configured to a different URL,
    classify_and_extract must resolve llm_triage internally.
    """
    _write_split_services_yaml(tmp_path)
    _enable_passive(monkeypatch)

    from glados.core.memory_writer import classify_and_extract

    seen: dict = {}
    responses = iter([
        _ok("yes"),  # classifier yes → triggers extractor
        _ok("Alex prefers tea over coffee."),  # extractor
    ])

    def _capture(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        return next(responses)

    with patch("requests.post", side_effect=_capture):
        classify_and_extract(
            "I really prefer tea over coffee in the morning",
            _StubMemoryStore(),
        )

    # Classifier + extractor = 2 POSTs, both at triage
    assert len(seen.get("urls", [])) == 2, seen
    assert all("triage-host" in u for u in seen["urls"]), seen
    assert not any("wrong-autonomy" in u for u in seen["urls"]), seen


def test_classify_only_hits_triage_when_classifier_says_no(monkeypatch, tmp_path) -> None:
    """Even if the classifier returns 'no' (extractor never runs), the
    one POST that does fire must hit the triage URL."""
    _write_split_services_yaml(tmp_path)
    _enable_passive(monkeypatch)

    from glados.core.memory_writer import classify_and_extract

    seen: dict = {}

    def _capture(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        return _ok("no")  # classifier says no → no extractor call

    with patch("requests.post", side_effect=_capture):
        classify_and_extract(
            "I really prefer tea over coffee in the morning",
            _StubMemoryStore(),
        )

    assert len(seen.get("urls", [])) == 1, seen
    assert "triage-host" in seen["urls"][0], seen
    assert "wrong-autonomy" not in seen["urls"][0], seen
