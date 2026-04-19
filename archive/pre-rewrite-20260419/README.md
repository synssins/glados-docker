# Pre-rewrite archive — 2026-04-19

Stage 3 Phase 7 consolidated all user-command ingress behind
`POST /v1/chat/completions` and the new `CommandResolver`
(`glados/core/command_resolver.py`). The ESPHome-direct `/command`
endpoint and its supporting `commands.yaml` / keyword-matcher /
WAV-playback pipeline were removed.

## What was deleted

| Symbol / path                             | Lived in                              |
|-------------------------------------------|---------------------------------------|
| `POST /command` route                     | `glados/core/api_wrapper.py`          |
| `APIHandler._handle_command()`            | `glados/core/api_wrapper.py`          |
| `handle_command()` (module function)      | `glados/core/api_wrapper.py`          |
| `match_light_command()`                   | `glados/core/api_wrapper.py`          |
| `_load_cmd_config()`                      | `glados/core/api_wrapper.py`          |
| `_ha_call_service()`                      | `glados/core/api_wrapper.py`          |
| `_find_cmd_base_wav()`                    | `glados/core/api_wrapper.py`          |
| `_pick_cmd_followup_wavs()`               | `glados/core/api_wrapper.py`          |
| `_cleanup_old_commands()`                 | `glados/core/api_wrapper.py`          |
| `COMMANDS_YAML`, `_cmd_config`, `_cmd_lock` | `glados/core/api_wrapper.py`        |
| `configs/commands.yaml`                   | operator deployment (gitignored)      |
| `_RecentTierAction` + carry-over stash    | `glados/core/api_wrapper.py`          |
| `_stash_recent_tier_action()`             | `glados/core/api_wrapper.py`          |
| `_get_recent_tier_action()`               | `glados/core/api_wrapper.py`          |
| `_clear_recent_tier_action()`             | `glados/core/api_wrapper.py`          |
| `_should_carry_over_home_command()`       | `glados/core/api_wrapper.py`          |
| `_last_ha_conversation_id()`              | `glados/core/api_wrapper.py`          |
| `_try_tier2_disambiguation()`             | `glados/core/api_wrapper.py`          |
| `_FOLLOWUP_HOME_COMMAND_WINDOW_S`         | `glados/core/api_wrapper.py`          |
| `tests/test_home_command_carryover.py`    | tests/                                |

## Why it went

- **Voice and chat are one pipeline now.** Home Assistant is the voice
  front-end; it forwards voice utterances through the OpenAI-compatible
  chat-completions endpoint with `area_id` metadata attached. The
  container no longer needs its own ESPHome-direct ingress.
- **Keyword + alias matching replaced by LLM disambiguation + learned
  context.** `match_light_command()` required a hand-curated `commands.yaml`
  keyed on exact aliases. The new `CommandResolver` lets Tier 2's LLM
  pick entities from HA's live registry and learns user patterns over
  time (durable store + HA-validated before execute).
- **Carry-over stash superseded by `SessionMemory`.** The old in-memory
  `_RECENT_TIER_ACTION` dict was keyed on a single `"default"`
  conversation id. The new `SessionMemory` keys on `SourceContext.session_id`,
  so multiple concurrent WebUI sessions don't clobber each other.

## Where to find the code

All of it is preserved in git history. To see the removed code in
context, check out any commit on `main` prior to the Stage 3 Phase 7
milestone commit.

## What replaces it

| Old symbol                             | New home                                       |
|----------------------------------------|------------------------------------------------|
| `handle_command()` / `match_light_command` / `_load_cmd_config` | `CommandResolver.resolve()` — `glados/core/command_resolver.py` |
| `commands.yaml`                        | HA `label_registry` + `configs/user_preferences.yaml` |
| carry-over stash                       | `SessionMemory` — `glados/core/session_memory.py` |
| `_last_ha_conversation_id()`           | `Turn.ha_conversation_id` in `SessionMemory`   |
| `_try_tier2_disambiguation()`          | Internal to `CommandResolver._try_tier2()`     |
