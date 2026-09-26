const $ = (selector) => document.querySelector(selector);

const form = $("#genForm");
const urlInput = $("#urlInput");
const varCount = $("#varCount");
const genBtn = $("#genBtn");
const loader = $("#loader");
const bar = $("#bar");
const steps = Array.from(document.querySelectorAll("#steps li"));
const results = $("#results");
const tabsEl = $("#tabs");
const panelsEl = $("#panels");
const errorBox = $("#errorBox");
const errorMessage = $("#errorMessage");
const toastEl = $("#toast");
const manualPanel = $("#manualPanel");
const manualTitle = $("#manualTitle");
const manualReason = $("#manualReason");
const manualForm = $("#manualForm");
const transcriptInput = $("#transcriptInput");
const manualCount = $("#manualCount");
const manualGenBtn = $("#manualGenBtn");

const state = { data: null, active: 0, stepTimer: null, toastTimer: null, pendingUrl: "" };

/* ---------------- boot ---------------- */
checkHealth();
urlInput.focus();

async function checkHealth() {
  const statusEl = $("#apiStatus");
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    statusEl.classList.add(data.groq_key_configured ? "ok" : "bad");
    statusEl.querySelector(".status-text").textContent = data.groq_key_configured
      ? `${data.model} ready`
      : "no api key";
    statusEl.title = data.groq_key_configured
      ? `Groq connected with ${data.model}`
      : "Set GROQ_API_KEY in your environment";
  } catch {
    statusEl.classList.add("bad");
    statusEl.querySelector(".status-text").textContent = "offline";
  }
}

/* ---------------- form ---------------- */
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  hide(errorBox);
  hide(results);
  hide(manualPanel);
  setBusy(true);
  startLoader();

  try {
    const data = await requestGenerate("/api/generate", {
      url,
      variations: Number(varCount.value),
    });
    finishGenerate(data);
  } catch (err) {
    stopLoader();
    if (err.kind === "blocked" || err.kind === "fetch_failed" || err.kind === "no_captions") {
      showManual(err, url);
    } else {
      showError(err.message || "Unexpected error. Try again.");
    }
  } finally {
    setBusy(false);
  }
});

async function requestGenerate(endpoint, payload) {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || "Request failed. Try again.");
    err.kind = data.kind || "error";
    throw err;
  }
  return data;
}

function finishGenerate(data) {
  finishLoader();
  state.data = data;
  state.active = 0;
  render(data);
  setTimeout(() => hide(loader), 650);
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

function showManual(err, url) {
  state.pendingUrl = url;
  manualTitle.textContent =
    err.kind === "no_captions"
      ? "No captions found automatically"
      : "YouTube blocked the automatic read";
  manualReason.textContent =
    err.kind === "no_captions"
      ? `${err.message} If you have the subtitles or script, paste them below and generation will continue as normal.`
      : `${err.message} Paste the transcript below and you will get the same titles, description and tags.`;
  show(manualPanel);
  manualPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

manualForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const transcript = transcriptInput.value.trim();
  if (transcript.length < 50) {
    toast("Paste a bit more text — at least a few sentences");
    transcriptInput.focus();
    return;
  }

  hide(errorBox);
  hide(results);
  setManualBusy(true);
  startLoader();

  try {
    const data = await requestGenerate("/api/generate-from-text", {
      transcript,
      url: state.pendingUrl,
      variations: Number(varCount.value),
    });
    hide(manualPanel);
    finishGenerate(data);
  } catch (err) {
    stopLoader();
    showError(err.message || "Unexpected error. Try again.");
  } finally {
    setManualBusy(false);
  }
});

transcriptInput.addEventListener("input", () => {
  manualCount.textContent = `${transcriptInput.value.length.toLocaleString()} characters`;
});

function setManualBusy(busy) {
  manualGenBtn.disabled = busy;
  manualGenBtn.classList.toggle("loading", busy);
}

$("#clearBtn").addEventListener("click", () => {
  urlInput.value = "";
  urlInput.focus();
  hide(errorBox);
  hide(results);
  hide(manualPanel);
  stopLoader();
  hide(loader);
});

$("#dismissError").addEventListener("click", () => hide(errorBox));

document.querySelectorAll(".example-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    urlInput.value = chip.dataset.url;
    urlInput.focus();
  });
});

/* ---------------- loader ---------------- */
function startLoader() {
  show(loader);
  steps.forEach((step, i) => step.className = i === 0 ? "active" : "");
  bar.style.width = "8%";
  clearInterval(state.stepTimer);

  let current = 0;
  const targets = [45, 72, 88];

  state.stepTimer = setInterval(() => {
    current = Math.min(current + 1, steps.length - 1);
    steps.forEach((step, i) => {
      step.className = i < current ? "done" : i === current ? "active" : "";
    });
    bar.style.width = `${targets[current]}%`;
  }, 1500);
}

function finishLoader() {
  clearInterval(state.stepTimer);
  steps.forEach((step) => step.className = "done");
  bar.style.width = "100%";
}

function stopLoader() {
  clearInterval(state.stepTimer);
  hide(loader);
}

function setBusy(busy) {
  genBtn.disabled = busy;
  genBtn.classList.toggle("loading", busy);
}

function showError(message) {
  errorMessage.textContent = message;
  show(errorBox);
}

/* ---------------- render ---------------- */
function render(data) {
  const variations = data.variations || [];

  $("#langPill").textContent = `transcript: ${data.language || "unknown"}`;
  $("#modelPill").textContent = data.model || "groq";
  $("#countPill").textContent = `${variations.length} variation${variations.length > 1 ? "s" : ""}`;

  tabsEl.innerHTML = "";
  variations.forEach((variation, index) => {
    const tab = el("button", {
      class: `tab${index === state.active ? " active" : ""}`,
      type: "button",
      role: "tab",
      text: `Option ${index + 1}`,
    });
    tab.addEventListener("click", () => {
      state.active = index;
      renderPanels(data);
      Array.from(tabsEl.children).forEach((node, i) =>
        node.classList.toggle("active", i === index)
      );
    });
    tabsEl.appendChild(tab);
  });

  renderPanels(data);

  const transcript = data.transcript || "";
  $("#transcriptBody").textContent = transcript;
  $("#transcriptMeta").textContent = `${transcript.length.toLocaleString()} characters`;

  show(results);
}

function renderPanels(data) {
  const variation = data.variations[state.active];
  if (!variation) return;

  panelsEl.innerHTML = "";

  panelsEl.appendChild(
    card("Title", variation.title, {
      text: variation.title,
      counter: `${variation.title.length}/100 characters`,
      copy: copyText,
      display: "title",
    })
  );

  panelsEl.appendChild(
    card("Description", variation.description, {
      text: variation.description,
      counter: `${variation.description.length}/5000 characters`,
      copy: copyText,
      display: "desc",
    })
  );

  const tagPanel = card("Tags", variation.tags.join(", "), {
    text: variation.tags.join(", "),
    counter: `${variation.tags.length} tags · ${variation.tags.join(", ").length}/500 characters`,
    copy: copyText,
    display: "tags",
    tags: variation.tags,
  });
  panelsEl.appendChild(tagPanel);
}

function card(label, value, options) {
  const panel = el("div", { class: "panel card" });

  const head = el("div", { class: "card-label" });
  head.appendChild(el("span", { text: label }));

  const copyBtn = el("button", { class: "copy-btn", type: "button", text: "Copy" });
  copyBtn.addEventListener("click", () => options.copy(options.text, copyBtn));
  head.appendChild(copyBtn);
  panel.appendChild(head);

  if (options.display === "title") {
    panel.appendChild(el("h3", { class: "card-title", text: options.text }));
  } else if (options.display === "tags") {
    const cloud = el("div", { class: "tag-cloud" });
    options.tags.forEach((tag, i) => {
      const chip = el("span", { class: "tag", text: tag });
      chip.style.animationDelay = `${Math.min(i * 22, 500)}ms`;
      cloud.appendChild(chip);
    });
    panel.appendChild(cloud);
  } else {
    panel.appendChild(el("p", { class: "card-desc", text: options.text }));
  }

  panel.appendChild(el("span", { class: "counter", text: options.counter }));
  return panel;
}

/* ---------------- actions ---------------- */
$("#copyAllBtn").addEventListener("click", (event) => {
  if (!state.data) return;
  copyText(buildReport(state.data), event.currentTarget);
});

$("#downloadBtn").addEventListener("click", () => {
  if (!state.data) return;
  const blob = new Blob([buildReport(state.data)], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${state.data.video_id}-metadata.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1500);
  toast("Downloaded");
});

$("#copyTranscriptBtn").addEventListener("click", (event) => {
  if (!state.data) return;
  copyText(state.data.transcript, event.currentTarget);
});

function buildReport(data) {
  const lines = [];
  lines.push(`YouTube metadata report`);
  lines.push(`Source: ${data.video_url}`);
  lines.push(`Language: ${data.language}   Model: ${data.model}`);
  lines.push("");

  data.variations.forEach((variation, index) => {
    lines.push("=".repeat(60));
    lines.push(`OPTION ${index + 1}`);
    lines.push("=".repeat(60));
    lines.push("");
    lines.push(`TITLE (${variation.title.length}/100)`);
    lines.push(variation.title);
    lines.push("");
    lines.push(`DESCRIPTION (${variation.description.length}/5000)`);
    lines.push(variation.description);
    lines.push("");
    lines.push(`TAGS (${variation.tags.join(", ").length}/500)`);
    lines.push(variation.tags.join(", "));
    lines.push("");
  });

  return lines.join("\n");
}

/* ---------------- helpers ---------------- */
async function copyText(text, button) {
  let ok = true;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      ok = document.execCommand("copy");
      area.remove();
    }
  } catch {
    ok = false;
  }

  if (ok) {
    toast("Copied to clipboard");
    if (button && button.classList.contains("copy-btn")) {
      const original = button.textContent;
      button.textContent = "Copied";
      button.classList.add("done");
      setTimeout(() => {
        button.textContent = original;
        button.classList.remove("done");
      }, 1600);
    }
  } else {
    toast("Copy failed — select the text manually");
  }
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2000);
}

function el(tag, attrs = {}) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  });
  return node;
}

function show(node) { node.classList.remove("hidden"); }
function hide(node) { node.classList.add("hidden"); }
