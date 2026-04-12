FROM python:3.12-slim

LABEL org.opencontainers.image.title="GLaDOS"
LABEL org.opencontainers.image.description="GLaDOS persona middleware layer — OpenAI-compatible AI assistant"
LABEL org.opencontainers.image.source="https://github.com/synssins/glados-docker"

# No GPU required for core functionality.
# TTS (GLaDOS ONNX, 60MB) runs well on CPU — 0.3s-1.6s latency.
# Set GLADOS_USE_GPU=true in compose to enable onnxruntime-gpu instead.
# GPU path requires nvidia runtime on the host — see docker/compose.cuda.yml.

ARG USE_GPU=false

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Python deps — CPU onnxruntime by default
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[api]"

# If USE_GPU=true, swap onnxruntime for onnxruntime-gpu
# onnxruntime and onnxruntime-gpu are mutually exclusive packages
RUN if [ "$USE_GPU" = "true" ]; then \
        pip uninstall -y onnxruntime && \
        pip install --no-cache-dir onnxruntime-gpu; \
    fi

# Application source
COPY glados/ ./glados/
COPY configs/config.example.yaml ./configs/config.example.yaml
COPY scripts/ ./scripts/

# Runtime dirs — operator provides real content via volume mounts
RUN mkdir -p /app/configs /app/data /app/logs /app/audio_files /app/certs /app/models

# Non-root user
RUN useradd -r -u 1000 -g root glados && chown -R glados:root /app
USER glados

EXPOSE 8015 8052

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8015/health || exit 1

CMD ["python", "-m", "glados.server"]
