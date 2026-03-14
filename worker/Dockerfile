FROM xserrat/facebook-demucs:latest

LABEL maintainer="PUT_YOUR_EMAIL_HERE"

# Install ffmpeg for instrumental mixing (amix filter)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Install only the packages not already in the base image.
# demucs + torch are pre-installed in xserrat/facebook-demucs — do NOT
# reinstall them here or pip may downgrade torch and break demucs.
RUN pip3 install --no-cache-dir \
    diffq \
    torchcodec \
    "minio==7.2.9" \
    "redis==5.0.8" \
    requests \
    "modal>=0.73"

WORKDIR /srv
COPY worker-server.py .

ENTRYPOINT ["python3", "worker-server.py"]
