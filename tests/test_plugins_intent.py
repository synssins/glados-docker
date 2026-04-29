"""Keyword-based plugin intent matcher (Phase 2c gate #1).

The matcher is the zero-latency pre-filter on the chitchat path.
Coverage focuses on stemming correctness and the precision/recall
contract: word-boundary matches only (so "remove" never matches
"movie"), case-insensitive, multi-plugin union, plugins with empty
keyword lists never match.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _FakeManifest:
    intent_keywords: list[str]


@dataclass
class _FakePlugin:
    name: str
    manifest_v2: Any


def _plugin(name: str, keywords: list[str]) -> _FakePlugin:
    return _FakePlugin(name=name, manifest_v2=_FakeManifest(intent_keywords=keywords))


def test_exact_keyword_match():
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["movie", "tv", "torrent"])]
    matched = match_plugins("add a movie to the queue", plugins)
    assert [p.name for p in matched] == ["arr-stack"]


def test_plural_to_singular():
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["movie"])]
    matched = match_plugins("what movies do I have", plugins)
    assert [p.name for p in matched] == ["arr-stack"]


def test_progressive_form_to_bare():
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["download"])]
    # "downloading" -> "download" via -ing strip.
    matched = match_plugins("am I downloading anything", plugins)
    assert [p.name for p in matched] == ["arr-stack"]


def test_remove_does_not_match_movie():
    """The classic substring trap: 'remove' contains 'mov' but is NOT
    'movie' under a word-boundary tokenizer. Regression guard."""
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["movie"])]
    matched = match_plugins("please remove the lights", plugins)
    assert matched == []


def test_multi_plugin_union():
    from glados.plugins.intent import match_plugins
    plugins = [
        _plugin("arr-stack", ["movie"]),
        _plugin("media-player", ["music", "song"]),
        _plugin("calendar", ["meeting"]),
    ]
    matched = match_plugins("queue a movie and play some music", plugins)
    names = sorted(p.name for p in matched)
    assert names == ["arr-stack", "media-player"]


def test_empty_keywords_never_matches():
    from glados.plugins.intent import match_plugins
    plugins = [
        _plugin("silent-plugin", []),
        _plugin("arr-stack", ["movie"]),
    ]
    matched = match_plugins("movie movie movie", plugins)
    assert [p.name for p in matched] == ["arr-stack"]


def test_case_insensitive():
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["movie"])]
    matched = match_plugins("MOVIE NIGHT", plugins)
    assert [p.name for p in matched] == ["arr-stack"]


def test_keyword_in_singular_matches_user_plural_via_ies_stem():
    """story -> storIES (operator declares 'story', user says 'stories')."""
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("notes", ["story"])]
    matched = match_plugins("read me my stories", plugins)
    assert [p.name for p in matched] == ["notes"]


def test_empty_message_returns_no_matches():
    from glados.plugins.intent import match_plugins
    plugins = [_plugin("arr-stack", ["movie"])]
    assert match_plugins("", plugins) == []


def test_no_plugins_returns_empty():
    from glados.plugins.intent import match_plugins
    assert match_plugins("any message", []) == []
