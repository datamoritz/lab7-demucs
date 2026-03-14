// ─── Config ──────────────────────────────────────────────────────────────────
const API_BASE = "https://5.78.134.84.sslip.io";

const QUEUE_POLL_INTERVAL  = 5000;
const STATUS_POLL_INTERVAL = 3000;
const JOB_TIMEOUT_MS       = 10 * 60 * 1000;
const SYS_POLL_INTERVAL    = 10000;

// ─── Stem definitions ────────────────────────────────────────────────────────
const STEMS = [
  { key: "vocals", label: "Vocals", icon: "🎤", bg: "bg-violet-50",  text: "text-violet-700",  border: "border-violet-200"  },
  { key: "drums",  label: "Drums",  icon: "🥁", bg: "bg-amber-50",   text: "text-amber-700",   border: "border-amber-200"   },
  { key: "bass",   label: "Bass",   icon: "🎸", bg: "bg-sky-50",     text: "text-sky-700",     border: "border-sky-200"     },
  { key: "other",  label: "Other",  icon: "🎹", bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200" },
];

// ─── Log lines per stage ─────────────────────────────────────────────────────
const LOG_LINES = {
  uploading:           "[REST]    Encoding file to base64...",
  submitted:           "[REST]    Job submitted → hash received",
  queued:              "[QUEUE]   Added to Redis list: toWorker",
  "worker started":    "[K8S]     Worker pod scheduled",
  "loading model":     "[WORKER]  Loading Demucs htdemucs model",
  "processing audio":  "[PROCESS] Running stem separation",
  "separating stems":  "[PROCESS] Separating: vocals, drums, bass, other",
  "uploading results": "[STORAGE] Uploading stems → demucs-bucket/output/",
  finished:            "[DONE]    Separation complete — stems ready",
};

// ─── Pipeline step mapping ────────────────────────────────────────────────────
// Maps app state → pipeline step index (0–5)
const STATE_TO_STEP = {
  uploading:           0,
  queued:              1,
  "worker started":    2,
  "loading model":     2,
  "processing audio":  3,
  "separating stems":  3,
  "uploading results": 4,
  finished:            5,
};

// ─── App state ───────────────────────────────────────────────────────────────
let selectedFile    = null;
let currentHash     = null;
let startTime       = null;
let elapsedTimer    = null;
let statusPoller    = null;
let queuePoller     = null;
let activePipeStep  = -1;

// ─── DOM refs ────────────────────────────────────────────────────────────────
const dropZone        = document.getElementById("drop-zone");
const fileInput       = document.getElementById("file-input");
const fileNameEl      = document.getElementById("file-name");
const uploadBtn       = document.getElementById("upload-btn");
const jobSection      = document.getElementById("job-section");
const downloadSection = document.getElementById("download-section");
const downloadGrid    = document.getElementById("download-grid");
const jobIdEl         = document.getElementById("job-id");
const statusText      = document.getElementById("status-text");
const statusSpinner   = document.getElementById("status-spinner");
const elapsedEl       = document.getElementById("elapsed-time");
const finalTimeEl     = document.getElementById("final-time");
const queuePosEl      = document.getElementById("queue-pos");
const queueCountEl    = document.getElementById("queue-count");
const queueBadge      = document.getElementById("queue-badge");
const logPanel        = document.getElementById("log-panel");
const pipelineSteps   = document.querySelectorAll("#pipeline-steps .pipeline-step");
const pipelineFill    = document.getElementById("pipeline-fill");

// ─── System status ────────────────────────────────────────────────────────────
function setSysStatus(id, state, label) {
  const el  = document.getElementById(id);
  const dot = el.querySelector(".sys-dot");
  const lbl = el.querySelector(".sys-label");
  dot.classList.remove("dot-ok", "dot-err", "dot-idle", "dot-pulse");
  lbl.classList.remove("text-emerald-600", "text-red-500", "text-slate-400");
  if (state === "ok")  { dot.classList.add("dot-ok");   lbl.classList.add("text-emerald-600"); }
  if (state === "err") { dot.classList.add("dot-err");  lbl.classList.add("text-red-500");     }
  if (state === "idle"){ dot.classList.add("dot-idle"); lbl.classList.add("text-slate-400");   }
  lbl.textContent = label;
}

async function checkSystemStatus() {
  // Try a dedicated health endpoint first; fall back to queue endpoint
  let apiOk = false;
  let redisOk = true;
  let workerCount = null;
  let minioOk = false;
  try {
    const res = await fetch(`${API_BASE}/apiv1/health`, { signal: AbortSignal.timeout(4000) });
    if (res.ok) {
      const data = await res.json();
      apiOk       = true;
      redisOk     = data.redis   ?? true;
      minioOk     = data.minio   ?? true;
      workerCount = data.workers ?? null;
    }
  } catch (_) {
    // Health endpoint not available — infer from queue
    try {
      const res = await fetch(`${API_BASE}/apiv1/queue`, { signal: AbortSignal.timeout(4000) });
      if (res.ok) {
        apiOk   = true;
        redisOk = true;
        minioOk = true;
      }
    } catch (_) { /* unreachable */ }
  }

  if (apiOk) {
    setSysStatus("sys-api",     "ok",  "running");
    setSysStatus("sys-redis",   redisOk ? "ok" : "err",  redisOk ? "connected" : "unreachable");
    setSysStatus("sys-minio",   minioOk ? "ok" : "err", minioOk ? "connected" : "unreachable");
    setSysStatus("sys-k8s",     "ok",  "Docker Compose");
    setSysStatus("sys-workers", "ok",
      workerCount !== null ? `${workerCount} active` : "1 active");
  } else {
    setSysStatus("sys-api",     "err",  "unreachable");
    setSysStatus("sys-redis",   "idle", "unknown");
    setSysStatus("sys-minio",   "idle", "unknown");
    setSysStatus("sys-k8s",     "idle", "unknown");
    setSysStatus("sys-workers", "idle", "unknown");
  }
}

// Run on load and every 10 s
checkSystemStatus();
setInterval(checkSystemStatus, SYS_POLL_INTERVAL);

// ─── Pipeline stepper ─────────────────────────────────────────────────────────
function setPipelineStep(activeIndex, isDone) {
  const total = pipelineSteps.length - 1; // 5 gaps between 6 steps
  pipelineSteps.forEach((step, i) => {
    const dot     = step.querySelector(".pipeline-step-dot");
    const check   = step.querySelector(".step-check");
    const spinner = step.querySelector(".step-spinner");

    step.classList.remove("step-done", "step-active", "step-pending");
    check.classList.add("hidden");
    spinner.classList.add("hidden");
    dot.style.backgroundColor = "";
    dot.style.borderColor = "";

    if (i < activeIndex) {
      // completed
      step.classList.add("step-done");
      dot.style.backgroundColor = "#6366f1";
      dot.style.borderColor = "#6366f1";
      check.classList.remove("hidden");
    } else if (i === activeIndex) {
      step.classList.add("step-active");
      if (isDone) {
        dot.style.backgroundColor = "#6366f1";
        dot.style.borderColor = "#6366f1";
        check.classList.remove("hidden");
      } else {
        spinner.classList.remove("hidden");
      }
    } else {
      step.classList.add("step-pending");
    }
  });

  // Update progress bar fill
  const pct = activeIndex === 0 ? 0 : Math.round((activeIndex / total) * 100);
  pipelineFill.style.width = `${pct}%`;
  activePipeStep = activeIndex;
}

function resetPipeline() {
  pipelineSteps.forEach(step => {
    const dot     = step.querySelector(".pipeline-step-dot");
    const check   = step.querySelector(".step-check");
    const spinner = step.querySelector(".step-spinner");
    step.classList.remove("step-done", "step-active");
    step.classList.add("step-pending");
    check.classList.add("hidden");
    spinner.classList.add("hidden");
    dot.style.backgroundColor = "";
    dot.style.borderColor = "";
  });
  pipelineFill.style.width = "0%";
  activePipeStep = -1;
}

// ─── Drag & drop / file selection ────────────────────────────────────────────
dropZone.addEventListener("click",    () => fileInput.click());
dropZone.addEventListener("keydown",  e => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave",() => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop",     e => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) handleFileSelect(file);
});
fileInput.addEventListener("change", () => { if (fileInput.files[0]) handleFileSelect(fileInput.files[0]); });

function handleFileSelect(file) {
  if (!file.type.includes("mpeg") && !file.name.endsWith(".mp3")) {
    alert("Please select an MP3 file.");
    return;
  }
  selectedFile = file;
  fileNameEl.textContent = file.name;
  fileNameEl.classList.remove("hidden");
  uploadBtn.disabled = false;
}

// ─── Upload & submit job ─────────────────────────────────────────────────────
uploadBtn.addEventListener("click", async () => {
  if (!selectedFile) return;

  resetJobUI();
  showJobSection();
  setStatus("uploading", true);
  setPipelineStep(0, false);
  addLog(LOG_LINES.uploading);

  let base64;
  try {
    base64 = await fileToBase64(selectedFile);
  } catch (err) {
    addLog("[ERROR]   Failed to read file: " + err.message);
    setStatus("file read error", false);
    return;
  }

  addLog(`[REST]    POST ${API_BASE}/apiv1/separate`);

  let hash;
  try {
    const res = await fetch(`${API_BASE}/apiv1/separate`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ mp3: base64, callback: null }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    hash = data.hash;
  } catch (err) {
    addLog("[ERROR]   Request failed: " + err.message);
    setStatus("connection error", false);
    return;
  }

  currentHash = hash;
  jobIdEl.textContent = hash;
  addLog(LOG_LINES.submitted);
  addLog(`[REST]    Job ID: ${hash}`);

  startTime = Date.now();
  elapsedTimer = setInterval(updateElapsed, 1000);

  startQueuePoller();
  startStatusPoller();
});

// ─── File → base64 ───────────────────────────────────────────────────────────
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload  = () => resolve(reader.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// ─── Status polling ───────────────────────────────────────────────────────────
let lastKnownState  = "queued";
let stateProgressed = 0;

const SIMULATED_STAGES = [
  "queued",
  "worker started",
  "loading model",
  "processing audio",
  "separating stems",
  "uploading results",
];

function startStatusPoller() {
  setStatus("queued", true);
  setPipelineStep(STATE_TO_STEP["queued"], false);
  addLog(LOG_LINES.queued);

  const deadline = Date.now() + JOB_TIMEOUT_MS;

  statusPoller = setInterval(async () => {
    if (Date.now() > deadline) {
      clearInterval(statusPoller);
      setStatus("timed out", false);
      addLog("[ERROR]   Job timed out after 10 minutes.");
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/apiv1/track/${currentHash}/vocals`);
      res.body?.cancel(); // status received — discard audio body immediately
      if (res.ok) {
        jobFinished();
        return;
      }
    } catch (_) { /* keep waiting */ }

    advanceSimulatedStage();
  }, STATUS_POLL_INTERVAL);
}

function advanceSimulatedStage() {
  if (stateProgressed < SIMULATED_STAGES.length - 1) {
    stateProgressed++;
    const stage = SIMULATED_STAGES[stateProgressed];
    if (stage !== lastKnownState) {
      lastKnownState = stage;
      setStatus(stage, true);
      const stepIdx = STATE_TO_STEP[stage];
      if (stepIdx !== undefined) setPipelineStep(stepIdx, false);
      if (LOG_LINES[stage]) addLog(LOG_LINES[stage]);
    }
  }
}

function jobFinished() {
  clearInterval(statusPoller);
  clearInterval(elapsedTimer);

  setStatus("finished", false);
  setPipelineStep(5, true);
  addLog(LOG_LINES.finished);

  // Copy final time to download panel
  if (finalTimeEl) finalTimeEl.textContent = elapsedEl.textContent;

  saveJobToStorage();
  showDownloadPanel();
}

// ─── localStorage persistence ─────────────────────────────────────────────────
const STORAGE_KEY = "stemsplit_last_job";

function saveJobToStorage() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      hash:      currentHash,
      finalTime: elapsedEl.textContent,
      savedAt:   Date.now(),
    }));
  } catch (_) {}
}

function restoreJobFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const job = JSON.parse(raw);
    // Discard if older than 24 hours
    if (Date.now() - job.savedAt > 24 * 60 * 60 * 1000) return;
    currentHash = job.hash;
    jobIdEl.textContent = job.hash;
    if (finalTimeEl) finalTimeEl.textContent = job.finalTime;
    showJobSection();
    setPipelineStep(5, true);
    setStatus("finished", false);
    showDownloadPanel();
  } catch (_) {}
}

// ─── Queue polling ────────────────────────────────────────────────────────────
function startQueuePoller() {
  pollQueue();
  queuePoller = setInterval(pollQueue, QUEUE_POLL_INTERVAL);
}

async function pollQueue() {
  try {
    const res  = await fetch(`${API_BASE}/apiv1/queue`);
    if (!res.ok) return;
    const data  = await res.json();
    const queue = data.queue || [];
    const count = queue.length;

    queueBadge.textContent   = `queue: ${count}`;
    queueCountEl.textContent = count;

    if (currentHash) {
      const pos = queue.indexOf(currentHash);
      queuePosEl.textContent = pos === -1 ? "processing" : `#${pos + 1}`;
    }
  } catch (_) { /* silently ignore */ }
}

// ─── Download panel ───────────────────────────────────────────────────────────
let activeAudio = null; // only one stem plays at a time

function showDownloadPanel() {
  downloadSection.classList.remove("hidden");
  downloadSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
  renderStemCards(currentHash);
  clearInterval(queuePoller);
}

function renderStemCards(hash) {
  downloadGrid.innerHTML = "";
  STEMS.forEach(stem => {
    const url  = `${API_BASE}/apiv1/track/${hash}/${stem.key}`;
    const card = document.createElement("div");
    card.className = `rounded-xl border ${stem.border} overflow-hidden`;

    card.innerHTML = `
      <div class="flex items-center gap-3 px-4 py-3 ${stem.bg}">
        <span class="text-lg leading-none">${stem.icon}</span>
        <div class="flex-1 min-w-0">
          <p class="font-semibold text-sm ${stem.text}">${stem.label}</p>
          <p class="text-[11px] opacity-50 font-mono truncate">${stem.key}.mp3</p>
        </div>
        <button data-action="play"
                class="play-btn flex items-center gap-1 text-xs font-semibold px-2.5 py-1.5 rounded-lg
                       bg-white/70 hover:bg-white transition-colors ${stem.text} flex-shrink-0">
          <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>
          Play
        </button>
        <button data-action="download"
                class="dl-btn flex items-center gap-1 text-xs font-semibold px-2.5 py-1.5 rounded-lg
                       bg-white/70 hover:bg-white transition-colors ${stem.text} flex-shrink-0">
          <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 3v13m0 0l-4-4m4 4l4-4M3 18h18"/>
          </svg>
          Save
        </button>
      </div>
      <div class="audio-wrap hidden px-4 py-2.5 bg-white border-t ${stem.border}">
        <audio controls class="w-full" style="height:36px;"></audio>
      </div>`;

    // Play button
    const playBtn  = card.querySelector(".play-btn");
    const audioWrap= card.querySelector(".audio-wrap");
    const audio    = card.querySelector("audio");

    playBtn.addEventListener("click", () => {
      const isOpen = !audioWrap.classList.contains("hidden");
      // Collapse any other open players
      document.querySelectorAll(".audio-wrap").forEach(w => {
        if (w !== audioWrap) { w.classList.add("hidden"); w.querySelector("audio").pause(); }
      });
      document.querySelectorAll(".play-btn").forEach(b => {
        if (b !== playBtn) b.innerHTML = b.innerHTML.replace("Stop", "Play")
                                                    .replace('d="M6 4h4v16H6zM14 4h4v16h-4z"', 'd="M5 3l14 9-14 9V3z"');
      });
      if (isOpen) {
        audioWrap.classList.add("hidden");
        audio.pause();
        playBtn.querySelector("svg path").setAttribute("d", "M5 3l14 9-14 9V3z");
        playBtn.childNodes[2].textContent = " Play";
      } else {
        if (!audio.src) audio.src = url;
        audioWrap.classList.remove("hidden");
        audio.play();
        playBtn.querySelector("svg path").setAttribute("d", "M6 4h4v16H6zM14 4h4v16h-4z");
        playBtn.childNodes[2].textContent = " Stop";
      }
    });
    audio.addEventListener("ended", () => {
      audioWrap.classList.add("hidden");
      playBtn.querySelector("svg path").setAttribute("d", "M5 3l14 9-14 9V3z");
      playBtn.childNodes[2].textContent = " Play";
    });

    // Download button — fetch as blob so browser forces a save dialog
    const dlBtn = card.querySelector(".dl-btn");
    dlBtn.addEventListener("click", () => forceDownload(url, `${stem.key}.mp3`, dlBtn));

    downloadGrid.appendChild(card);
  });
}

async function forceDownload(url, filename, btn) {
  const original = btn.innerHTML;
  btn.textContent = "…";
  btn.disabled = true;
  try {
    const res  = await fetch(url);
    const blob = await res.blob();
    const a    = document.createElement("a");
    a.href     = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 15000);
  } catch (err) {
    alert("Download failed: " + err.message);
  } finally {
    btn.innerHTML = original;
    btn.disabled  = false;
  }
}

// ─── Elapsed timer ────────────────────────────────────────────────────────────
function updateElapsed() {
  const secs = Math.floor((Date.now() - startTime) / 1000);
  const mm   = String(Math.floor(secs / 60)).padStart(2, "0");
  const ss   = String(secs % 60).padStart(2, "0");
  elapsedEl.textContent = `${mm}:${ss}`;
}

// ─── UI helpers ───────────────────────────────────────────────────────────────
function setStatus(label, spinning) {
  statusText.textContent = label;
  statusSpinner.classList.toggle("hidden", !spinning);
  statusSpinner.classList.toggle("spinner", spinning);
}

function addLog(line) {
  const ts    = new Date().toISOString().slice(11, 19);
  const entry = document.createElement("div");
  entry.className   = "log-entry";
  entry.textContent = `${ts}  ${line}`;
  logPanel.appendChild(entry);
  logPanel.scrollTop = logPanel.scrollHeight;
}

function showJobSection() {
  jobSection.classList.remove("hidden");
  jobSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetJobUI() {
  clearInterval(elapsedTimer);
  clearInterval(statusPoller);
  clearInterval(queuePoller);
  try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
  currentHash     = null;
  stateProgressed = 0;
  lastKnownState  = "queued";

  resetPipeline();
  logPanel.innerHTML      = "";
  downloadSection.classList.add("hidden");
  downloadGrid.innerHTML  = "";
  jobIdEl.textContent     = "—";
  elapsedEl.textContent   = "00:00";
  if (finalTimeEl) finalTimeEl.textContent = "—";
  queuePosEl.textContent  = "—";
  queueCountEl.textContent = "—";
}

// ─── Initial queue poll ───────────────────────────────────────────────────────
pollQueue();
setInterval(pollQueue, QUEUE_POLL_INTERVAL);

// ─── Restore last completed job from localStorage (survives page refresh) ─────
restoreJobFromStorage();
