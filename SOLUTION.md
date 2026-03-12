# Music Separation as a Service (MSaaS) — Solution Documentation

Lab 7, CSCI 4253/5253 Datacenters
Docker Hub user: `mokn4989`

---

## Architecture

```
Client
  │
  │  POST /apiv1/separate  (base64 MP3)
  ▼
REST Service (FastAPI, port 5001)
  │  → stores MP3 in MinIO  demucs-bucket/queue/<hash>.mp3
  │  → lpush job JSON       Redis key "toWorker"
  │  → returns              { hash, reason }
  │
  ├── GET /apiv1/queue      reads Redis "toWorker" list
  ├── GET /apiv1/track/{hash}/{track}   streams MP3 from MinIO output/
  └── GET /apiv1/remove/{hash}/{track}  deletes object from MinIO output/

Redis ("toWorker" list)
  │  blpop
  ▼
Worker
  │  → downloads MP3 from MinIO queue/
  │  → runs: python3 -m demucs.separate --out ... --mp3 -n mdx_extra_q
  │  → uploads 4 tracks to MinIO output/<hash>-{bass,drums,vocals,other}.mp3
  └── → fires optional callback (best-effort)

MinIO (demucs-bucket)
  queue/<hash>.mp3              ← input, written by REST
  output/<hash>-bass.mp3        ← output, written by worker
  output/<hash>-drums.mp3
  output/<hash>-vocals.mp3
  output/<hash>-other.mp3

Redis ("logging" list)
  ← REST and worker both lpush log entries
  ← logs service blpops and prints to stdout
```

---

## Repository Structure

```
lab7-demucs/
├── data/                        # test MP3 files (short-*.mp3 for dev)
├── images/                      # lab diagram screenshots
├── logs/
│   ├── Dockerfile               # pre-built; uses dirkcgrunwald/demucs-logs:v9
│   ├── logs-deployment.yaml
│   └── logs.py
├── minio/
│   ├── minio-config.yaml        # Helm values for in-cluster MinIO
│   └── minio-external-service.yaml  # ExternalName svc: minio → minio-ns
├── redis/
│   ├── redis-deployment.yaml
│   └── redis-service.yaml
├── rest/
│   ├── Dockerfile-rest
│   ├── requirements-rest.txt
│   ├── rest-server.py           # FastAPI app (all endpoints)
│   ├── rest-deployment.yaml
│   ├── rest-service.yaml
│   └── rest-ingress.yaml
├── worker/
│   ├── Dockerfile               # extends xserrat/facebook-demucs
│   ├── requirements-worker.txt
│   ├── worker-server.py         # blocking worker loop
│   └── worker-deployment.yaml
├── short-sample-request.py      # test client for short clips
├── sample-requests.py           # test client for full songs
├── deploy-all.sh                # one-shot kubectl apply
└── deploy-local-dev.sh          # deploy + port-forward for local dev
```

---

## Environment Variables

### REST service (`rest-server.py`)

| Variable | Default (local) | In-cluster value |
|---|---|---|
| `REDIS_HOST` | `localhost` | `redis` |
| `REDIS_PORT` | `6379` | `6379` |
| `MINIO_HOST` | `localhost` | `minio` |
| `MINIO_PORT` | `9000` | `9000` |
| `MINIO_ACCESS_KEY` | `minio` | `rootuser` |
| `MINIO_SECRET_KEY` | `minio123` | `rootpass123` |
| `MINIO_BUCKET` | `demucs-bucket` | `demucs-bucket` |
| `MINIO_SECURE` | `false` | `false` |
| `REST_PORT` | `5001` | `5001` |

### Worker (`worker-server.py`)

| Variable | Default (local) | In-cluster value |
|---|---|---|
| `REDIS_HOST` | `localhost` | `redis` |
| `REDIS_PORT` | `6379` | `6379` |
| `MINIO_HOST` | `localhost` | `minio` |
| `MINIO_PORT` | `9000` | `9000` |
| `MINIO_ACCESS_KEY` | `minio` | `rootuser` |
| `MINIO_SECRET_KEY` | `minio123` | `rootpass123` |
| `MINIO_SECURE` | `false` | `false` |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/apiv1/separate` | Enqueue a song for separation |
| `GET` | `/apiv1/queue` | List hashes currently queued in Redis |
| `GET` | `/apiv1/track/{hash}/{track}` | Download a separated track as MP3 |
| `GET` | `/apiv1/remove/{hash}/{track}` | Delete a separated track from MinIO |

`{track}` must be one of: `bass`, `drums`, `vocals`, `other`

### POST /apiv1/separate — request body
```json
{
  "mp3": "<base64-encoded MP3 bytes>",
  "model": "mdx_extra_q",
  "callback": null
}
```

### POST /apiv1/separate — response
```json
{
  "hash": "e5f013b19e55e5b7354eaf303c86b2b93f1d2847d8c9fa58c77a0add",
  "reason": "Song enqueued for separation"
}
```

### Redis job message (pushed to `toWorker`)
```json
{
  "hash": "<sha224hex>",
  "model": "mdx_extra_q",
  "callback": null,
  "bucket": "demucs-bucket",
  "key": "queue/<hash>.mp3"
}
```

---

## Local Development (no Docker)

### Prerequisites

```bash
brew install ffmpeg redis
# Start local MinIO (Docker):
docker run -d --name minio-local \
  -e MINIO_ROOT_USER=minio -e MINIO_ROOT_PASSWORD=minio123 \
  -p 9000:9000 -p 9001:9001 \
  minio/minio server /data --console-address ":9001"
redis-server &
```

### Install deps

```bash
cd rest   && pip install -r requirements-rest.txt
cd worker && pip install -r requirements-worker.txt
```

### Run services

```bash
# Terminal 1 — REST
cd rest
python3 rest-server.py
# → Uvicorn running on http://0.0.0.0:5001

# Terminal 2 — Worker
cd worker
python3 worker-server.py
# → Worker started, waiting for jobs on 'toWorker'
```

### Test

```bash
cd lab7-demucs
python3 short-sample-request.py          # uses REST=localhost:5001 by default

curl http://localhost:5001/apiv1/queue

HASH=<hash from response>
curl -s http://localhost:5001/apiv1/track/$HASH/vocals -o /tmp/vocals.mp3
file /tmp/vocals.mp3

curl -s http://localhost:5001/apiv1/remove/$HASH/bass
```

---

## Local Development (Docker containers, shared network)

### Build images

```bash
cd lab7-demucs/rest
docker build -f Dockerfile-rest -t demucs-rest .

cd lab7-demucs/worker
docker build -f Dockerfile -t demucs-worker .
```

### Run

```bash
docker network create lab7

docker run -d --name lab7-redis --network lab7 redis

docker run -d --name lab7-minio --network lab7 \
  -e MINIO_ROOT_USER=minio -e MINIO_ROOT_PASSWORD=minio123 \
  -p 9000:9000 -p 9001:9001 \
  minio/minio server /data --console-address ":9001"

docker run -d --name lab7-rest --network lab7 \
  -e REDIS_HOST=lab7-redis \
  -e MINIO_HOST=lab7-minio \
  -e MINIO_ACCESS_KEY=minio \
  -e MINIO_SECRET_KEY=minio123 \
  -e MINIO_SECURE=false \
  -p 5001:5001 \
  demucs-rest

docker run -d --name lab7-worker --network lab7 \
  -e REDIS_HOST=lab7-redis \
  -e MINIO_HOST=lab7-minio \
  -e MINIO_ACCESS_KEY=minio \
  -e MINIO_SECRET_KEY=minio123 \
  -e MINIO_SECURE=false \
  demucs-worker
```

### Test

```bash
python3 short-sample-request.py          # REST defaults to localhost:5001
docker logs -f lab7-worker
```

### Teardown

```bash
docker rm -f lab7-rest lab7-worker lab7-redis lab7-minio
docker network rm lab7
```

---

## Kubernetes Deployment

### Prerequisites

- Docker Desktop with Kubernetes enabled
- Helm installed

**1. Deploy MinIO via Helm into `minio-ns`:**
```bash
kubectl create namespace minio-ns
helm repo add minio https://charts.min.io/
helm install minio-proj minio/minio -n minio-ns -f minio/minio-config.yaml
```

**2. Install the nginx ingress controller** (required — the cluster has none by default):
```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml
kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx
```
Without this step `kubectl get ingress` will show no ADDRESS and all HTTP requests will fail.

**3. Create the MinIO credentials secret in the default namespace** (required before REST and worker pods can start):
```bash
kubectl create secret generic minio-credentials \
  --from-literal=access-key=rootuser \
  --from-literal=secret-key=rootpass123
```
The values must match `minio/minio-config.yaml` (`auth.rootUser` / `auth.rootPassword`).
Both `rest-deployment.yaml` and `worker-deployment.yaml` reference this secret via `secretKeyRef`.

### Push images

```bash
DOCKERUSER=mokn4989

docker tag demucs-rest   $DOCKERUSER/demucs-rest:latest   && docker push $DOCKERUSER/demucs-rest:latest
docker tag demucs-worker $DOCKERUSER/demucs-worker:latest && docker push $DOCKERUSER/demucs-worker:latest
```

### Deploy (in order)

```bash
# 1. Redis
kubectl apply -f redis/redis-deployment.yaml
kubectl apply -f redis/redis-service.yaml
kubectl rollout status deployment/redis

# 2. MinIO alias service (MinIO itself is in minio-ns via Helm)
kubectl apply -f minio/minio-external-service.yaml

# 3. Logs
kubectl apply -f logs/logs-deployment.yaml
kubectl rollout status deployment/logs

# 4. REST
kubectl apply -f rest/rest-deployment.yaml
kubectl apply -f rest/rest-service.yaml
kubectl rollout status deployment/rest

# 5. Ingress
kubectl apply -f rest/rest-ingress.yaml

# 6. Worker (last — needs Redis and MinIO ready)
kubectl apply -f worker/worker-deployment.yaml
kubectl rollout status deployment/worker
```

Or all at once:
```bash
./deploy-all.sh
```

### Verify

```bash
# All pods running
kubectl get pods

# Ingress has an address
kubectl get ingress frontend-ingress

# Health check (Docker Desktop exposes ingress on localhost:80)
curl http://localhost/

# End-to-end test
REST=localhost python3 short-sample-request.py

# Watch worker logs
kubectl logs -f deployment/worker

# Watch system logs
kubectl logs -f deployment/logs

# Download a track
HASH=<hash from separate response>
curl -s http://localhost/apiv1/track/$HASH/vocals -o /tmp/vocals.mp3
```

---

## MinIO Credentials Reference

| Context | Access Key | Secret Key |
|---|---|---|
| Local dev / Docker | `minio` | `minio123` |
| In-cluster (Helm) | `rootuser` | `rootpass123` |

The Helm values in `minio/minio-config.yaml` are the authoritative source for in-cluster credentials. Kubernetes deployment YAMLs pass these explicitly via env vars, overriding the code defaults.

---

## Known Issues and Fixes Applied

| Issue | Fix |
|---|---|
| FastAPI startup hang | Removed blocking MinIO call from startup; `ensure_bucket()` called lazily on first `POST /apiv1/separate` |
| MinIO `BadStatusLine` / TLS error in containers | Added `MINIO_SECURE` env var; defaults `false`; was previously hardcoded literal |
| Worker container exits immediately (code 0) | Base image `xserrat/facebook-demucs` defines its own `ENTRYPOINT`; overridden with `ENTRYPOINT ["python3", "worker-server.py"]` in worker Dockerfile |
| Ingress ADDRESS empty, no controller | Installed `ingress-nginx` controller; replaced deprecated `kubernetes.io/ingress.class` annotation with `spec.ingressClassName: nginx` |
| Stale worker pods during rollout | Added `strategy: Recreate` to worker deployment; prevents two worker pods competing on the same Redis queue during rolling update |
| MinIO credentials hardcoded in YAML | Replaced literal values with `secretKeyRef` referencing `minio-credentials` secret; secret must be created before deploying |
| `torchcodec` import error in worker | Added `diffq` and `torchcodec` to `requirements-worker.txt` and worker Dockerfile |
| Hardcoded `localhost:5000` callback in test script | Made callback URL configurable via `CALLBACK_URL` env var; disabled by default |
