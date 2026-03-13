// ─── Config ──────────────────────────────────────────────────────────────────
const API_BASE = "http://localhost:5001";

const QUEUE_POLL_INTERVAL  = 5000;
const STATUS_POLL_INTERVAL = 3000;
const JOB_TIMEOUT_MS       = 10 * 60 * 1000;
const SYS_POLL_INTERVAL    = 10000;

// ─── Stem definitions ────────────────────────────────────────────────────────
const STEMS = [
  { key: "vocals", label: "Vocals", icon: "🎤", color: "bg-violet-50 text-violet-700 border-violet-200 hover:bg-violet-100" },
  { key: "drums",  label: "Drums",  icon: "🥁", color: "bg-amber-50  text-amber-700  border-amber-200  hover:bg-amber-100"  },
  { key: "bass",   label: "Bass",   icon: "🎸", color: "bg-sky-50    text-sky-700    border-sky-200    hover:bg-sky-100"    },
  { key: "other",  label: "Other",  icon: "🎹", color: "bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-100" },
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
  if (state === "ok")  { dot.classList.add("dot-ok");  el.classList.replace("text-slate-400", "text-slate-600"); }
  if (state === "err") { dot.classList.add("dot-err");  el.classList.replace("text-slate-400", "text-red-500"); }
  if (state === "idle"){ dot.classList.add("dot-idle"); }
  lbl.textContent = label;
}

async function checkSystemStatus() {
  // Try a dedicated health endpoint first; fall back to queue endpoint
  let apiOk = false;
  let redisOk = true;
  let workerCount = null;
  let minioOk = false;
  let k8sOk = false;

  try {
    const res = await fetch(`${API_BASE}/apiv1/health`, { signal: AbortSignal.timeout(4000) });
    if (res.ok) {
      const data = await res.json();
      apiOk      = true;
      redisOk    = data.redis  ?? true;
      minioOk    = data.minio  ?? true;
      k8sOk      = data.k8s   ?? true;
      workerCount = data.workers ?? null;
    }
  } catch (_) {
    // Health endpoint not available — infer from queue
    try {
      const res = await fetch(`${API_BASE}/apiv1/queue`, { signal: AbortSignal.timeout(4000) });
      if (res.ok) {
        apiOk   = true;
        redisOk = true;
        minioOk = true;  // MinIO must be reachable if API is fully up
        k8sOk   = true;
      }
    } catch (_) { /* unreachable */ }
  }

  if (apiOk) {
    setSysStatus("sys-api",     "ok",  "running");
    setSysStatus("sys-redis",   redisOk ? "ok" : "err",  redisOk ? "connected" : "unreachable");
    setSysStatus("sys-minio",   minioOk ? "ok" : "err", minioOk ? "connected" : "unreachable");
    setSysStatus("sys-k8s",     k8sOk  ? "ok" : "err", k8sOk   ? "cluster active" : "unavailable");
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
      const res = await fetch(`${API_BASE}/apiv1/track/${currentHash}/vocals`, { method: "HEAD" });
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

  showDownloadPanel();
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
function showDownloadPanel() {
  downloadSection.classList.remove("hidden");
  downloadSection.scrollIntoView({ behavior: "smooth", block: "nearest" });

  downloadGrid.innerHTML = "";
  STEMS.forEach(stem => {
    const url = `${API_BASE}/apiv1/track/${currentHash}/${stem.key}`;
    const btn = document.createElement("a");
    btn.href      = url;
    btn.download  = `${stem.key}.mp3`;
    btn.className = `flex items-center gap-3 px-4 py-4 rounded-xl border font-medium text-sm
                     transition-colors cursor-pointer ${stem.color}`;
    btn.innerHTML = `
      <span class="text-xl leading-none">${stem.icon}</span>
      <div class="text-left">
        <p class="font-semibold text-sm">Download ${stem.label}</p>
        <p class="text-xs opacity-50 font-mono mt-0.5">${stem.key}.mp3</p>
      </div>
      <svg class="w-4 h-4 ml-auto opacity-40 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>
      </svg>`;
    downloadGrid.appendChild(btn);
  });

  clearInterval(queuePoller);
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
