FROM python:3.12-slim

LABEL org.opencontainers.image.title="GLaDOS"
LABEL org.opencontainers.image.description="GLaDOS persona middleware — OpenAI-compatible AI assistant. Pure CPU; delegates LLM inference to Ollama and speech synthesis to speaches."
LABEL org.opencontainers.image.source="https://github.com/synssins/glados-docker"

# This container is CPU-only middleware. It does not run any ML inference:
#   - LLM inference is delegated to Ollama via OLLAMA_URL
#   - Speech synthesis is delegated to speaches via SPEACHES_URL
#   - Memory is stored in ChromaDB via CHROMADB_URL
# GPU access provides no benefit and is not supported.

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

# Application source
COPY glados/ ./glados/
COPY configs/config.example.yaml ./configs/config.example.yaml
COPY scripts/ ./scripts/

# Runtime dirs — operator provides real content via volume mounts
RUN mkdir -p /app/configs /app/data /app/logs /app/audio_files /app/certs /app/models

# Non-root user with home dir (subagent memory writes to ~/.glados/)
RUN useradd -r -u 1000 -g root -m glados && chown -R glados:root /app
USER glados

EXPOSE 8015 8052

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8015/health || exit 1

CMD ["python", "-m", "glados.server"]
