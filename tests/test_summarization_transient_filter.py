"""Transient-state filter on conversation summaries and extracted facts.

Compaction LLM (small triage model) can produce summaries / facts describing
tool errors or current library/database state. If those land in ChromaDB,
they get injected into future chat context as canon and the model trusts
them over re-querying. This filter is the storage-boundary safety net,
applied in both `extract_facts` and `summarize_messages`.

Real-world failure case the filter must catch (operator-flagged 2026-05-03):
when `radarr_get_movies` failed with "database is locked", the compaction
agent summarized the assistant reply as "The system does not currently
have any movies in your library", stored that in ChromaDB, and from then
on every movie-related chat saw the false fact injected — model picked
the wrong tool (radarr_search vs radarr_get_movies) and confirmed the
phantom emptiness.
"""

from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Unit tests for _is_transient_state
# ---------------------------------------------------------------------------

def test_is_transient_state_catches_database_locked() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("The radarr database is locked")
    assert _is_transient_state("database is locked, please retry later")


def test_is_transient_state_catches_no_movies_in_library() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("The system does not currently have any movies in your library.")
    assert _is_transient_state("no movies are currently in your library")


def test_is_transient_state_catches_cannot_access_library() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("I cannot access your movie library or preferences.")
    assert _is_transient_state("cannot access the database right now")


def test_is_transient_state_catches_currently_unavailable() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("The Radarr service is currently unavailable.")


def test_is_transient_state_catches_x_is_not_in_library() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("Ghostbusters is not currently in your library.")
    assert _is_transient_state("Inception is not in your library")


def test_is_transient_state_catches_user_question_about_library() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("Is Ghostbusters in your movie library?")


def test_is_transient_state_catches_radarr_temporarily_offline() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state(
        "indicating a temporary issue with Radarr's database. No movie was retrieved."
    )
    assert _is_transient_state("The movie database is temporarily unavailable")


def test_is_transient_state_catches_failed_to_retrieve() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert _is_transient_state("Failed to retrieve list of movies from Radarr.")
    assert _is_transient_state("failed to connect to Sonarr API")


def test_is_transient_state_does_not_catch_personal_fact() -> None:
    """The filter MUST NOT catch personal facts about the operator/household."""
    from glados.autonomy.summarization import _is_transient_state
    assert not _is_transient_state("Alex prefers tea over coffee.")
    assert not _is_transient_state("The operator's dog is named Rex.")
    assert not _is_transient_state("Maya is allergic to peanuts.")
    assert not _is_transient_state("The household is in Jordan, Minnesota.")
    assert not _is_transient_state("The operator works as a software engineer.")


def test_is_transient_state_does_not_catch_door_locked() -> None:
    """A door being locked is a household fact — filter MUST NOT touch it.
    Only data-source 'is locked' phrases (database/library/server/file/service)
    are transient."""
    from glados.autonomy.summarization import _is_transient_state
    assert not _is_transient_state("The front door is locked at night.")
    assert not _is_transient_state("Maya keeps her bedroom locked.")


def test_is_transient_state_handles_empty_and_none() -> None:
    from glados.autonomy.summarization import _is_transient_state
    assert not _is_transient_state("")
    assert not _is_transient_state(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: extract_facts drops transient facts
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _ok(content: str):
    return _Resp({"choices": [{"message": {"content": content}}]})


def test_extract_facts_drops_transient_facts(tmp_path) -> None:
    """If the LLM returns a mix of personal facts and transient-state facts,
    only the personal facts survive into ChromaDB."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_triage:\n  url: http://triage-host:11434/v1/chat/completions\n  model: tiny\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)

    llm_response = (
        "Alex prefers tea over coffee\n"
        "The system does not currently have any movies in your library\n"
        "The household is in Jordan, Minnesota\n"
        "Radarr's database is locked, no movies available\n"
        "Maya is allergic to peanuts\n"
    )

    from glados.autonomy.summarization import extract_facts

    with patch("requests.post", return_value=_ok(llm_response)):
        facts = extract_facts([
            {"role": "user", "content": "Tell me about my household and library."},
            {"role": "assistant", "content": "Acknowledged."},
        ])

    # Only the 3 personal facts should survive
    assert "Alex prefers tea over coffee" in facts
    assert "The household is in Jordan, Minnesota" in facts
    assert "Maya is allergic to peanuts" in facts

    # The 2 transient-state facts MUST be dropped
    assert not any("does not currently have any movies" in f for f in facts), facts
    assert not any("database is locked" in f for f in facts), facts


def test_extract_facts_returns_empty_when_only_transient(tmp_path) -> None:
    """If every line returned by the LLM is transient, result is empty list."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_triage:\n  url: http://triage-host:11434/v1/chat/completions\n  model: tiny\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)

    llm_response = (
        "Radarr database is locked\n"
        "Cannot access your movie library\n"
        "No movies are currently in your library\n"
    )

    from glados.autonomy.summarization import extract_facts

    with patch("requests.post", return_value=_ok(llm_response)):
        facts = extract_facts([
            {"role": "user", "content": "What's in my library?"},
            {"role": "assistant", "content": "Database is locked."},
        ])

    assert facts == []


# ---------------------------------------------------------------------------
# Integration: summarize_messages drops fully-transient summaries
# ---------------------------------------------------------------------------

def test_summarize_messages_drops_transient_summary(tmp_path) -> None:
    """A summary that's entirely about a tool failure / library state must
    return None (compaction will retry next tick rather than canonize garbage)."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_triage:\n  url: http://triage-host:11434/v1/chat/completions\n  model: tiny\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)

    transient_summary = (
        "Conversation summary: The system does not currently have any movies "
        "in your library. Would you like to add a movie using Radarr?"
    )

    from glados.autonomy.summarization import summarize_messages

    with patch("requests.post", return_value=_ok(transient_summary)):
        result = summarize_messages([
            {"role": "user", "content": "Suggest a movie."},
            {"role": "assistant", "content": "Database error."},
        ])

    assert result is None


def test_summarize_messages_keeps_clean_summary(tmp_path) -> None:
    """A summary about personal/household topics must pass through unchanged."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_triage:\n  url: http://triage-host:11434/v1/chat/completions\n  model: tiny\n",
        encoding="utf-8",
    )
    from glados.core.config_store import cfg
    cfg.load(configs_dir=cfgs)

    clean_summary = (
        "Conversation summary: Alex talked about preparing dinner for the "
        "household. Maya is allergic to peanuts; the meal must avoid them."
    )

    from glados.autonomy.summarization import summarize_messages

    with patch("requests.post", return_value=_ok(clean_summary)):
        result = summarize_messages([
            {"role": "user", "content": "Help plan dinner."},
            {"role": "assistant", "content": "Sure."},
        ])

    assert result == clean_summary
