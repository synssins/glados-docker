FROM python:3.12-slim

LABEL org.opencontainers.image.title="GLaDOS"
LABEL org.opencontainers.image.description="GLaDOS persona middleware — OpenAI-compatible AI assistant. CPU; bundled local TTS (VITS ONNX); delegates LLM inference to Ollama."
LABEL org.opencontainers.image.source="https://github.com/synssins/glados-docker"

# Container runs:
#   - Local VITS TTS inference on CPU (bundled glados.onnx + ONNX phonemizer)
#   - BGE embedding retrieval on CPU (for entity semantic matching)
# Delegates externally:
#   - LLM inference → Ollama (OLLAMA_URL)
#   - Memory storage → ChromaDB (CHROMADB_URL)
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

# Application source
COPY glados/ ./glados/
COPY configs/config.example.yaml ./configs/config.example.yaml
COPY scripts/ ./scripts/

# Non-root user with home dir (subagent memory writes to ~/.glados/)
RUN useradd -r -u 1000 -g root -m glados && chown -R glados:root /app
USER glados

EXPOSE 8015 8052

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8015/health || exit 1

CMD ["python", "-m", "glados.server"]
