#!/usr/bin/env python3
"""Worker: consumes jobs from Redis, runs Demucs, uploads results to MinIO.

Routing rule
------------
If USE_MODAL=true AND the input file exceeds LOCAL_THRESHOLD_BYTES (200 KB),
the job is sent to the Modal serverless GPU function (separate_stems).
Smaller files (demo clips) are always processed locally on CPU.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO

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

HEARTBEAT_KEY      = "worker:heartbeat"
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 30))
HEARTBEAT_TTL      = 120   # Redis auto-expires key if worker dies

# Modal routing — disabled by default; set USE_MODAL=true in .env to enable
USE_MODAL             = os.getenv("USE_MODAL", "false").lower() == "true"
LOCAL_THRESHOLD_BYTES = int(os.getenv("LOCAL_THRESHOLD_BYTES", 200 * 1024))  # 200 KB

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
# Heartbeat — background thread writes liveness + connectivity to Redis
# ---------------------------------------------------------------------------
def _heartbeat_loop() -> None:
    """Every HEARTBEAT_INTERVAL seconds, probe Redis and MinIO and record results.

    If the worker cannot reach Redis the heartbeat key stays absent/stale,
    which the API interprets as 'worker unhealthy'.
    """
    minio_bucket = os.getenv("MINIO_BUCKET", "demucs-bucket")
    while True:
        redis_ok    = False
        redis_error = ""
        minio_ok    = False
        minio_error = ""

        try:
            redis_client.ping()
            redis_ok = True
        except Exception as exc:
            redis_error = str(exc)[:200]
            print(f"{HOSTNAME}.worker.error:Heartbeat: Redis unreachable — {exc}", flush=True)

        try:
            minio_client.bucket_exists(minio_bucket)
            minio_ok = True
        except Exception as exc:
            minio_error = str(exc)[:200]
            print(f"{HOSTNAME}.worker.error:Heartbeat: MinIO unreachable — {exc}", flush=True)

        if redis_ok:
            try:
                redis_client.hset(HEARTBEAT_KEY, mapping={
                    "last_seen":   int(time.time()),
                    "hostname":    HOSTNAME,
                    "redis_ok":    "1",
                    "minio_ok":    "1" if minio_ok else "0",
                    "redis_error": "",
                    "minio_error": minio_error,
                })
                redis_client.expire(HEARTBEAT_KEY, HEARTBEAT_TTL)
            except Exception as exc:
                print(f"{HOSTNAME}.worker.error:Heartbeat write failed: {exc}", flush=True)
        else:
            # Cannot write to Redis — heartbeat will be absent/stale; API detects this
            print(
                f"{HOSTNAME}.worker.error:Heartbeat skipped (Redis down): {redis_error}",
                flush=True,
            )

        time.sleep(HEARTBEAT_INTERVAL)


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

            file_size = os.path.getsize(input_path)
            use_modal = USE_MODAL and file_size > LOCAL_THRESHOLD_BYTES

            if use_modal:
                # ── Modal GPU path ───────────────────────────────────────────
                log("info", f"Routing to Modal GPU ({file_size}B > {LOCAL_THRESHOLD_BYTES}B threshold)")
                set_job_status(songhash, "processing",
                               current_stage="separating",
                               stage_message="Running Demucs on Modal GPU (A10G)")

                with open(input_path, "rb") as f:
                    mp3_bytes = f.read()

                try:
                    import modal as _modal
                    _fn = _modal.Function.from_name("demucs-gpu-separation", "separate_stems")
                    stem_bytes = _fn.remote(mp3_bytes, songhash, model)
                except Exception as exc:
                    err = f"Modal GPU separation failed: {exc}"
                    log("error", err)
                    set_job_status(songhash, "failed",
                                   current_stage="failed", stage_message=err, error=err)
                    return

                # Upload stems returned by Modal
                set_job_status(songhash, "processing",
                               current_stage="uploading_outputs",
                               stage_message="Uploading separated stems to storage")
                upload_errs = []
                for part in PARTS:
                    if part not in stem_bytes:
                        upload_errs.append(f"Stem '{part}' missing from Modal result")
                        continue
                    object_key = f"output/{songhash}-{part}.mp3"
                    try:
                        data = stem_bytes[part]
                        minio_client.put_object(
                            bucket, object_key, BytesIO(data),
                            length=len(data), content_type="audio/mpeg",
                        )
                        log("info", f"Uploaded {object_key}")
                    except S3Error as exc:
                        upload_errs.append(f"Upload failed for {object_key}: {exc}")

                if upload_errs:
                    err = upload_errs[0]
                    set_job_status(songhash, "failed",
                                   current_stage="failed", stage_message=err, error=err)
                    return

                instrumental_ok = False
                if "instrumental" in stem_bytes:
                    try:
                        data = stem_bytes["instrumental"]
                        minio_client.put_object(
                            bucket, f"output/{songhash}-instrumental.mp3",
                            BytesIO(data), length=len(data), content_type="audio/mpeg",
                        )
                        log("info", f"Uploaded output/{songhash}-instrumental.mp3")
                        instrumental_ok = True
                    except S3Error as exc:
                        log("error", f"Instrumental upload failed (non-fatal): {exc}")
                try:
                    redis_client.hset(f"job:{songhash}", "instrumental",
                                      "1" if instrumental_ok else "0")
                except Exception:
                    pass

            else:
                # ── Local CPU path ───────────────────────────────────────────
                if file_size <= LOCAL_THRESHOLD_BYTES:
                    log("info", f"Running locally ({file_size}B ≤ threshold)")
                else:
                    log("info", f"Running locally (Modal disabled)")

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
                log("debug", "Mixing instrumental track")
                instrumental_ok = False
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-i", os.path.join(stems_dir, "bass.mp3"),
                            "-i", os.path.join(stems_dir, "drums.mp3"),
                            "-i", os.path.join(stems_dir, "other.mp3"),
                            "-filter_complex", "amix=inputs=3:duration=first",
                            "-codec:a", "libmp3lame", "-q:a", "2",
                            instrumental_path,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if result.returncode == 0 and os.path.exists(instrumental_path):
                        minio_client.fput_object(
                            bucket, f"output/{songhash}-instrumental.mp3", instrumental_path,
                            content_type="audio/mpeg",
                        )
                        log("info", f"Uploaded output/{songhash}-instrumental.mp3")
                        instrumental_ok = True
                    else:
                        stderr_tail = result.stderr[-600:].strip() if result.stderr else "(no output)"
                        log("error", f"ffmpeg exit {result.returncode}: {stderr_tail}")
                except S3Error as exc:
                    log("error", f"Instrumental upload failed (non-fatal): {exc}")
                except Exception as exc:
                    log("error", f"Instrumental mix error (non-fatal): {exc}")
                try:
                    redis_client.hset(f"job:{songhash}", "instrumental",
                                      "1" if instrumental_ok else "0")
                except Exception:
                    pass

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
    # Start heartbeat before anything else so the API can see the worker immediately
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    hb_thread.start()

    # Clear stale processing entries from a previous crash/restart
    try:
        redis_client.delete("jobs:processing")
        log("info", "Cleared stale jobs:processing set on startup")
    except Exception:
        pass
    modal_status = f"enabled (>{LOCAL_THRESHOLD_BYTES}B)" if USE_MODAL else "disabled"
    log("info", f"Worker started — Modal routing: {modal_status}")
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
