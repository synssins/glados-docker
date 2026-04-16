"""
Conversation compaction agent.

Monitors conversation length and compacts older messages when
approaching token limits. Uses LLM to summarize and extract facts.

Write path: extracted facts → ChromaDB MemoryStore (semantic collection)
Fallback:   if ChromaDB unavailable, facts are silently dropped
            (compaction still happens — memory persistence is best-effort)

Platform note: Pure Python, no platform-specific code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from ..llm_client import LLMConfig
from ..subagent import Subagent, SubagentConfig, SubagentOutput
from ..summarization import estimate_tokens, extract_facts, summarize_messages

if TYPE_CHECKING:
    from ...core.conversation_store import ConversationStore
    from ...memory.chromadb_store import MemoryStore


class CompactionAgent(Subagent):
    """
    Monitors conversation and compacts when token count gets high.

    Preserves recent messages while summarizing older ones.
    Extracted facts are stored in ChromaDB for semantic retrieval.
    """

    def __init__(
        self,
        config: SubagentConfig,
        llm_config: LLMConfig | None = None,
        conversation_store: "ConversationStore | None" = None,
        memory_store: "MemoryStore | None" = None,
        token_threshold: int = 6000,
        preserve_recent: int = 15,
        **kwargs,
    ) -> None:
        """
        Initialize the compaction agent.

        Args:
            config: Subagent configuration.
            llm_config: LLM configuration for summarization calls.
            conversation_store: Thread-safe conversation store to compact.
            memory_store: ChromaDB MemoryStore for persisting extracted facts.
                          If None, facts are not persisted (compaction still works).
            token_threshold: Start compacting when tokens exceed this.
            preserve_recent: Number of recent messages to keep uncompacted.
        """
        super().__init__(config, **kwargs)
        self._llm_config = llm_config
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._token_threshold = token_threshold
        self._preserve_recent = preserve_recent
        self._last_compaction_size = 0

    def tick(self) -> SubagentOutput | None:
        """Check conversation size and compact if needed."""
        if not self._llm_config:
            return SubagentOutput(
                status="idle",
                summary="No LLM configured",
                notify_user=False,
            )

        if not self._conversation_store:
            return SubagentOutput(
                status="idle",
                summary="No conversation store configured",
                notify_user=False,
            )

        messages = self._conversation_store.snapshot()
        token_count = estimate_tokens(messages)

        if token_count < self._token_threshold:
            return SubagentOutput(
                status="monitoring",
                summary=f"Context at {token_count} tokens (threshold: {self._token_threshold})",
                notify_user=False,
            )

        # Find compactable messages.
        # Protected: initial preprompt messages (by index) and recent messages.
        # Everything else is fair game — including old [summary] system messages,
        # which previously accumulated forever and caused unbounded context growth.
        preprompt_count = getattr(self._conversation_store, "preprompt_count", 0)
        compactable_indices = []
        for i, msg in enumerate(messages):
            if i < preprompt_count:
                continue  # Protect personality preprompt
            if i >= len(messages) - self._preserve_recent:
                continue  # Protect recent messages
            compactable_indices.append(i)

        if len(compactable_indices) < 3:
            return SubagentOutput(
                status="monitoring",
                summary=f"At {token_count} tokens but not enough compactable messages",
                notify_user=False,
            )

        # Compact oldest half
        half = max(3, len(compactable_indices) // 2)
        indices_to_compact = compactable_indices[:half]
        messages_to_compact = [messages[i] for i in indices_to_compact]

        logger.info(
            "CompactionAgent: compacting {} messages ({} tokens)",
            len(messages_to_compact),
            estimate_tokens(messages_to_compact),
        )

        summary = summarize_messages(messages_to_compact, self._llm_config)
        facts = extract_facts(messages_to_compact, self._llm_config)

        if not summary:
            return SubagentOutput(
                status="error",
                summary="Failed to generate summary",
                notify_user=False,
            )

        summary_content = f"[summary] Previous conversation summary: {summary}"

        # Rebuild conversation history with compacted summary replacing old messages
        current_messages = self._conversation_store.snapshot()
        new_history = []
        summary_inserted = False

        for i, msg in enumerate(current_messages):
            if i in indices_to_compact:
                if not summary_inserted:
                    new_history.append({
                        "role": "system",
                        "content": summary_content,
                    })
                    summary_inserted = True
                continue
            new_history.append(msg)

        self._conversation_store.replace_all(new_history)
        new_token_count = estimate_tokens(new_history)
        self._last_compaction_size = len(indices_to_compact)

        # Persist extracted facts to ChromaDB
        facts_stored = 0
        if facts and self._memory_store:
            logger.info("CompactionAgent: storing {} facts in ChromaDB", len(facts))
            for fact_text in facts:
                try:
                    self._memory_store.add_semantic(
                        text=fact_text,
                        metadata={
                            "source": "conversation_compaction",
                            "importance": 0.6,
                        },
                    )
                    facts_stored += 1
                except Exception as exc:
                    logger.warning("CompactionAgent: failed to store fact in ChromaDB: {}", exc)
        elif facts:
            logger.debug(
                "CompactionAgent: {} facts extracted but no ChromaDB store — not persisted",
                len(facts),
            )

        # Also store the summary itself as a semantic memory
        if summary and self._memory_store:
            try:
                self._memory_store.add_semantic(
                    text=f"Conversation summary: {summary}",
                    metadata={
                        "source": "compaction_summary",
                        "importance": 0.5,
                        "messages_compacted": len(indices_to_compact),
                    },
                )
            except Exception as exc:
                logger.warning("CompactionAgent: failed to store summary in ChromaDB: {}", exc)

        return SubagentOutput(
            status="compacted",
            summary=(
                f"Compacted {len(indices_to_compact)} messages: "
                f"{token_count} -> {new_token_count} tokens, "
                f"{facts_stored} facts stored in ChromaDB"
            ),
            notify_user=False,
            raw={
                "compacted_count": len(indices_to_compact),
                "facts_extracted": len(facts),
                "facts_stored_chromadb": facts_stored,
                "tokens_before": token_count,
                "tokens_after": new_token_count,
            },
        )
