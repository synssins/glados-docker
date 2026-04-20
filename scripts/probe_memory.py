"""Diagnostic: ask MemoryContext whether it retrieves the newly-
imported household facts by name. Copied into the container and
run with PYTHONPATH=/app.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from glados.core.memory_context import MemoryContext, MemoryContextConfig  # noqa: E402
from glados.memory import MemoryStore  # noqa: E402


def main() -> None:
    store = MemoryStore.from_chromadb_url(url="http://127.0.0.1:8000")
    ctx = MemoryContext(store=store, config=MemoryContextConfig())
    queries = [
        "Who is ResidentB?",
        "Tell me about Pet1",
        "Pet6 cat",
        "ResidentA's father",
    ]
    for q in queries:
        print()
        print(f"=== {q!r} ===")
        text = ctx.as_prompt(q)
        print(text or "(no memory facts)")


if __name__ == "__main__":
    main()
