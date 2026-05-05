"""Unit tests for the gate that decides whether MCP context messages
(HA entity catalog, plugin context resources) are injected as system
messages on a given LLM turn.

History: until this gate landed, autonomy ticks fired every ~30 s and
EACH tick paid for the full MCP context dump (~16 K tokens — HA
catalog + per-plugin context blocks). Compaction never reduced this
because compaction only watches `conversation_store`, while MCP
context is composed at request-build time from
`mcp_manager.get_context_messages()`. The autonomy 4 B lane was
saturated as a direct result.

Strictly subtractive: home-command chat turns still get the catalog
(unchanged); chitchat still skips it (unchanged); autonomy now also
skips it (new). Tools remain available — only the static catalog as
system text is dropped.
"""

from glados.core.llm_processor import _should_include_mcp_context


# ── home-command chat ⇒ include MCP context (unchanged behaviour) ────


def test_home_command_chat_includes_mcp_context():
    assert _should_include_mcp_context(autonomy_mode=False, is_chitchat=False) is True


# ── chitchat ⇒ skip (unchanged behaviour) ────────────────────────────


def test_chitchat_chat_skips_mcp_context():
    assert _should_include_mcp_context(autonomy_mode=False, is_chitchat=True) is False


# ── autonomy ⇒ skip (NEW behaviour) ──────────────────────────────────


def test_autonomy_skips_mcp_context_even_when_not_chitchat():
    """The autonomy loop fires clockwork ticks; even when its synthetic
    'user' message would not be classified as chitchat, dumping the
    full MCP catalog every tick consumes ~16 K tokens to make a
    do_nothing decision. Tool definitions stay available — autonomy
    can still call HA tools — only the catalog system messages drop.
    """
    assert _should_include_mcp_context(autonomy_mode=True, is_chitchat=False) is False


def test_autonomy_skips_mcp_context_when_also_chitchat():
    # Defensive: autonomy_mode=True with is_chitchat=True is unreachable
    # via the production code path (the chitchat check only runs for
    # non-autonomy turns), but the helper must still return False so a
    # future caller refactor can't accidentally re-include the catalog.
    assert _should_include_mcp_context(autonomy_mode=True, is_chitchat=True) is False
