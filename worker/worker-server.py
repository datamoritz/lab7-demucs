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


def set_job_status(songhash: str, status: str, current_stage: str = "",
                   stage_message: str = "", error: str = "") -> None:
    """Write job status to Redis. Best-effort — failures are logged but not fatal."""
    try:
        redis_client.hset(f"job:{songhash}", mapping={
            "status":        status,
            "current_stage": current_stage or status,
            "stage_message": stage_message,
            "error":         error,
        })
    except Exception as exc:
        log("error", f"Failed to update job status for {songhash}: {exc}")


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
    set_job_status(songhash, "processing",
                   current_stage="downloading_input",
                   stage_message="Downloading input from storage")
    try:
        redis_client.sadd("jobs:processing", songhash)
    except Exception:
        pass

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path  = os.path.join(tmpdir, f"{songhash}.mp3")
            output_dir  = os.path.join(tmpdir, "output")

            # 1. Download input MP3 from MinIO
            log("debug", f"Downloading {bucket}/{input_key}")
            try:
                minio_client.fget_object(bucket, input_key, input_path)
            except S3Error as exc:
                err = f"Failed to download input file: {exc}"
                log("error", err)
                set_job_status(songhash, "failed",
                               current_stage="failed", stage_message=err, error=err)
                return

            # 2. Run Demucs (CPU-only)
            set_job_status(songhash, "processing",
                           current_stage="separating",
                           stage_message="Running Demucs stem separation")
            cmd = (
                f"python3 -m demucs.separate "
                f"--out {output_dir} "
                f"--mp3 "
                f"--device cpu "
                f"-n {model} "
                f"{input_path}"
            )
            log("debug", f"Running: {cmd}")
            rc = os.system(cmd)
            exit_code = rc >> 8  # os.system returns raw waitpid status
            if rc != 0:
                err = f"Demucs exited with code {exit_code}"
                log("error", f"{err} for {songhash}")
                set_job_status(songhash, "failed",
                               current_stage="failed", stage_message=err, error=err)
                return

            # 3. Upload the four separated tracks to MinIO
            #    Demucs writes to: <output_dir>/<model>/<songhash>/<part>.mp3
            set_job_status(songhash, "processing",
                           current_stage="uploading_outputs",
                           stage_message="Uploading separated stems to storage")
            stems_dir   = os.path.join(output_dir, model, songhash)
            upload_errs = []
            for part in PARTS:
                local_file = os.path.join(stems_dir, f"{part}.mp3")
                object_key = f"output/{songhash}-{part}.mp3"

                if not os.path.exists(local_file):
                    msg = f"Expected output not found: {local_file}"
                    log("error", msg)
                    upload_errs.append(msg)
                    continue

                try:
                    minio_client.fput_object(
                        bucket, object_key, local_file,
                        content_type="audio/mpeg",
                    )
                    log("info", f"Uploaded {object_key}")
                except S3Error as exc:
                    msg = f"Upload failed for {object_key}: {exc}"
                    log("error", msg)
                    upload_errs.append(msg)

            if upload_errs:
                err = upload_errs[0]
                set_job_status(songhash, "failed",
                               current_stage="failed", stage_message=err, error=err)
                return

            # 4. Mix instrumental (bass + drums + other) — non-fatal if it fails
            instrumental_path = os.path.join(stems_dir, "instrumental.mp3")
            mix_cmd = (
                f"ffmpeg -y -loglevel error "
                f"-i {os.path.join(stems_dir, 'bass.mp3')} "
                f"-i {os.path.join(stems_dir, 'drums.mp3')} "
                f"-i {os.path.join(stems_dir, 'other.mp3')} "
                f"-filter_complex amix=inputs=3 "
                f"{instrumental_path}"
            )
            log("debug", "Mixing instrumental track")
            if os.system(mix_cmd) == 0 and os.path.exists(instrumental_path):
                try:
                    minio_client.fput_object(
                        bucket, f"output/{songhash}-instrumental.mp3", instrumental_path,
                        content_type="audio/mpeg",
                    )
                    log("info", f"Uploaded output/{songhash}-instrumental.mp3")
                except S3Error as exc:
                    log("error", f"Instrumental upload failed (non-fatal): {exc}")
            else:
                log("error", f"ffmpeg instrumental mix failed (non-fatal) for {songhash}")

        # 5. Fire callback if provided (best-effort, failures are ignored)
        if callback and isinstance(callback, dict) and callback.get("url"):
            try:
                requests.post(callback["url"], json=callback.get("data"), timeout=5)
                log("debug", f"Callback sent to {callback['url']}")
            except Exception as exc:
                log("debug", f"Callback failed (ignored): {exc}")

        set_job_status(songhash, "done",
                       current_stage="done",
                       stage_message="All stems ready")
        log("info", f"Done {songhash}")

    finally:
        # Always remove from the processing set, even if an exception escapes
        try:
            redis_client.srem("jobs:processing", songhash)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    # Clear stale processing entries from a previous crash/restart
    try:
        redis_client.delete("jobs:processing")
        log("info", "Cleared stale jobs:processing set on startup")
    except Exception:
        pass
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
