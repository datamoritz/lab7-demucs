# How to Separate Your Own MP3

This guide shows how to submit any MP3 file to the MSaaS pipeline and download the four separated tracks (bass, drums, vocals, other).

---

## Prerequisites

- REST service is running (locally on port 5001, or via ingress on port 80)
- Worker service is running and connected to the same Redis and MinIO
- Python 3 with `requests` installed: `pip install requests`

Set the `REST` variable once for your session:

```bash
# Local dev
export REST=localhost:5001

# Kubernetes via ingress
export REST=localhost
```

---

## Option A — Python one-liner (simplest)

Save this as `separate_song.py` and run it with your MP3 file path as argument:

```python
#!/usr/bin/env python3
"""Submit an MP3 file and download all four separated tracks."""

import base64
import json
import os
import sys
import requests

REST = os.getenv("REST", "localhost:5001")

if len(sys.argv) < 2:
    print("Usage: python3 separate_song.py <path/to/song.mp3>")
    sys.exit(1)

mp3_path = sys.argv[1]

# 1. Read and encode the file
with open(mp3_path, "rb") as f:
    mp3_b64 = base64.b64encode(f.read()).decode("utf-8")

print(f"Submitting {mp3_path} ({len(mp3_b64)} base64 chars) ...")

# 2. Submit to REST service
resp = requests.post(
    f"http://{REST}/apiv1/separate",
    json={"mp3": mp3_b64, "model": "mdx_extra_q", "callback": None},
)
resp.raise_for_status()
data = resp.json()
song_hash = data["hash"]
print(f"Hash: {song_hash}")
print(f"Reason: {data['reason']}")

# 3. Wait for the worker to finish, then download the four tracks
print("\nWaiting for separation to complete ...")
print("(Watch worker logs in another terminal — this can take a few minutes)")
input("Press Enter once the worker has finished processing ...")

out_dir = os.path.splitext(mp3_path)[0] + "_separated"
os.makedirs(out_dir, exist_ok=True)

for track in ("bass", "drums", "vocals", "other"):
    url = f"http://{REST}/apiv1/track/{song_hash}/{track}"
    r = requests.get(url, stream=True)
    if r.status_code == 200:
        out_path = os.path.join(out_dir, f"{track}.mp3")
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"  Saved {out_path}")
    else:
        print(f"  {track}: not ready yet (HTTP {r.status_code})")

print(f"\nDone. Tracks saved to: {out_dir}/")
```

**Run it:**

```bash
python3 separate_song.py /path/to/your/song.mp3
```

---

## Option B — curl commands step by step

### Step 1 — Encode and submit

```bash
MP3_FILE="/path/to/your/song.mp3"
MP3_B64=$(base64 < "$MP3_FILE")

HASH=$(curl -s http://$REST/apiv1/separate \
  -H "Content-Type: application/json" \
  -d "{\"mp3\": \"$MP3_B64\", \"model\": \"mdx_extra_q\", \"callback\": null}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['hash'])")

echo "Song hash: $HASH"
```

> **Note:** Large MP3 files produce a large base64 string. For files over ~10 MB
> the shell variable may be slow to assign. Use the Python script above instead.

### Step 2 — Check queue (optional)

```bash
curl -s http://$REST/apiv1/queue | python3 -m json.tool
```

Your hash should appear. Once the worker processes it, the queue will be empty.

### Step 3 — Watch the worker finish

```bash
# Local Python
# (worker terminal prints progress)

# Kubernetes
kubectl logs -f deployment/worker

# Docker
docker logs -f lab7-worker
```

Wait until you see:
```
<hostname>.worker.info:Done <your-hash>
```

### Step 4 — Download the four tracks

```bash
mkdir -p separated_tracks

for TRACK in bass drums vocals other; do
  curl -s http://$REST/apiv1/track/$HASH/$TRACK \
    -o separated_tracks/${TRACK}.mp3
  echo "Downloaded $TRACK.mp3"
done
```

### Step 5 — Play and verify

```bash
# macOS
open separated_tracks/vocals.mp3

# Linux
mpv separated_tracks/vocals.mp3
# or
ffplay separated_tracks/vocals.mp3
```

### Step 6 — Clean up (optional)

```bash
for TRACK in bass drums vocals other; do
  curl -s http://$REST/apiv1/remove/$HASH/$TRACK
done
```

---

## Tips

**File size:** Demucs works best with standard MP3s (128–320 kbps, stereo).
Files over ~20 MB will hit the 16 MB ingress body limit when sent through the
nginx ingress. For large files either:
- Use `short-sample-request.py` as a template and raise the limit in
  `rest-ingress.yaml` (`proxy-body-size: 32m`), or
- Submit directly to the REST service via port-forward, bypassing the ingress:
  ```bash
  kubectl port-forward service/rest-service 5001:5001
  export REST=localhost:5001
  ```

**Processing time:** Separation on CPU takes roughly 2–5× the song duration
(e.g. a 3-minute song takes 6–15 minutes). The worker logs show progress.

**Model:** The default model is `mdx_extra_q` (high quality). You can change it
by passing `"model": "htdemucs"` in the request body for faster processing.

**Multiple songs:** Submit them all first, then download once the worker has
processed all of them — the queue handles them one at a time.
