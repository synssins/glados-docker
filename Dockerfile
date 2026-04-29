FROM python:3.12-slim

LABEL org.opencontainers.image.title="GLaDOS"
LABEL org.opencontainers.image.description="GLaDOS persona middleware — OpenAI-compatible AI assistant. Self-contained: TTS/STT/ChromaDB all embedded. Delegates LLM inference to Ollama."
LABEL org.opencontainers.image.source="https://github.com/synssins/glados-docker"

# Container runs:
#   - Local VITS TTS inference on CPU (bundled glados.onnx + ONNX phonemizer)
#   - Local Parakeet CTC STT on CPU (bundled model)
#   - BGE embedding retrieval on CPU (entity semantic matching)
#   - ChromaDB in-process via PersistentClient (bundled all-MiniLM-L6-v2
#     for vector embeddings). No separate chromadb service required.
# Delegates externally:
#   - LLM inference → Ollama (OLLAMA_URL from YAML)
# GPU access provides no benefit for the workloads we run here.

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps (CPU only)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[api]" \
    && pip install --no-cache-dir certbot certbot-dns-cloudflare

# Runtime dirs — operator provides real content via volume mounts
RUN mkdir -p /app/configs /app/data /app/logs /app/audio_files /app/certs /app/models

# Phase 8.3 — BGE-small-en-v1.5 ONNX for entity semantic retrieval.
# Downloaded at image build so there is no runtime network dependency
# on Hugging Face. The layer is cached across rebuilds as long as the
# URLs don't change. ~130 MB model + ~700 KB tokenizer.
# The model file exists at both onnx/model.onnx and onnx/model_quint8_avx2.onnx
# on HF; we use the full fp32 export for consistent retrieval quality
# on CPU.
RUN curl -fsSL --retry 5 --retry-delay 2 -o /app/models/bge-small-en-v1.5.onnx \
        https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/onnx/model.onnx \
    && curl -fsSL --retry 5 --retry-delay 2 -o /app/models/bge-small-en-v1.5.tokenizer.json \
        https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/tokenizer.json

# GLaDOS TTS voice + ONNX phonemizer — bundled for self-contained TTS.
# ~135 MB (glados.onnx 63.5 MB + phomenizer_en.onnx 61 MB + pickles).
# Ported from dnhkng/GLaDOS. No espeak, no HF, no Speaches required.
COPY models/TTS/ ./models/TTS/

# Parakeet CTC ASR + Silero VAD — bundled for self-contained STT.
# ~440 MB. Enables /v1/audio/transcriptions without external Speaches.
COPY models/ASR/ ./models/ASR/

# Pin the models root so `resource_path()` resolves regardless of cwd
# or how the package was installed (parents[3] fallback doesn't line
# up with the container's `/app/glados/utils/…` layout).
ENV GLADOS_MODELS=/app/models

# Bake all path defaults here so compose.yml doesn't need to set them.
# Operators can still override per-container via compose env, but the
# default-minimal compose (just TZ + volumes + ports) works out of
# the box.
ENV GLADOS_ROOT=/app \
    GLADOS_CONFIG=/app/configs/glados_config.yaml \
    GLADOS_CONFIG_DIR=/app/configs \
    GLADOS_DATA=/app/data \
    GLADOS_LOGS=/app/logs \
    GLADOS_AUDIO=/app/audio_files \
    GLADOS_ASSETS=/app/audio_files \
    GLADOS_TTS_MODELS_DIR=/app/models/TTS \
    GLADOS_PORT=8015 \
    WEBUI_PORT=8052 \
    SERVE_PORT=5051 \
    TTS_BACKEND=local

# Download ChromaDB default embedding model to /tmp; move into place
# after the glados user exists so ownership is correct. (Doing the
# mkdir in /home/glados/ before `useradd -m` breaks: useradd skips
# skel copy when the dir already exists, leaving /home/glados owned
# by root and preventing subagents from creating ~/.glados/.)
RUN curl -fsSL --retry 5 --retry-delay 2 \
        -o /tmp/chroma-minilm.tar.gz \
        https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz

# Application source
COPY glados/ ./glados/
COPY configs/config.example.yaml ./configs/config.example.yaml
COPY scripts/ ./scripts/

# Non-root user with home dir (subagent memory writes to ~/.glados/)
RUN useradd -r -u 1000 -g root -m glados \
    && mkdir -p /home/glados/.cache/chroma/onnx_models/all-MiniLM-L6-v2 \
    && tar -xzf /tmp/chroma-minilm.tar.gz \
        -C /home/glados/.cache/chroma/onnx_models/all-MiniLM-L6-v2 \
    && rm /tmp/chroma-minilm.tar.gz \
    && chown -R glados:root /app /home/glados
USER glados

EXPOSE 8015 8052

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:18015/health || exit 1
# ^ Targets the loopback-only internal API port (always plain HTTP)
# rather than 0.0.0.0:8015 which TLS-wraps when SSL_CERT/SSL_KEY are
# mounted. Internal-port healthcheck is protocol-stable across both
# the no-cert and TLS-enabled deployments.

CMD ["python", "-m", "glados.server"]
