# Stage 3 — MQTT + Intent Classifier (Tentative)

**Status:** Tentative plan — not yet approved for implementation
**Date:** 2026-04-17
**Prerequisite:** Stage 2 (tool execution loop) functional, SSL complete

---

## Problem

The current device control path routes every command through the LLM:

1. User says "turn off the kitchen lights"
2. LLM processes full personality prompt + 21 MCP tools (~5700 tokens)
3. LLM generates tool call (~3-8s)
4. MCP executes against HA (~0.3s)
5. LLM generates follow-up response (~3-8s)
6. Total: 10-20s for a simple light switch

Production voice assistants (Alexa, Google Home, HA Assist) use intent
classifiers that resolve simple commands in ~10ms, falling back to LLM
only for complex queries.

Additionally, HA's MCP tool `GetLiveContext` returns ALL entities
(hundreds), making area-filtered queries unreliable with a 14B model.
Device name matching is strict — "cabinet lights" fails to match
"Kitchen cabinet light switch" without fuzzy resolution.

## Solution

Two-tier command processing inside the GLaDOS container:

### Fast Path (~50-100ms) — Intent Classifier + Local Cache

```
User message
  -> hassil intent parser (~2ms)
  -> Local entity cache + rapidfuzz name matching (~1ms)
  -> MQTT publish OR HA REST API call (~10-50ms)
  -> GLaDOS LLM generates personality response (async, streamed)
```

Lights turn off in <100ms. GLaDOS's snarky confirmation streams 3-5s later.

### Slow Path (~5-15s) — LLM Fallback

```
User message
  -> hassil: no match
  -> GLaDOS LLM with MCP tools (current path)
  -> Full agentic loop for complex queries/conversation
```

## Components

### 1. Local Entity Cache

**Startup population:**
- Single `GET /api/states` call to HA REST API (~200ms)
- Stores: entity_id, friendly_name, domain, area, state, aliases
- Indexed by area and domain for fast lookup

**Runtime updates:**
- MQTT statestream subscription: `homeassistant/#`
- Every state_changed event updates the cache in-place
- No polling, no REST API calls after startup

**Alternative (future):** Replace startup REST call with MQTT retained
discovery messages (`homeassistant/+/+/+/config`). Eliminates the need
for an HA token entirely for state tracking. HA token still needed for
REST API service calls (scenes, automations).

### 2. hassil Intent Classifier

**Library:** `hassil` + `home-assistant-intents` (pip packages)
**Size:** ~2MB, pure Python, no GPU
**Latency:** ~1-5ms per parse

Loads OHF intent templates covering ~50 common home commands:
- turn on/off [device] [in area]
- set [device] brightness to [value]
- activate [scene]
- lock/unlock [door]
- what is the [sensor] in [area]

Entity names and area names loaded from the local cache so hassil
can slot-fill against real device names.

### 3. Fuzzy Name Resolution

**Library:** `rapidfuzz` (already installed)

When hassil extracts a name slot like "cabinet lights", fuzzy match
against the entity cache:
- "cabinet lights" -> "Kitchen cabinet light switch" (score: 88)
- "overhead" -> "Kitchen Overhead Light Switch" (score: 72)

Threshold: score >= 50 to match. Below that, fall through to LLM.

### 4. MQTT Client

**Broker:** User's existing MQTT broker (Mosquitto)
**Auth:** Broker-level username/password (no HA token needed)

**Subscribe:**
- `homeassistant/#` — statestream for cache updates

**Publish (commands):**
- Direct MQTT command topics for MQTT-native devices
- For non-MQTT devices (Z-Wave, ZHA): use HA REST API service calls

**Configuration (new section in config.yaml):**
```yaml
mqtt:
  broker: "mqtt://192.168.1.x:1883"
  username: "glados"
  password: "..."
  statestream_prefix: "homeassistant"
```

### 5. Command Execution

**MQTT-native devices:** Publish directly to command topic
- `home/kitchen/cabinet_light/set` -> `OFF`
- Latency: ~10ms

**HA-managed devices (Z-Wave, ZHA, cloud):** HA REST API service call
- `POST /api/services/light/turn_off` with `entity_id`
- Requires HA token
- Latency: ~50ms

**HA scenes:** REST API service call
- `POST /api/services/scene/turn_on` with `entity_id`
- Or MQTT automation bridge (publish to trigger topic, HA automation
  calls scene.turn_on)

## Data Flow

```
GLaDOS Container Startup:
  1. GET /api/states -> populate entity cache (one REST call)
  2. Connect MQTT broker -> subscribe statestream
  3. Load hassil + OHF templates + entity names from cache
  4. Ready

User: "turn off kitchen cabinet lights"
  hassil: intent=HassTurnOff, name="cabinet lights"
  cache:  area=Kitchen -> fuzzy match -> light.kitchen_cabinet_light_switch
  action: MQTT publish OR REST service call -> light turns off (~50ms)
  LLM:    (async) "Testing illumination in the kitchen deactivated." (~3-5s)

User: "what's the weather forecast for tomorrow?"
  hassil: no match
  LLM:    full agentic loop with tools (~5-15s)
```

## Authentication Summary

| Component | Auth Method | Token Required? |
|-----------|-------------|-----------------|
| Startup entity cache | HA REST API | Yes (HA token) |
| MQTT state subscription | MQTT broker | No (broker credentials) |
| MQTT device commands | MQTT broker | No (broker credentials) |
| HA service calls (scenes, non-MQTT) | HA REST API | Yes (HA token) |
| hassil / rapidfuzz | Local | No |

## Container Changes

**New pip dependencies:**
- `hassil` — intent parser
- `home-assistant-intents` — OHF sentence templates
- `paho-mqtt` — MQTT client

**New modules:**
- `glados/intent/classifier.py` — hassil wrapper
- `glados/intent/entity_cache.py` — in-memory entity store
- `glados/intent/mqtt_client.py` — MQTT pub/sub client
- `glados/intent/resolver.py` — fuzzy name resolution (extract from api_wrapper)

**Modified modules:**
- `glados/core/api_wrapper.py` — fast path intercept before LLM
- `glados/core/engine.py` — initialize intent subsystem
- `glados/webui/tts_ui.py` — config UI for MQTT settings

**New config:**
- `configs/mqtt.yaml` — broker connection, credentials
- WebUI: MQTT section in Configuration tab

## Open Questions

1. Should the fast path be in api_wrapper (streaming chat) or engine
   (all input paths including voice)?
2. Should the entity cache persist to disk (SQLite) or stay in-memory?
3. How to handle devices that aren't on MQTT (Z-Wave direct, cloud)?
   REST API fallback is the current answer.
4. Should MQTT credentials be in the WebUI config or .env only?
5. Future: can MQTT replace the startup REST call entirely using
   retained discovery messages?

## Success Criteria

- "Turn off the kitchen lights" executes in <200ms (vs 10-20s today)
- "Turn on the reading scene" activates the scene in <200ms
- "Kitchen cabinet lights" fuzzy-matches to the correct entity
- Area-wide commands (all lights in kitchen) work without specifying
  individual devices
- State changes from HA reflect in the cache within 1s
- LLM fallback works for anything hassil can't parse
- GLaDOS personality response still streams after fast-path execution
