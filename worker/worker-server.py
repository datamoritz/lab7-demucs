#!/usr/bin/env python3
"""Worker: consumes jobs from Redis, runs Demucs, uploads results to MinIO."""

import json
import os
import socket
import sys
import tempfile

import redis
import requests
from minio import Minio
from minio.error import S3Error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_HOST   = os.getenv("REDIS_HOST",       "localhost")
REDIS_PORT   = int(os.getenv("REDIS_PORT",   6379))
MINIO_HOST   = os.getenv("MINIO_HOST",       "localhost")
MINIO_PORT   = os.getenv("MINIO_PORT",       "9000")
MINIO_ACCESS  = os.getenv("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET  = os.getenv("MINIO_SECRET_KEY", "minio123")
MINIO_SECURE  = os.getenv("MINIO_SECURE", "false").lower() == "true"

HOSTNAME = socket.gethostname()
PARTS    = ("bass", "drums", "vocals", "other")

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=0)

minio_client = Minio(
    f"{MINIO_HOST}:{MINIO_PORT}",
    access_key=MINIO_ACCESS,
    secret_key=MINIO_SECRET,
    secure=MINIO_SECURE,
)

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def log(level: str, msg: str) -> None:
    entry = f"{HOSTNAME}.worker.{level}:{msg}"
    try:
        redis_client.lpush("logging", entry)
    except Exception:
        pass
    print(entry, flush=True)


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------
def process(job: dict) -> None:
    songhash  = job["hash"]
    model     = job.get("model", "mdx_extra_q")
    bucket    = job["bucket"]
    input_key = job["key"]          # e.g. "queue/<hash>.mp3"
    callback  = job.get("callback")

    log("info", f"Processing {songhash}")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path  = os.path.join(tmpdir, f"{songhash}.mp3")
        output_dir  = os.path.join(tmpdir, "output")

        # 1. Download input MP3 from MinIO
        log("debug", f"Downloading {bucket}/{input_key}")
        try:
            minio_client.fget_object(bucket, input_key, input_path)
        except S3Error as exc:
            log("error", f"Download failed: {exc}")
            return

        # 2. Run Demucs
        cmd = (
            f"python3 -m demucs.separate "
            f"--out {output_dir} "
            f"--mp3 "
            f"-n {model} "
            f"{input_path}"
        )
        log("debug", f"Running: {cmd}")
        rc = os.system(cmd)
        if rc != 0:
            log("error", f"Demucs exited with code {rc} for {songhash}")
            return

        # 3. Upload the four separated tracks to MinIO
        #    Demucs writes to: <output_dir>/<model>/<songhash>/<part>.mp3
        stems_dir = os.path.join(output_dir, model, songhash)
        for part in PARTS:
            local_file  = os.path.join(stems_dir, f"{part}.mp3")
            object_key  = f"output/{songhash}-{part}.mp3"

            if not os.path.exists(local_file):
                log("error", f"Expected output not found: {local_file}")
                continue

            try:
                minio_client.fput_object(
                    bucket, object_key, local_file,
                    content_type="audio/mpeg",
                )
                log("info", f"Uploaded {object_key}")
            except S3Error as exc:
                log("error", f"Upload failed for {object_key}: {exc}")

    # 4. Fire callback if provided (best-effort, failures are ignored)
    if callback and isinstance(callback, dict) and callback.get("url"):
        try:
            requests.post(callback["url"], json=callback.get("data"), timeout=5)
            log("debug", f"Callback sent to {callback['url']}")
        except Exception as exc:
            log("debug", f"Callback failed (ignored): {exc}")

    log("info", f"Done {songhash}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    log("info", "Worker started, waiting for jobs on 'toWorker'")
    while True:
        try:
            # blpop blocks until a job arrives; returns (key, value)
            _, raw = redis_client.blpop("toWorker", timeout=0)
            job = json.loads(raw.decode("utf-8"))
            process(job)
        except KeyboardInterrupt:
            log("info", "Worker stopped by user")
            sys.exit(0)
        except Exception as exc:
            log("error", f"Unexpected error in main loop: {exc}")


if __name__ == "__main__":
    main()
