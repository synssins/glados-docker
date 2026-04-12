FROM python:3.12-slim

LABEL org.opencontainers.image.title="GLaDOS"
LABEL org.opencontainers.image.description="GLaDOS persona middleware layer — OpenAI-compatible AI assistant"
LABEL org.opencontainers.image.source="https://github.com/YOUR_ORG/glados-container"

# No GPU required — this is pure middleware
# All inference is delegated to ollama and speaches containers

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps — installed before copying source for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[api]"

# Application source
COPY glados/ ./glados/
COPY configs/config.example.yaml ./configs/config.example.yaml
COPY scripts/ ./scripts/

# Runtime dirs created as volumes in compose, pre-created here for standalone use
RUN mkdir -p /app/configs /app/data /app/logs /app/audio_files /app/certs

# Non-root user
RUN useradd -r -u 1000 -g root glados && chown -R glados:root /app
USER glados

EXPOSE 8015 8052

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8015/health || exit 1

CMD ["python", "-m", "glados.server"]
