"""Local fast-paths for deterministic queries.

Time and weather questions have answers fully determined by data the
container already has on hand (``time_source.now()``,
``weather_cache.get_data()``). Sending them through the chat LLM is
ceremony — the round-trip cost is dominated by prompt processing on
the persona preprompt, not by anything the model actually computes.

This package short-circuits those queries: parse the utterance, pull
from the local data source, render 1-2 sentences, run through the
persona rewriter for character voice, and return. The full Tier 3
chat path is bypassed.

See ``docs/roadmap.md`` "Time & weather fast-path" for the design
context.
"""

from .local import try_time, try_weather

__all__ = ["try_time", "try_weather"]
