FROM python:3.11-slim

LABEL maintainer="PUT_YOUR_EMAIL_HERE"

# System dependencies: ffmpeg for instrumental mixing, git for demucs model fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch wheels — keeps the image small (no CUDA).
# Install torch before demucs so demucs picks up the right build.
RUN pip3 install --no-cache-dir \
    torch==2.1.2+cpu \
    torchaudio==2.1.2+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Demucs and worker runtime dependencies.
RUN pip3 install --no-cache-dir \
    demucs \
    diffq \
    "minio==7.2.9" \
    "redis==5.0.8" \
    requests \
    "modal>=0.73"

WORKDIR /srv
COPY worker-server.py .

ENTRYPOINT ["python3", "worker-server.py"]
