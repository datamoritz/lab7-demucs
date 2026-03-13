#!/usr/bin/env python3
"""REST front-end for Music-Separation-as-a-Service (MSaaS)."""

import base64
import hashlib
import json
import os
import socket
import sys
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

HOSTNAME = socket.gethostname()

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


@app.get("/apiv1/health")
def detailed_health():
    """Detailed health check used by the frontend status panel."""
    status = {"api": True, "redis": False, "minio": False, "workers": 1, "k8s": False}
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
    return status


class SeparateRequest(BaseModel):
    mp3: str                      # base64-encoded MP3 bytes
    model: Optional[str] = "htdemucs"
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

    # 4. Enqueue job to Redis
    job = {
        "hash": songhash,
        "model": req.model or "htdemucs",
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

    return {"queue": hashes}


VALID_TRACKS = {"bass", "drums", "vocals", "other"}


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
