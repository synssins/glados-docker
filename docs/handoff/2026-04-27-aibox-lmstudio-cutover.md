# AIBox + LM Studio cutover (2026-04-27)

**TL;DR.** AIBox is now an LLM-only host running mainline LM Studio
(headless `llmster` daemon) on the Intel Arc Pro B60 via Vulkan
(llama.cpp 2.14.0 beta). It serves GLM-4.7-Flash and Qwen2.5-VL-3B
concurrently. Replaces the dead/archived IPEX-LLM Ollama. The native
GLaDOS NSSM stack (glados-api, glados-tts, glados-vision, etc.) on
AIBox is intentionally stopped — production GLaDOS personality runs
on the Docker container at `docker-host.local` and treats AIBox as an
opaque OpenAI-compatible LLM endpoint.

## Ground rules (operator-locked)

1. **GLaDOS Docker container code = 100% OpenAI-compliant in both
   directions.**
   - What it sends to the LLM upstream → OpenAI shape (`/v1/...`).
   - What it serves to clients → OpenAI shape (`/v1/...`).
   - Ollama-native paths (`/api/tags`, `/api/chat`) are legacy /
     compat-shim only; the cleanup item list below tracks what needs
     ripping out.
2. **LM Studio code/behavior is off-limits.** No code edits, no
   custom plugins, no GLaDOS-specific accommodations on the LM
   Studio side. Settings (parallel slots, ctx, ttl, GPU pin,
   runtime selection) are tunable.
3. **No GLM-specific code in GLaDOS.** Anything that breaks for GLM
   should be fixed by making GLaDOS more strictly OpenAI-compliant,
   not by special-casing GLM. The same fix should help any
   reasoning-mode OpenAI-compatible model (DeepSeek-R1, OpenAI o-
   series, GLM-4.x, future Qwen-think variants, etc.).

## Hardware

- **Host:** AIBox = `WIN-GTLJ7GFJPC4` @ `aibox.local`,
  Windows Server 2022 Datacenter, ~64 GB RAM, Intel i7-9700K.
- **GPUs:** **Intel Arc Pro B60 24 GB** is the LLM target. Driver
  `32.0.101.8314` (Q4'25 Arc Pro WHQL workstation, 2026-02-11). 2×
  Tesla T4 (TCC mode) are present but ignored — Vulkan does not
  enumerate them.
  - **Vulkan double-enumerates the B60.** `lms runtime survey`
    shows 2× "Intel(R) Arc(TM) Pro B60 Graphics (Vulkan, Discrete)"
    at 23.88 GiB. This is a known llama.cpp/Vulkan ICD quirk on
    Intel — same physical adapter exposed twice via different driver
    paths. PCI bus, PnP, and Win32_VideoController all show exactly
    one B60 (`PCI\VEN_8086&DEV_E211&SUBSYS_60231849`). Empirically
    proven harmless: the runtime allocates on one device, no layer
    splitting, no VRAM doubling.

## Software stack

| Layer | Where | What |
|---|---|---|
| LLM runtime | `C:\Users\Administrator\.lmstudio\` | LM Studio CLI `lms`, llmster headless daemon `0.0.12-1` |
| Inference engine | bundled in LM Studio | llama.cpp Vulkan `2.14.0` (beta channel; stable 2.13.0 didn't recognize GLM-4.7-Flash arch) |
| Server | `0.0.0.0:11434` | LM Studio OpenAI-compat HTTP server (no Ollama-native endpoints — `/api/tags`, `/api/chat`, etc. all return 404 with error JSON body) |
| Reasoning model | `lmstudio-community/GLM-4.7-Flash-GGUF` Q4_K_M | 16.89 GiB on disk, ~18 GB VRAM at 4K ctx; 30B-A3B MoE; arch `deepseek2`; 64 experts × 4 active per token; native ctx 202,752 |
| Vision model | `lmstudio-community/Qwen2.5-VL-3B-Instruct-GGUF` Q4_K_M | 3.27 GB total (1.84 GB weights + 1.28 GB mmproj-f16); arch `qwen2vl`; native ctx 128K |
| Test model | `lmstudio-community/Llama-3.2-1B-Instruct-GGUF` Q8_0 | 1.32 GB; kept for runtime sanity |

Both production models are loaded with `--gpu max --context-length
4096`, parallel=4, no TTL (always loaded). VRAM totals ~20 GB on the
24 GB card.

## Why LM Studio (not mainline Ollama or vLLM)

| Engine | Multi-model in one process | GLM-4.7-Flash chat template | OpenAI-compat | Notes |
|---|---|---|---|---|
| Ollama mainline | ✅ | Stable in 0.14.3+; we'd need pre-release | ✅ | Native protocol mismatch with our middleware contract |
| LM Studio (`llmster`) | ✅ | ✅ today (curated GGUF) | ✅ exclusive | Picked |
| LLM-Scaler vLLM | ❌ (1 model/process) | ✅ | ✅ | VRAM math doesn't fit two containers on one B60 |
| llama.cpp SYCL | ❌ (1 model/server) | ✅ | ✅ | More operational overhead |

Ollama as the engine was rejected because its native protocol
(`/api/chat`, `/api/tags`) leaks into the GLaDOS contract. LM Studio
is OpenAI-only, which forces the GLaDOS code to be properly
OpenAI-compliant — a feature, not a bug.

## State changes from prior architecture

| Component | Before | After |
|---|---|---|
| `ollama-ipex-llm` NSSM service | Running, served qwen3:14b | **Stopped + Disabled.** `nssm set ollama-ipex-llm Start SERVICE_DISABLED`. Files retained at `C:\AI\ollama-ipex-llm\` for rollback. |
| Default LLM port `:11434` | ipex-llm-ollama | LM Studio (`llmster` foreground) |
| `glados_config.yaml` `Glados.completion_url` | `http://aibox.local:11434/api/chat` | `http://aibox.local:11434/v1/chat/completions` |
| `glados_config.yaml` `Glados.llm_model` | `qwen3:14b` | `glm-4.7-flash` |
| Same for `Glados.autonomy.*` | (same) | (same) |
| `services.yaml` `ollama_*.url` | `http://aibox.local:11434` (bare) | `http://aibox.local:11434/v1/chat/completions` |
| `services.yaml` `ollama_vision.model` | `llama3.2-vision:latest` | `qwen2.5-vl-3b-instruct` |

## LM Studio install path

Repeatable on a fresh AIBox:

```powershell
# Install LM Studio (headless llmster daemon + lms CLI):
irm https://lmstudio.ai/install.ps1 | iex

# Beta-channel runtime for GLM-4.7-Flash arch support:
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" runtime get llama.cpp:vulkan --channel beta -y
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" runtime select llama.cpp-win-x86_64-vulkan-avx2@2.14.0

# Server bound LAN-wide on the legacy Ollama port:
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" server start --port 11434 --bind 0.0.0.0 --cors

# Pull production models (use BITS for resilience — `lms get`
# rolls back partial downloads on shell timeout):
$base = "$env:USERPROFILE\.lmstudio\models\lmstudio-community"
foreach ($t in @(
    @{repo='GLM-4.7-Flash-GGUF';            file='GLM-4.7-Flash-Q4_K_M.gguf'},
    @{repo='Qwen2.5-VL-3B-Instruct-GGUF';   file='Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf'},
    @{repo='Qwen2.5-VL-3B-Instruct-GGUF';   file='mmproj-model-f16.gguf'}
)) {
    $dst = "$base\$($t.repo)\$($t.file)"
    New-Item -ItemType Directory -Path (Split-Path $dst -Parent) -Force | Out-Null
    Start-BitsTransfer `
        -Source "https://huggingface.co/lmstudio-community/$($t.repo)/resolve/main/$($t.file)" `
        -Destination $dst -Priority Foreground
}

# Load models with always-resident keepalive:
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" load qwen2.5-vl-3b-instruct --gpu max --context-length 4096 -y
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" load glm-4.7-flash --gpu max --context-length 4096 -y
```

NSSM-wrapping `lms server` for reboot survival is the next operational
step (see "Open work" doc).

## Why the unsloth GLM-4.7-Flash GGUF didn't work

First attempt was `unsloth/GLM-4.7-Flash-GGUF` Q4_K_S (auto-resolved
by `lms get`). LM Studio's metadata cache parsed it (`arch:
deepseek2`, 30B-A3B MoE) but the indexer silently skipped it — never
showed in `lms ls`, no entry in `model-index-cache.json`, no entry
in `badModels`. Likely an unsloth-specific GGUF metadata quirk.

Switched to `lmstudio-community/GLM-4.7-Flash-GGUF` Q4_K_M (LM
Studio team's curated build, llama.cpp b7779). Indexer accepted
immediately. Use lmstudio-community when both are available.

## Critical knobs (LM Studio settings the operator can tune)

| Knob | Where | Current |
|---|---|---|
| Runtime selection | `lms runtime select` | `llama.cpp-win-x86_64-vulkan-avx2@2.14.0` (beta channel — needed for GLM arch) |
| Server bind | `lms server start --bind` | `0.0.0.0:11434` |
| Per-model parallel | `lms load --parallel` | `4` (default) — chat + autonomy share these slots |
| Per-model ctx | `lms load --context-length` | `4096` — could go to 8K-32K with VRAM headroom checks |
| Per-model TTL | `lms load --ttl` | unset (always loaded) |
| Vulkan device pin | `~/.lmstudio/.internal/preferred-gpu.json` | not pinned; runtime picks B60 automatically because Vulkan only enumerates B60 (T4s in TCC mode are invisible) |

VRAM budget at current config: ~20 GB used of 24 GB. Headroom for
moderate ctx growth on either model.
