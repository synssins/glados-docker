# Recommended Models

The GLaDOS container is backend-agnostic — anything that speaks the
OpenAI `/v1/chat/completions` protocol works as an upstream. Two chat
configurations are tuned in production; pick by your VRAM budget and
how warm you want the persona voice to feel.

URLs in **System → Services** are bare `scheme://host:port` — the
container appends `/v1/chat/completions`, `/api/chat`, `/v1/audio/speech`,
etc. at dispatch time. No client-side path knowledge required.

## Chat models (`llm_interactive` slot)

This slot drives Tier 3 chat, the persona rewriter, and tool-call
planning on the home-command path.

### `qwen3:14b` — original recommendation (Ollama)

The model the container shipped on. Mature, low-friction, runs on a
single 16 GB GPU at fp16 or 12 GB at Q4\_K\_M. Good baseline persona
consistency. Around 45–60 tok/s on a 4090, 20–25 tok/s on a 3090.

- **Pull:** `ollama pull qwen3:14b`
- **VRAM:** ~9.3 GB at Q4\_K\_M, ~28 GB at fp16
- **Strengths:** Wide model maturity. Honors the `/no_think` chat-template
  directive injected by the container. Stable persona voice through the
  rewriter overlay.
- **Weak spots:** Tier 2 disambiguator latency (~5–11 s) on a 14B model
  is the bottleneck — split Tier 2 onto `llm_triage` (a smaller model)
  to claw it back. See "Triage" below.

### `qwen3-30b-a3b` — current main (LM Studio or any A3B-capable host)

What runs in the operator's primary deployment. The MoE architecture
activates ~3B params per token, so end-to-end throughput sits closer
to a 3B than a 30B model — ~70 tok/s on a single 4090 at ctx=12288.
Persona voice is noticeably warmer; multi-step planning on the home-
command path is more reliable than 14B.

- **Load (LM Studio CLI):**
  ```
  lms load qwen3-30b-a3b -c 12288 --parallel 4 --gpu max
  ```
- **VRAM:** ~14.6 GB at Q4\_K\_M with ctx=12288. Each additional 4 K of
  ctx costs roughly +1 GB.
- **Strengths:** Higher throughput than 14B, larger usable ctx, better
  tool-call planning. Honors `/no_think`.
- **Caveats:**
  - Use the *hybrid* `qwen3-30b-a3b`. The `qwen3-30b-a3b-thinking-2507`
    variant always reasons; if you see `<think>…</think>` chains on
    chitchat, you have the wrong build.
  - LM Studio's JIT auto-load can silently revert ctx to the model-
    bundled default (4096) on a per-request basis, which makes any
    post-compaction chat history overflow context and re-prefill
    every turn (TTFT spikes from ~3 s to 30+ s). Disable JIT in
    `~/.lmstudio/settings.json` (`developer.jitModelTTL.enabled: false`)
    and load models manually. See `docs/CHANGES.md` Change 28 for the
    full diagnosis.

## Triage model (`llm_triage` slot)

A small, fast model dedicated to classification work: the Tier 2
disambiguator, autonomy memory classifier, and post-conversation
compaction.

### `llama-3.2-1b-instruct` — recommended

About 1.3 GB resident. Drops Tier 2 latency from 5–11 s to 1–2 s.
Tier 2's job is candidate disambiguation against a pre-filtered list
(semantic retrieval already cut the prompt by ~85 %), not free-form
generation — a 1B model is sufficient.

- **Load (LM Studio):**
  ```
  lms load llama-3.2-1b-instruct -c 4096 --parallel 2 --gpu max
  ```
- **Pull (Ollama):** `ollama pull llama3.2:1b`
- **VRAM:** ~1.3 GB at Q4\_K\_M, ctx=4096

`qwen3:8b` works as a substitute if you'd rather keep model families
consistent; it's bigger and slower but still well below the 14B
disambiguator latency.

## Autonomy model (`llm_autonomy` slot)

Background autonomy loops (HA sensor watcher, weather, camera, hacker
news). By default unset and falls back to `llm_interactive`. Operators
with a second GPU can split this onto a separate endpoint to keep
autonomy workloads from crowding the chat path.

## Vision model (`llm_vision` slot, optional)

| Model | Backend | Notes |
|-------|---------|-------|
| `qwen2.5vl:7b` | Ollama | Original recommendation; ~6 GB. |
| `qwen2.5-vl-3b-instruct` | LM Studio | Smaller, fits alongside triage; lower fidelity on detailed scenes. |

Vision is invoked by `camera_watcher` and on demand from the WebUI.
If unconfigured the container still starts — vision queries return a
clear "no vision backend configured" error.

## Routing

All four LLM roles route through `services.yaml` slots:
`llm_interactive`, `llm_autonomy`, `llm_triage`, `llm_vision`.
Configure each in **System → Services**. URL accepts bare
`scheme://host:port`; model name comes from the dropdown populated
from the upstream's `/v1/models` (or `/api/tags`) endpoint.

A single endpoint can host all four. Unset slots fall back to
`llm_interactive`. Mixing backends (Ollama for chat, LM Studio for
vision, etc.) is supported — the container talks to each via stdlib
`http.client` against canonical OpenAI / Ollama-native chunk shapes.

## VRAM math — reference deployment

The operator's primary deployment on a 4090 (24 GB):

| Slot | Model | VRAM at given ctx |
|------|-------|-------------------|
| `llm_interactive` + `llm_autonomy` | `qwen3-30b-a3b` ctx=12288 parallel=4 | 14.6 GB |
| `llm_triage` | `llama-3.2-1b-instruct` ctx=4096 parallel=2 | 1.3 GB |
| `llm_vision` | `qwen2.5-vl-3b-instruct` (when loaded) | 4.5–5 GB |
| **Resident, vision unloaded** | | ~16 GB |
| **Resident, vision loaded** | | ~21 GB (tight) |

Running all three concurrently on 24 GB is workable but tight; the
reference setup keeps vision unloaded until needed.
