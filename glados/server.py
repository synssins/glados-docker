"""GLaDOS container entrypoint.

Starts the API server (port 8015) and WebUI (port 8052) as threads,
then runs the GLaDOS engine on the main thread.

This replaces the two NSSM services (glados-api and glados-tts-ui)
that the host-native deployment uses.

Usage:
    python -m glados.server
    python -m glados.server --port 8015 --webui-port 8052
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Any

from loguru import logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GLaDOS container server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GLADOS_PORT", "8015")),
        help="API listen port (default: 8015, env: GLADOS_PORT)",
    )
    parser.add_argument(
        "--webui-port",
        type=int,
        default=int(os.environ.get("WEBUI_PORT", "8052")),
        help="WebUI listen port (default: 8052, env: WEBUI_PORT)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-webui",
        action="store_true",
        help="Disable the WebUI admin panel",
    )
    parser.add_argument(
        "--input-mode",
        choices=["audio", "text", "both"],
        default=os.environ.get("GLADOS_INPUT_MODE", "text"),
        help="Input mode (default: text)",
    )
    return parser.parse_args()


def _start_webui(host: str, port: int) -> None:
    """Start the WebUI admin panel in a background thread."""
    try:
        from glados.webui.tts_ui import run_webui
        logger.info("Starting WebUI on {}:{}", host, port)
        run_webui(host=host, port=port)
    except ImportError:
        logger.warning("WebUI module not available — admin panel disabled")
    except Exception as exc:
        logger.error("WebUI failed to start: {}", exc)


def _ensure_dirs() -> None:
    """Create required runtime directories if they don't exist."""
    audio_base = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
    data_dir = Path(os.environ.get("GLADOS_DATA", "/app/data"))
    logs_dir = Path(os.environ.get("GLADOS_LOGS", "/app/logs"))

    for d in [
        audio_base / "glados_ha",
        audio_base / "glados_archive",
        audio_base / "glados_announcements",
        audio_base / "glados_commands",
        audio_base / "chat_audio",
        audio_base / "chimes",
        data_dir,
        logs_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def _init_audit_logger() -> None:
    """Initialize the process-wide audit logger from config.

    Called once during startup so any subsequent audit() call from any
    thread writes to disk. Safe to call before the engine is up — the
    logger is independent of engine state.
    """
    try:
        from glados.core.config_store import cfg
        from glados.observability import init_audit_logger

        init_audit_logger(path=cfg.audit.path, enabled=cfg.audit.enabled)
    except Exception as exc:
        # Audit log failure must never prevent the engine from starting.
        logger.warning("Audit logger init failed: {}", exc)


def _fetch_and_apply_registries(client: Any, semantic_index: Any) -> None:
    """Phase 8.3.4 — pull HA's area / device / floor registries
    through the WS client and feed them into the SemanticIndex so
    each entity's document carries its area + device + floor label.

    Runs after HAClient.start() has authenticated; uses the same
    `acall` helper the client uses internally. Failures here degrade
    to documents without the extra facets — retrieval still works,
    it just weights friendly_name more heavily.
    """
    def _sync_call(msg: dict[str, Any]) -> dict[str, Any] | None:
        # HAClient.call() is thread-safe and waits for the WS loop
        # to pick up the message. Used here so we don't fight the
        # event loop from this bootstrap thread.
        try:
            return client.call(msg, timeout_s=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Registry call {!r} failed: {}", msg, exc)
            return None

    calls = (
        ("area", {"type": "config/area_registry/list"},
         semantic_index.apply_area_registry),
        ("device", {"type": "config/device_registry/list"},
         semantic_index.apply_device_registry),
        ("floor", {"type": "config/floor_registry/list"},
         semantic_index.apply_floor_registry),
        # Phase 8.5 — entity registry carries area_id for most
        # entities; without this the build() area→floor resolution
        # can only see state-level area_ids, which HA populates
        # inconsistently.
        ("entity", {"type": "config/entity_registry/list"},
         semantic_index.apply_entity_registry),
    )
    for name, msg, apply_fn in calls:
        resp = _sync_call(msg)
        if not resp or not resp.get("success"):
            logger.debug("HA {} registry fetch returned no data", name)
            continue
        entries = resp.get("result") or []
        if isinstance(entries, list):
            n = apply_fn(entries)
            logger.info(
                "SemanticIndex: applied {} {} registry entries",
                n, name,
            )


def _init_ha_client() -> None:
    """Stage 3 Phase 1: stand up the HA WebSocket client + bridge.

    Runs in the background; the client reconnects if HA is unreachable.
    Failure here must not block engine startup — fast-path intercept
    will simply see `get_bridge() is None` and fall through.

    Also stands up the Tier 2 disambiguator so it can take over when
    Tier 1 (HA conversation API) misses with should_disambiguate.
    """
    try:
        import os
        from glados.core.config_store import cfg
        from glados.ha import (
            ConversationBridge, EntityCache, HAClient, init_singletons,
        )
        from glados.intent import (
            Disambiguator, apply_precheck_overrides, init_disambiguator,
            load_rules_from_yaml,
        )
        from glados.persona import PersonaRewriter, init_rewriter

        token = cfg.ha_token
        ws_url = cfg.ha_ws_url
        if not token:
            logger.warning(
                "HA_TOKEN not set; skipping HA WS client init "
                "(Tier 1 fast-path will be disabled)"
            )
            return

        cache = EntityCache()
        client = HAClient(ws_url=ws_url, token=token, entity_cache=cache)
        client.start()
        bridge = ConversationBridge(client)
        init_singletons(client, bridge, cache)
        logger.info("HA WS client started; url={}", ws_url)

        # Tier 2 disambiguator. Uses the autonomy Ollama (faster T4)
        # because the disambiguator is on the latency path and produces
        # short JSON, not free-form prose.
        # Env vars take precedence over services.yaml (which often has
        # stale hardcoded URLs). DISAMBIGUATOR_OLLAMA_URL is the explicit
        # override; OLLAMA_AUTONOMY_URL is the next preference.
        ollama_url = (
            os.environ.get("DISAMBIGUATOR_OLLAMA_URL", "").strip()
            or os.environ.get("OLLAMA_AUTONOMY_URL", "").strip()
            or cfg.service_url("llm_autonomy")
        )
        # Model source of truth: the Ollama Autonomy row on the LLM &
        # Services page (services.yaml.llm_autonomy.model). Hot-reload
        # picks up changes. Env var DISAMBIGUATOR_MODEL is kept as an
        # explicit override for operators who want the disambiguator on
        # a different model than everything else.
        disambig_model = os.environ.get("DISAMBIGUATOR_MODEL", "").strip() \
            or cfg.service_model("llm_autonomy", fallback="qwen3:8b")
        # Operator's disambiguation rules YAML, optional.
        config_dir = os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")
        rules = load_rules_from_yaml(
            os.path.join(config_dir, "disambiguation.yaml")
        )
        # Phase 8.2 — activate operator-supplied precheck extras
        # (command verbs + ambient regexes). Merges additively with
        # the shipped defaults. Repeated on hot-reload.
        apply_precheck_overrides(rules)

        # Phase 8.3 — SemanticIndex for the disambiguator. Built on
        # a background thread so startup isn't blocked by the ~2 s
        # one-time embed pass. The disambiguator falls back to the
        # fuzzy matcher while the build is in flight; once the
        # index is ready, semantic retrieval + device-diversity
        # filtering take over on the next request.
        from glados.ha.semantic_index import (
            SemanticIndex, is_semantic_retrieval_available,
        )
        semantic_index: SemanticIndex | None = None
        if is_semantic_retrieval_available():
            try:
                semantic_index = SemanticIndex(cache=cache)
                # Try a warm restore first; on miss the background
                # build writes a fresh one.
                if not semantic_index.load():
                    logger.info(
                        "SemanticIndex: no cached index on disk; "
                        "building from live cache"
                    )
                import threading
                import time as _time

                def _bootstrap_semantic_index() -> None:
                    # Wait for the HA WS client to connect AND the
                    # entity cache to populate before fetching
                    # registries or building the index. Bounded so
                    # a never-reachable HA doesn't leave a permanent
                    # thread idling here.
                    deadline = _time.monotonic() + 60.0
                    while _time.monotonic() < deadline:
                        try:
                            if client.is_connected() and cache.size() > 0:
                                break
                        except Exception:  # noqa: BLE001
                            pass
                        _time.sleep(0.5)
                    else:
                        logger.warning(
                            "SemanticIndex bootstrap: HA cache not "
                            "populated within 60s; skipping"
                        )
                        return
                    try:
                        _fetch_and_apply_registries(client, semantic_index)
                        n = semantic_index.build()
                        if n > 0:
                            semantic_index.persist()
                            logger.info(
                                "SemanticIndex ready (entities={})", n,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "SemanticIndex bootstrap failed: {}", exc,
                        )
                threading.Thread(
                    target=_bootstrap_semantic_index,
                    name="SemanticIndexBootstrap",
                    daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SemanticIndex init skipped: {}", exc,
                )
                semantic_index = None
        else:
            logger.info(
                "Semantic retrieval unavailable (model files or "
                "deps missing); Tier 2 stays on fuzzy matcher"
            )

        disambig = Disambiguator(
            ha_client=client, cache=cache,
            ollama_url=ollama_url, model=disambig_model,
            rules=rules,
            semantic_index=semantic_index,
        )
        init_disambiguator(disambig)
        logger.info(
            "Tier 2 disambiguator ready; ollama={} model={} semantic={}",
            ollama_url, disambig_model, bool(semantic_index),
        )

        # Persona rewriter for Tier 1 hits (HA's plain "Turned off the
        # light." -> GLaDOS-voiced restyling). Same Ollama as the
        # disambiguator; smaller models work well here since the input
        # is short and the output is constrained to one or two sentences.
        # Rewriter defaults to the same autonomy model as the
        # disambiguator. Env var REWRITER_MODEL is kept as an explicit
        # override for operators who want to pin the rewriter to a
        # small/fast model independent of the chat / autonomy choice.
        rewriter_model = os.environ.get("REWRITER_MODEL", "").strip() \
            or cfg.service_model("llm_autonomy", fallback="qwen3:8b")
        rewriter = PersonaRewriter(ollama_url=ollama_url, model=rewriter_model)
        init_rewriter(rewriter)
        logger.info("Persona rewriter ready; model={}", rewriter_model)

        # CommandResolver — the single entry point for home-control
        # intents. Sits in front of Tier 1 / Tier 2 and adds short-term
        # session memory + durable learned-context with HA validation.
        # See glados/core/command_resolver.py.
        from pathlib import Path

        from glados.core.command_resolver import (
            CommandResolver, HAStateValidator, init_resolver,
        )
        from glados.core.learned_context import LearnedContextStore
        from glados.core.session_memory import SessionMemory
        from glados.core.user_preferences import load_user_preferences

        data_dir = Path(os.environ.get("GLADOS_DATA", "/app/data"))
        prefs_path = Path(config_dir) / "user_preferences.yaml"
        preferences = load_user_preferences(prefs_path)
        # Phase 8.8 — read the anaphora follow-up window from the
        # memory config. Default 600 s; YAML override surfaces in the
        # Memory page as session_idle_ttl_seconds.
        from glados.core.config_store import cfg as _cfg
        session_ttl = max(1, int(_cfg.memory.session_idle_ttl_seconds))
        session_memory = SessionMemory(idle_ttl_seconds=session_ttl)
        learned_store = LearnedContextStore(data_dir / "learned_context.db")
        state_validator = HAStateValidator(entity_cache=cache)
        resolver = CommandResolver(
            bridge=bridge,
            disambiguator=disambig,
            rewriter=rewriter,
            session_memory=session_memory,
            learned_context=learned_store,
            preferences=preferences,
            state_validator=state_validator,
        )
        init_resolver(resolver)
        logger.info(
            "CommandResolver ready; learned_ctx={} prefs={}",
            data_dir / "learned_context.db", prefs_path,
        )
    except Exception as exc:
        logger.warning("HA WS / Tier 2 init failed: {}", exc)


def main() -> None:
    args = _parse_args()

    logger.info("GLaDOS container starting")
    logger.info("  API port:   {}", args.port)
    logger.info("  WebUI port: {}", args.webui_port)
    logger.info("  Input mode: {}", args.input_mode)
    logger.info("  Config dir: {}", os.environ.get("GLADOS_CONFIG_DIR", "/app/configs"))

    # Ensure runtime directories exist
    _ensure_dirs()

    # Initialize audit logger early so startup events can be captured.
    _init_audit_logger()

    # Stage 3 Phase 1: connect to HA WS for Tier 1 fast-path.
    _init_ha_client()

    # Start WebUI in background thread
    if not args.no_webui:
        webui_thread = threading.Thread(
            target=_start_webui,
            args=(args.host, args.webui_port),
            name="WebUI",
            daemon=True,
        )
        webui_thread.start()

    # Start API wrapper — this imports the engine and runs it on the main thread
    # Import here so env vars are fully set before any glados module loads
    from glados.core.api_wrapper import main as api_main

    # Patch sys.argv so api_wrapper's argparse reads our values
    sys.argv = [
        "glados.server",
        "--port", str(args.port),
        "--host", args.host,
        "--input-mode", args.input_mode,
    ]

    logger.info("Handing off to API wrapper")
    api_main()


if __name__ == "__main__":
    main()
