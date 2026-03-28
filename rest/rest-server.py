#!/usr/bin/env python3
"""REST front-end for Music-Separation-as-a-Service (MSaaS)."""

import base64
import hashlib
import json
import os
import socket
import sys
import time
from io import BytesIO
from typing import Any, Optional

import redis
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from minio import Minio
from minio.error import S3Error
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (environment variables with local-dev defaults)
# ---------------------------------------------------------------------------
REDIS_HOST      = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT      = int(os.getenv("REDIS_PORT", 6379))
MINIO_HOST      = os.getenv("MINIO_HOST", "localhost")
MINIO_PORT      = os.getenv("MINIO_PORT", "9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET    = os.getenv("MINIO_SECRET_KEY", "minio123")
MINIO_BUCKET    = os.getenv("MINIO_BUCKET", "demucs-bucket")
MINIO_SECURE    = os.getenv("MINIO_SECURE", "false").lower() == "true"
PORT            = int(os.getenv("REST_PORT", 5001))

HOSTNAME    = socket.gethostname()

JOB_TTL     = 86400  # seconds — job status keys expire after 24 h
SAMPLES_DIR = os.getenv("SAMPLES_DIR", "/srv/data")

HEARTBEAT_KEY        = "worker:heartbeat"
HEARTBEAT_STALE_SECS = 90   # 3 missed heartbeats → worker considered unhealthy

# Known sample files — key is the public sample ID, value is the filename in SAMPLES_DIR
SAMPLE_FILES = {
    "short": "short-dreams.mp3",
    "long":  "Opalite.mp3",
}

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
# App
# ---------------------------------------------------------------------------

def log(level: str, msg: str) -> None:
    entry = f"{HOSTNAME}.rest.{level}:{msg}"
    try:
        redis_client.lpush("logging", entry)
    except Exception:
        print(entry, file=sys.stderr)


def ensure_bucket() -> None:
    try:
        if not minio_client.bucket_exists(MINIO_BUCKET):
            minio_client.make_bucket(MINIO_BUCKET)
            log("info", f"Created MinIO bucket '{MINIO_BUCKET}'")
    except S3Error as exc:
        log("error", f"MinIO bucket check failed: {exc}")


app = FastAPI(title="MSaaS REST Service")

# ---------------------------------------------------------------------------
# CORS — required for browser-based frontends (e.g. Vercel)
# Restrict allow_origins to your actual domain in production:
#   allow_origins=["https://your-app.vercel.app"]
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "HEAD"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def health():
    return {"status": "Music Separation Server running"}


def _read_worker_heartbeat() -> dict:
    """Read worker:heartbeat from Redis and return a normalised dict."""
    now = int(time.time())
    result: dict = {
        "worker_seen":         False,
        "worker_stale":        True,
        "worker_redis_ok":     False,
        "worker_minio_ok":     False,
        "worker_last_seen_ago": None,
        "worker_hostname":     None,
        "worker_error":        None,
    }
    try:
        raw = redis_client.hgetall(HEARTBEAT_KEY)
        if raw:
            hb  = {k.decode(): v.decode() for k, v in raw.items()}
            age = now - int(hb.get("last_seen", 0))
            result["worker_seen"]          = True
            result["worker_stale"]         = age > HEARTBEAT_STALE_SECS
            result["worker_last_seen_ago"] = age
            result["worker_hostname"]      = hb.get("hostname")
            result["worker_redis_ok"]      = hb.get("redis_ok") == "1"
            result["worker_minio_ok"]      = hb.get("minio_ok") == "1"
            err = hb.get("redis_error") or hb.get("minio_error") or None
            result["worker_error"]         = err if err else None
    except Exception:
        pass
    return result


@app.get("/apiv1/health")
def detailed_health():
    """Detailed health check used by the frontend status panel."""
    status: dict = {
        "api":   True,
        "redis": False,
        "minio": False,
        "k8s":   False,
        # Worker fields — populated below
        "workers":              0,
        "worker_seen":          False,
        "worker_stale":         True,
        "worker_redis_ok":      False,
        "worker_minio_ok":      False,
        "worker_last_seen_ago": None,
        "worker_error":         None,
        "queue_stalled":        False,
    }
    try:
        redis_client.ping()
        status["redis"] = True
    except Exception:
        pass
    try:
        minio_client.bucket_exists(MINIO_BUCKET)
        status["minio"] = True
    except Exception:
        pass

    if status["redis"]:
        hb = _read_worker_heartbeat()
        status.update(hb)
        if hb["worker_seen"] and not hb["worker_stale"] and hb["worker_redis_ok"]:
            status["workers"] = 1
        # Queue stall: jobs waiting but worker cannot dequeue them
        try:
            queue_len = int(redis_client.llen("toWorker"))
            worker_healthy = (
                hb["worker_seen"]
                and not hb["worker_stale"]
                and hb["worker_redis_ok"]
            )
            if queue_len > 0 and not worker_healthy:
                status["queue_stalled"] = True
        except Exception:
            pass

    return status


@app.get("/apiv1/worker-health")
def worker_health():
    """Detailed worker connectivity report — polled separately by the frontend."""
    result = _read_worker_heartbeat()
    # Rename keys to drop the "worker_" prefix for this dedicated endpoint
    return {
        "seen":          result["worker_seen"],
        "stale":         result["worker_stale"],
        "redis_ok":      result["worker_redis_ok"],
        "minio_ok":      result["worker_minio_ok"],
        "last_seen_ago": result["worker_last_seen_ago"],
        "hostname":      result["worker_hostname"],
        "error":         result["worker_error"],
    }


class SeparateRequest(BaseModel):
    mp3: str                      # base64-encoded MP3 bytes
    model: Optional[str] = "mdx_extra_q"
    callback: Optional[Any] = None


@app.post("/apiv1/separate")
def separate(req: SeparateRequest):
    # 1. Decode base64 → raw bytes
    try:
        mp3_bytes = base64.b64decode(req.mp3)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {exc}")

    # 2. Hash the raw bytes (SHA-224, matching lab sample output length)
    songhash = hashlib.sha224(mp3_bytes).hexdigest()
    object_key = f"queue/{songhash}.mp3"

    # 3. Upload to MinIO (ensure bucket exists on first use)
    ensure_bucket()
    try:
        minio_client.put_object(
            MINIO_BUCKET,
            object_key,
            BytesIO(mp3_bytes),
            length=len(mp3_bytes),
            content_type="audio/mpeg",
        )
    except S3Error as exc:
        log("error", f"MinIO upload failed for {songhash}: {exc}")
        raise HTTPException(status_code=500, detail="Object storage error")

    # 4. Write job status BEFORE enqueuing so the worker never picks up a job
    #    whose status key doesn't exist yet.
    try:
        redis_client.hset(f"job:{songhash}", mapping={
            "status":        "queued",
            "current_stage": "queued",
            "stage_message": "Waiting in queue",
            "error":         "",
        })
        redis_client.expire(f"job:{songhash}", JOB_TTL)
    except Exception as exc:
        log("error", f"Failed to write job status for {songhash}: {exc}")

    # 5. Enqueue job to Redis
    job = {
        "hash": songhash,
        "model": req.model or "mdx_extra_q",
        "callback": req.callback,
        "bucket": MINIO_BUCKET,
        "key": object_key,
    }
    redis_client.lpush("toWorker", json.dumps(job))
    log("info", f"Enqueued {songhash} ({len(mp3_bytes)} bytes)")

    return {"hash": songhash, "reason": "Song enqueued for separation"}


@app.get("/apiv1/queue")
def queue():
    try:
        raw_items = redis_client.lrange("toWorker", 0, -1)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    hashes = []
    for item in raw_items:
        try:
            job = json.loads(item.decode("utf-8"))
            hashes.append(job.get("hash", item.decode("utf-8")))
        except Exception:
            hashes.append(item.decode("utf-8"))

    try:
        jobs_processing = int(redis_client.scard("jobs:processing"))
    except Exception:
        jobs_processing = 0

    return {
        "queue":           hashes,
        "jobs_waiting":    len(hashes),
        "jobs_processing": jobs_processing,
    }


@app.get("/apiv1/status/{songhash}")
def job_status(songhash: str):
    """Return the current status of a specific job."""
    try:
        raw = redis_client.hgetall(f"job:{songhash}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not raw:
        raise HTTPException(status_code=404, detail="Job not found")

    # hgetall returns bytes — decode
    record = {k.decode(): v.decode() for k, v in raw.items()}

    try:
        jobs_waiting    = int(redis_client.llen("toWorker"))
        jobs_processing = int(redis_client.scard("jobs:processing"))
    except Exception:
        jobs_waiting    = 0
        jobs_processing = 0

    return {
        "hash":            songhash,
        "status":          record.get("status", "unknown"),
        "current_stage":   record.get("current_stage", ""),
        "stage_message":   record.get("stage_message", ""),
        "error":           record.get("error") or None,
        "instrumental":    record.get("instrumental", "0") == "1",
        "jobs_waiting":    jobs_waiting,
        "jobs_processing": jobs_processing,
    }


@app.post("/apiv1/sample/{sample_id}")
def run_sample(sample_id: str):
    """Enqueue a pre-loaded sample file without requiring a client upload."""
    if sample_id not in SAMPLE_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown sample '{sample_id}'. Valid: {sorted(SAMPLE_FILES)}")

    path = os.path.join(SAMPLES_DIR, SAMPLE_FILES[sample_id])
    try:
        with open(path, "rb") as f:
            mp3_bytes = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Sample file not found on server: {SAMPLE_FILES[sample_id]}")

    songhash   = hashlib.sha224(mp3_bytes).hexdigest()
    object_key = f"queue/{songhash}.mp3"

    ensure_bucket()
    try:
        minio_client.put_object(
            MINIO_BUCKET, object_key, BytesIO(mp3_bytes),
            length=len(mp3_bytes), content_type="audio/mpeg",
        )
    except S3Error as exc:
        log("error", f"MinIO upload failed for sample '{sample_id}': {exc}")
        raise HTTPException(status_code=500, detail="Object storage error")

    try:
        redis_client.hset(f"job:{songhash}", mapping={
            "status":        "queued",
            "current_stage": "queued",
            "stage_message": "Waiting in queue",
            "error":         "",
        })
        redis_client.expire(f"job:{songhash}", JOB_TTL)
    except Exception as exc:
        log("error", f"Failed to write job status for sample '{sample_id}': {exc}")

    job = {
        "hash":     songhash,
        "model":    "mdx_extra_q",
        "callback": None,
        "bucket":   MINIO_BUCKET,
        "key":      object_key,
    }
    redis_client.lpush("toWorker", json.dumps(job))
    log("info", f"Sample '{sample_id}' enqueued as {songhash} ({len(mp3_bytes)} bytes)")

    return {"hash": songhash, "reason": f"Sample '{sample_id}' enqueued for separation"}


VALID_TRACKS = {"bass", "drums", "vocals", "other", "instrumental"}


@app.get("/apiv1/track/{songhash}/{track}")
def get_track(songhash: str, track: str):
    if track not in VALID_TRACKS:
        raise HTTPException(status_code=400, detail=f"Invalid track '{track}'. Must be one of {sorted(VALID_TRACKS)}")

    object_key = f"output/{songhash}-{track}.mp3"
    try:
        response = minio_client.get_object(MINIO_BUCKET, object_key)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            raise HTTPException(status_code=404, detail="Track not found")
        log("error", f"MinIO get failed for {object_key}: {exc}")
        raise HTTPException(status_code=500, detail="Object storage error")

    log("info", f"Serving {object_key}")
    return StreamingResponse(response, media_type="audio/mpeg")


@app.get("/apiv1/remove/{songhash}/{track}")
def remove_track(songhash: str, track: str):
    if track not in VALID_TRACKS:
        raise HTTPException(status_code=400, detail=f"Invalid track '{track}'. Must be one of {sorted(VALID_TRACKS)}")

    object_key = f"output/{songhash}-{track}.mp3"
    try:
        minio_client.remove_object(MINIO_BUCKET, object_key)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            raise HTTPException(status_code=404, detail="Track not found")
        log("error", f"MinIO remove failed for {object_key}: {exc}")
        raise HTTPException(status_code=500, detail="Object storage error")

    log("info", f"Removed {object_key}")
    return {"result": "removed", "key": object_key}


# ---------------------------------------------------------------------------
# Entry point for local dev: python3 rest-server.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
