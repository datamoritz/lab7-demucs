#!/usr/bin/env python3
"""Modal serverless GPU function for Demucs stem separation.

Deploy once from your local machine:
    modal deploy worker/modal_demucs.py

The worker then calls it remotely via modal.Function.lookup().
"""

import modal

app = modal.App("demucs-gpu-separation")

# ── Container image ────────────────────────────────────────────────────────────
# Install in strict order: numpy first (pinned 1.x), then torch, then demucs.
# Model weights are lazy-loaded at runtime (first invocation) to avoid fragile
# build-time preload failures.
demucs_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git")
    .pip_install("numpy==1.26.4")
    .pip_install(
        "torch==2.1.2",
        "torchaudio==2.1.2",
        extra_index_url="https://download.pytorch.org/whl/cu118",
    )
    .pip_install(
        "demucs",
        "diffq",
        "julius",
        "lameenc",
        "soundfile",
    )
)


# ── GPU function ───────────────────────────────────────────────────────────────
@app.function(
    image=demucs_image,
    gpu="T4",
    timeout=600,    # 10 min ceiling per job
    memory=8192,    # 8 GB RAM
)
def separate_stems(
    mp3_bytes: bytes,
    songhash: str,
    model: str = "mdx_extra_q",
) -> dict:
    """
    Run Demucs stem separation on a T4 GPU.

    Args:
        mp3_bytes: Raw MP3 file content.
        songhash:  SHA-224 hex digest of the file (used for temp-dir naming).
        model:     Demucs model name (default: mdx_extra_q).

    Returns:
        dict with keys bass / drums / vocals / other (bytes each), and
        optionally instrumental (bytes) if the ffmpeg mix succeeded.

    Raises:
        RuntimeError if Demucs fails or any stem is missing.
    """
    import os
    import subprocess
    import tempfile

    # Lazy-load model weights on first invocation (cached by Modal container reuse)
    from demucs.pretrained import get_model as _get_model
    _get_model(model)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"{songhash}.mp3")
        output_dir = os.path.join(tmpdir, "output")

        with open(input_path, "wb") as f:
            f.write(mp3_bytes)

        cmd = [
            "python3", "-m", "demucs.separate",
            "--out", output_dir,
            "--mp3",
            "--device", "cuda",
            "-n", model,
            input_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=540)
        if result.returncode != 0:
            raise RuntimeError(
                f"Demucs exited {result.returncode}: {result.stderr[-600:]}"
            )

        stems_dir = os.path.join(output_dir, model, songhash)
        parts = ("bass", "drums", "vocals", "other")
        stem_bytes: dict = {}

        for part in parts:
            path = os.path.join(stems_dir, f"{part}.mp3")
            if not os.path.exists(path):
                raise RuntimeError(f"Expected stem not found: {part}.mp3")
            with open(path, "rb") as f:
                stem_bytes[part] = f.read()

        # Mix instrumental (bass + drums + other) — non-fatal if ffmpeg fails
        try:
            instr_path = os.path.join(stems_dir, "instrumental.mp3")
            mix = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", os.path.join(stems_dir, "bass.mp3"),
                    "-i", os.path.join(stems_dir, "drums.mp3"),
                    "-i", os.path.join(stems_dir, "other.mp3"),
                    "-filter_complex", "amix=inputs=3:duration=first",
                    "-codec:a", "libmp3lame", "-q:a", "2",
                    instr_path,
                ],
                capture_output=True,
                text=True,
            )
            if mix.returncode == 0 and os.path.exists(instr_path):
                with open(instr_path, "rb") as f:
                    stem_bytes["instrumental"] = f.read()
        except Exception:
            pass

        return stem_bytes
