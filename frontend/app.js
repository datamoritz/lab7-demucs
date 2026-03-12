// ─── Config ──────────────────────────────────────────────────────────────────
// Change API_BASE to point at your running REST service.
// In production this would be your ingress hostname / IP.
const API_BASE = "http://localhost:5001";

// How often to poll for queue updates (ms)
const QUEUE_POLL_INTERVAL  = 5000;
// How often to check whether the job has been processed (ms)
const STATUS_POLL_INTERVAL = 3000;
// Maximum time to wait for a job before giving up (ms) — 10 minutes
const JOB_TIMEOUT_MS       = 10 * 60 * 1000;

// ─── Stem definitions ────────────────────────────────────────────────────────
const STEMS = [
  { key: "vocals", label: "Vocals",  icon: "🎤", color: "bg-violet-100 text-violet-700 border-violet-200 hover:bg-violet-200" },
  { key: "drums",  label: "Drums",   icon: "🥁", color: "bg-amber-100  text-amber-700  border-amber-200  hover:bg-amber-200"  },
  { key: "bass",   label: "Bass",    icon: "🎸", color: "bg-sky-100    text-sky-700    border-sky-200    hover:bg-sky-200"    },
  { key: "other",  label: "Other",   icon: "🎹", color: "bg-emerald-100 text-emerald-700 border-emerald-200 hover:bg-emerald-200" },
];

// ─── Simulated log messages per stage ────────────────────────────────────────
// Real-time log lines are injected as state changes. These map a state key
// to the console line that should appear when the state is entered.
const LOG_LINES = {
  uploading:          "[REST]    Encoding file to base64...",
  submitted:          "[REST]    Job submitted → hash received",
  queued:             "[QUEUE]   Added to Redis list: toWorker",
  "worker started":   "[K8S]     Worker pod scheduled",
  "loading model":    "[WORKER]  Loading Demucs htdemucs model",
  "processing audio": "[PROCESS] Running stem separation",
  "separating stems": "[PROCESS] Separating: vocals, drums, bass, other",
  "uploading results":"[STORAGE] Uploading stems to MinIO → demucs-bucket/output/",
  finished:           "[DONE]    Separation complete — stems ready",
};

// ─── App state ───────────────────────────────────────────────────────────────
let selectedFile   = null;
let currentHash    = null;
let startTime      = null;
let elapsedTimer   = null;
let statusPoller   = null;
let queuePoller    = null;

// ─── DOM refs ────────────────────────────────────────────────────────────────
const dropZone       = document.getElementById("drop-zone");
const fileInput      = document.getElementById("file-input");
const fileNameEl     = document.getElementById("file-name");
const uploadBtn      = document.getElementById("upload-btn");
const jobSection     = document.getElementById("job-section");
const downloadSection= document.getElementById("download-section");
const downloadGrid   = document.getElementById("download-grid");
const jobIdEl        = document.getElementById("job-id");
const statusText     = document.getElementById("status-text");
const statusSpinner  = document.getElementById("status-spinner");
const elapsedEl      = document.getElementById("elapsed-time");
const queuePosEl     = document.getElementById("queue-pos");
const queueCountEl   = document.getElementById("queue-count");
const queueBadge     = document.getElementById("queue-badge");
const logPanel       = document.getElementById("log-panel");

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
fileInput.addEventListener("change",  () => { if (fileInput.files[0]) handleFileSelect(fileInput.files[0]); });

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

  // Reset UI for a new run
  resetJobUI();
  showJobSection();
  setStatus("uploading", true);
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

  // Start elapsed timer
  startTime = Date.now();
  elapsedTimer = setInterval(updateElapsed, 1000);

  // Start polling queue + job status
  startQueuePoller();
  startStatusPoller();
});

// ─── Convert File to base64 string ───────────────────────────────────────────
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload  = () => {
      // FileReader result is "data:audio/mpeg;base64,XXXX" — strip the prefix
      const b64 = reader.result.split(",")[1];
      resolve(b64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// ─── Status polling ───────────────────────────────────────────────────────────
// The backend has no /status endpoint, so we poll /apiv1/queue.
// When the hash disappears from the queue we attempt to fetch a stem.
// A successful 200 means the job is done; any error means still processing.

let lastKnownState  = "queued";
let stateProgressed = 0; // index into simulated progress stages

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
  addLog(LOG_LINES.queued);

  const deadline = Date.now() + JOB_TIMEOUT_MS;

  statusPoller = setInterval(async () => {
    if (Date.now() > deadline) {
      clearInterval(statusPoller);
      setStatus("timed out", false);
      addLog("[ERROR]   Job timed out after 10 minutes.");
      return;
    }

    // Try to fetch one stem. 200 → done. Anything else → still working.
    try {
      const res = await fetch(`${API_BASE}/apiv1/track/${currentHash}/vocals`, { method: "HEAD" });
      if (res.ok) {
        jobFinished();
        return;
      }
    } catch (_) {
      // Network error — keep waiting
    }

    // Simulate progressive status stages while we wait
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
      if (LOG_LINES[stage]) addLog(LOG_LINES[stage]);
    }
  }
}

function jobFinished() {
  clearInterval(statusPoller);
  clearInterval(elapsedTimer);

  setStatus("finished", false);
  addLog(LOG_LINES.finished);
  showDownloadPanel();
}

// ─── Queue polling ────────────────────────────────────────────────────────────
function startQueuePoller() {
  pollQueue(); // immediate first call
  queuePoller = setInterval(pollQueue, QUEUE_POLL_INTERVAL);
}

async function pollQueue() {
  try {
    const res  = await fetch(`${API_BASE}/apiv1/queue`);
    if (!res.ok) return;
    const data = await res.json();
    const queue = data.queue || [];

    const count = queue.length;
    queueBadge.textContent  = `queue: ${count}`;
    queueCountEl.textContent = count;

    if (currentHash) {
      const pos = queue.indexOf(currentHash);
      queuePosEl.textContent = pos === -1 ? "processing" : `#${pos + 1}`;
    }
  } catch (_) {
    // Silently ignore queue poll errors
  }
}

// ─── Download panel ───────────────────────────────────────────────────────────
function showDownloadPanel() {
  downloadSection.classList.remove("hidden");
  downloadSection.scrollIntoView({ behavior: "smooth", block: "nearest" });

  downloadGrid.innerHTML = "";
  STEMS.forEach(stem => {
    const url = `${API_BASE}/apiv1/track/${currentHash}/${stem.key}`;
    const btn = document.createElement("a");
    btn.href     = url;
    btn.download = `${stem.key}.mp3`;
    btn.className = `flex items-center gap-3 px-4 py-4 rounded-xl border font-medium text-sm
                     transition-colors cursor-pointer ${stem.color}`;
    btn.innerHTML = `
      <span class="text-xl">${stem.icon}</span>
      <div class="text-left">
        <p class="font-semibold">Download ${stem.label}</p>
        <p class="text-xs opacity-60 font-mono">${stem.key}.mp3</p>
      </div>
      <svg class="w-4 h-4 ml-auto opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>
      </svg>`;
    downloadGrid.appendChild(btn);
  });

  // Stop queue poller — no longer relevant
  clearInterval(queuePoller);
}

// ─── Elapsed timer ────────────────────────────────────────────────────────────
function updateElapsed() {
  const secs   = Math.floor((Date.now() - startTime) / 1000);
  const mm     = String(Math.floor(secs / 60)).padStart(2, "0");
  const ss     = String(secs % 60).padStart(2, "0");
  elapsedEl.textContent = `${mm}:${ss}`;
}

// ─── UI helpers ───────────────────────────────────────────────────────────────
function setStatus(label, spinning) {
  statusText.textContent = label;
  statusSpinner.classList.toggle("hidden", !spinning);
  statusSpinner.classList.toggle("spinner", spinning);
}

function addLog(line) {
  const ts    = new Date().toISOString().substr(11, 8);
  const entry = document.createElement("div");
  entry.className = "log-entry";
  entry.textContent = `${ts}  ${line}`;
  logPanel.appendChild(entry);
  logPanel.scrollTop = logPanel.scrollHeight;
}

function showJobSection() {
  jobSection.classList.remove("hidden");
  jobSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetJobUI() {
  // Clear previous run state
  clearInterval(elapsedTimer);
  clearInterval(statusPoller);
  clearInterval(queuePoller);
  currentHash     = null;
  stateProgressed = 0;
  lastKnownState  = "queued";

  logPanel.innerHTML    = "";
  downloadSection.classList.add("hidden");
  downloadGrid.innerHTML = "";
  jobIdEl.textContent   = "—";
  elapsedEl.textContent = "00:00";
  queuePosEl.textContent= "—";
  queueCountEl.textContent = "—";
}

// ─── Initial queue poll (shows queue count even before a job is submitted) ───
pollQueue();
setInterval(pollQueue, QUEUE_POLL_INTERVAL);
