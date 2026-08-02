// Recorder widget: capture → client-side playback → save/discard → parse.
// Nothing is uploaded until the user hits save.
// Usage: <div data-recorder data-variant="full|compact"></div>

(() => {
  const MIME_CANDIDATES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4",
    "audio/ogg;codecs=opus",
  ];
  const OPUS_BITRATE = 96000;
  // whisper.cpp on this setup only reads WAV, so we decode the
  // MediaRecorder output and resample to 16 kHz mono in the browser.
  const TARGET_RATE = 16000;

  const widgets = document.querySelectorAll("[data-recorder]");
  widgets.forEach(initRecorder);

  const imageWidgets = document.querySelectorAll("[data-image-picker]");
  imageWidgets.forEach(initImagePicker);

  const textWidgets = document.querySelectorAll("[data-text-entry]");
  textWidgets.forEach(initTextEntry);

  if (widgets.length === 0 && imageWidgets.length === 0 && textWidgets.length === 0) return;

  const pendingList = document.querySelector("[data-pending-list]");
  if (pendingList) {
    refreshPending(pendingList);
    document.addEventListener("recorder:saved", () => refreshPending(pendingList));
    document.addEventListener("recorder:parsed", () => refreshPending(pendingList));
    document.addEventListener("recorder:deleted", () => refreshPending(pendingList));
    window.addEventListener("pageshow", (ev) => {
      if (ev.persisted) refreshPending(pendingList);
    });
  }

  const pullBtn = document.getElementById("pull-btn");
  const pullStatus = document.querySelector(".pull-status");
  if (pullBtn && pendingList) {
    pullBtn.addEventListener("click", () => runPullAndParse(pullBtn, pullStatus, pendingList));
  }

  async function runPullAndParse(btn, statusEl, listEl) {
    const setStatus = (msg) => {
      if (!statusEl) return;
      statusEl.hidden = !msg;
      statusEl.textContent = msg || "";
    };
    btn.disabled = true;
    setStatus("pulling…");
    let pulled = [];
    try {
      const res = await fetch("/api/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "audio" }),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
      pulled = json.pulled || [];
    } catch (e) {
      setStatus(`pull failed: ${e.message || e}`);
      btn.disabled = false;
      return;
    }

    if (pulled.length === 0) {
      setStatus("nothing to import");
      btn.disabled = false;
      setTimeout(() => setStatus(""), 2200);
      return;
    }

    await refreshPending(listEl);
    const errors = [];
    let parsed = 0;
    for (let i = 0; i < pulled.length; i++) {
      const item = pulled[i];
      setStatus(`parsing ${i + 1}/${pulled.length}: ${item.filename}`);
      try {
        const res = await fetch("/api/record/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: item.filename }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
        parsed++;
        await refreshPending(listEl);
      } catch (e) {
        errors.push(`${item.filename}: ${e.message || e}`);
      }
    }
    const summary = errors.length
      ? `imported ${pulled.length}, parsed ${parsed}, ${errors.length} error(s)`
      : `imported ${pulled.length}, parsed ${parsed}`;
    setStatus(summary);
    if (errors.length) console.warn("pull/parse errors:", errors);
    btn.disabled = false;
    setTimeout(() => setStatus(""), 4000);
  }

  function initRecorder(root) {
    const variant = root.dataset.variant || "full";
    root.innerHTML = template(variant);
    root.dataset.state = "idle";

    const btn = root.querySelector(".rec-btn");
    const statusEl = root.querySelector(".rec-status");
    const review = root.querySelector(".rec-review");
    const playback = root.querySelector(".rec-playback");
    const saveBtn = root.querySelector(".rec-save");
    const discardBtn = root.querySelector(".rec-discard");
    const errEl = root.querySelector(".rec-error");
    const result = root.querySelector(".rec-result");
    const transcript = root.querySelector(".rec-transcript");
    const toastEl = root.querySelector(".rec-toast");
    let toastHideTimer = null;

    const state = {
      recorder: null,
      stream: null,
      chunks: [],
      mime: "",
      recording: false,
      startedAt: 0,
      tickTimer: null,
      pendingBlob: null,
      blobUrl: null,
    };

    const setStatus = (s) => { statusEl.textContent = s; };
    const showError = (msg) => { errEl.hidden = false; errEl.textContent = msg; };
    const clearError = () => { errEl.hidden = true; errEl.textContent = ""; };
    const setUiState = (s) => {
      root.dataset.state = s;
      review.hidden = s !== "review" && s !== "saving" && s !== "saved";
      result.hidden = s !== "saved";
      if (s === "idle" || s === "recording") {
        transcript.hidden = true;
        transcript.textContent = "";
      }
    };

    function resetToIdle() {
      playback.removeAttribute("src");
      playback.load();
      playback.hidden = true;
      result.hidden = true;
      result.innerHTML = "";
      transcript.hidden = true;
      transcript.textContent = "";
      setStatus("idle");
      setUiState("idle");
    }

    function flashSavedToast() {
      if (!toastEl) return;
      if (toastHideTimer) { clearTimeout(toastHideTimer); toastHideTimer = null; }
      toastEl.hidden = false;
      toastEl.textContent = "saved";
      toastEl.classList.remove("fade-out");
      requestAnimationFrame(() => {
        requestAnimationFrame(() => toastEl.classList.add("fade-out"));
      });
      toastHideTimer = setTimeout(() => {
        toastEl.hidden = true;
        toastEl.classList.remove("fade-out");
        toastHideTimer = null;
      }, 2200);
    }

    btn.addEventListener("click", () => {
      if (state.recording) stopCapture();
      else startCapture();
    });
    discardBtn.addEventListener("click", () => discard());
    saveBtn.addEventListener("click", () => save());

    async function startCapture() {
      clearError();
      if (!navigator.mediaDevices?.getUserMedia) {
        showError("this browser has no getUserMedia (needs https or localhost)");
        return;
      }
      if (typeof MediaRecorder === "undefined") {
        showError("this browser has no MediaRecorder");
        return;
      }
      try {
        state.stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      } catch (e) {
        showError(`mic permission denied: ${e.message || e}`);
        return;
      }
      const mime = MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported?.(m)) || "";
      try {
        state.recorder = new MediaRecorder(state.stream, mime
          ? { mimeType: mime, audioBitsPerSecond: OPUS_BITRATE }
          : { audioBitsPerSecond: OPUS_BITRATE });
      } catch (e) {
        showError(`MediaRecorder init failed: ${e.message || e}`);
        stopStream();
        return;
      }
      state.mime = state.recorder.mimeType || mime || "audio/webm";
      state.chunks = [];
      state.recorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) state.chunks.push(ev.data);
      };
      state.recorder.onerror = (ev) => {
        showError(`recorder error: ${ev.error?.message || ev.error || "unknown"}`);
      };
      state.recorder.onstop = () => finalizeCapture();
      state.recorder.start(1000);
      state.recording = true;
      state.startedAt = Date.now();
      btn.textContent = "stop";
      btn.classList.add("recording");
      setStatus(`recording 0s`);
      setUiState("recording");
      state.tickTimer = setInterval(() => {
        const secs = Math.floor((Date.now() - state.startedAt) / 1000);
        setStatus(`recording ${secs}s`);
      }, 500);
    }

    function stopCapture() {
      if (!state.recording) return;
      state.recording = false;
      if (state.tickTimer) { clearInterval(state.tickTimer); state.tickTimer = null; }
      btn.textContent = "record";
      btn.classList.remove("recording");
      try { state.recorder?.stop(); } catch {}
    }

    function stopStream() {
      if (state.stream) {
        for (const t of state.stream.getTracks()) t.stop();
        state.stream = null;
      }
    }

    async function finalizeCapture() {
      stopStream();
      if (state.chunks.length === 0) {
        showError("captured 0 bytes");
        setStatus("idle");
        setUiState("idle");
        return;
      }
      const encoded = new Blob(state.chunks, { type: state.mime });
      state.chunks = [];
      setStatus(`encoding ${(encoded.size / 1024).toFixed(1)} kB → 16 kHz wav…`);
      try {
        state.pendingBlob = await transcodeToWav(encoded, TARGET_RATE);
      } catch (e) {
        showError(`transcode failed: ${e.message || e}`);
        setStatus("idle");
        setUiState("idle");
        return;
      }
      if (state.blobUrl) URL.revokeObjectURL(state.blobUrl);
      state.blobUrl = URL.createObjectURL(state.pendingBlob);
      playback.src = state.blobUrl;
      playback.hidden = false;
      setStatus(`ready — ${(state.pendingBlob.size / 1024).toFixed(1)} kB. review below.`);
      setUiState("review");
    }

    function discard() {
      state.pendingBlob = null;
      if (state.blobUrl) { URL.revokeObjectURL(state.blobUrl); state.blobUrl = null; }
      playback.removeAttribute("src");
      playback.load();
      playback.hidden = true;
      setStatus("discarded. idle.");
      setUiState("idle");
      clearError();
    }

    async function save() {
      if (!state.pendingBlob) { showError("nothing to save"); return; }
      clearError();
      setStatus(`saving ${(state.pendingBlob.size / 1024).toFixed(1)} kB…`);
      setUiState("saving");
      let savedFilename = null;
      try {
        const res = await fetch("/api/record", {
          method: "POST",
          body: state.pendingBlob,
          headers: { "Content-Type": state.pendingBlob.type || "audio/wav" },
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
        savedFilename = json.filename;
        document.dispatchEvent(new CustomEvent("recorder:saved", { detail: json }));
        resetToIdle();
        flashSavedToast();
      } catch (e) {
        showError(`upload failed: ${e.message || e}`);
        setStatus("idle");
        setUiState("review");
        return;
      }
      // parse is best-effort — capture itself is already persisted.
      try {
        const res = await fetch("/api/record/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: savedFilename }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
        document.dispatchEvent(new CustomEvent("recorder:parsed", { detail: json }));
      } catch (e) {
        console.warn("parse failed:", e);
      }
      state.pendingBlob = null;
      if (state.blobUrl) { URL.revokeObjectURL(state.blobUrl); state.blobUrl = null; }
    }

    setUiState("idle");
    setStatus("idle");
  }

  function initImagePicker(root) {
    root.innerHTML = imageTemplate();
    const fileInputs = Array.from(root.querySelectorAll(".img-file"));
    const preview = root.querySelector(".img-preview");
    const renameInput = root.querySelector(".img-rename");
    const instructionsEl = root.querySelector(".img-instructions");
    const fieldsEl = root.querySelector(".img-fields");
    const actionsEl = root.querySelector(".img-actions");
    const uploadBtn = root.querySelector(".img-upload");
    const clearBtn = root.querySelector(".img-clear");
    const statusEl = root.querySelector(".img-status");
    const errorEl = root.querySelector(".img-error");

    let chosenFile = null;
    let previewUrl = null;

    const setStatus = (msg) => { statusEl.textContent = msg || ""; };
    const showError = (msg) => { errorEl.hidden = !msg; errorEl.textContent = msg || ""; };

    fileInputs.forEach((input) => {
      input.addEventListener("change", (e) => {
        showError("");
        const picked = e.currentTarget.files && e.currentTarget.files[0];
        chosenFile = picked || null;
        if (!chosenFile) {
          resetPicker();
          return;
        }
        fileInputs.forEach((other) => { if (other !== e.currentTarget) other.value = ""; });
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(chosenFile);
        preview.src = previewUrl;
        preview.hidden = false;
        fieldsEl.hidden = false;
        actionsEl.hidden = false;
        const defaultStem = suggestStem(chosenFile.name);
        if (!renameInput.value) renameInput.value = defaultStem;
        uploadBtn.disabled = false;
        clearBtn.disabled = false;
        setStatus(`${(chosenFile.size / 1024).toFixed(1)} kB selected`);
      });
    });

    clearBtn.addEventListener("click", () => resetPicker());

    uploadBtn.addEventListener("click", async () => {
      if (!chosenFile) { showError("pick an image first"); return; }
      const stemRaw = (renameInput.value || "").trim();
      const stemClean = sanitizeStem(stemRaw);
      if (stemRaw && stemClean && stemClean !== stemRaw) renameInput.value = stemClean;
      const instructions = (instructionsEl.value || "").trim();
      if (instructions.length > 500) {
        showError("instructions too long (max 500 chars).");
        return;
      }
      showError("");
      uploadBtn.disabled = true;
      clearBtn.disabled = true;
      setStatus(`uploading ${(chosenFile.size / 1024).toFixed(1)} kB…`);
      try {
        const headers = { "Content-Type": chosenFile.type || "image/jpeg" };
        if (stemClean) headers["X-Filename-Stem"] = stemClean;
        if (instructions) headers["X-Instructions"] = instructions;
        const res = await fetch("/api/image", {
          method: "POST",
          body: chosenFile,
          headers,
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
        setStatus(`saved: ${json.filename}${json.has_instructions ? " (with instructions)" : ""}`);
        document.dispatchEvent(new CustomEvent("recorder:saved", { detail: json }));
        resetPicker();
      } catch (e) {
        showError(`upload failed: ${e.message || e}`);
        setStatus("");
        uploadBtn.disabled = false;
        clearBtn.disabled = false;
      }
    });

    function resetPicker() {
      chosenFile = null;
      fileInputs.forEach((input) => { input.value = ""; });
      if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
      preview.removeAttribute("src");
      preview.hidden = true;
      fieldsEl.hidden = true;
      actionsEl.hidden = true;
      renameInput.value = "";
      instructionsEl.value = "";
      uploadBtn.disabled = true;
      clearBtn.disabled = true;
      showError("");
    }

    function sanitizeStem(raw) {
      return (raw || "")
        .trim()
        .replace(/\s+/g, "_")
        .replace(/[^A-Za-z0-9._-]/g, "")
        .replace(/_+/g, "_")
        .replace(/^[-_.]+|[-_.]+$/g, "");
    }

    function suggestStem(filename) {
      const base = (filename || "").replace(/\.[^.]+$/, "");
      const cleaned = sanitizeStem(base);
      const meaningful = cleaned && /[A-Za-z]/.test(cleaned);
      return meaningful ? cleaned : `ui-image_${timestampStamp()}`;
    }

    function timestampStamp() {
      const d = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    }

    resetPicker();
  }

  function initTextEntry(root) {
    root.innerHTML = textEntryTemplate();
    const openBtn = root.querySelector(".txt-open");
    const form = root.querySelector(".txt-form");
    const textarea = root.querySelector(".txt-body");
    const submitBtn = root.querySelector(".txt-submit");
    const cancelBtn = root.querySelector(".txt-cancel");
    const statusEl = root.querySelector(".txt-status");
    const errorEl = root.querySelector(".txt-error");

    const setStatus = (msg) => { statusEl.textContent = msg || ""; };
    const showError = (msg) => { errorEl.hidden = !msg; errorEl.textContent = msg || ""; };

    function reset() {
      textarea.value = "";
      form.hidden = true;
      openBtn.hidden = false;
      submitBtn.disabled = false;
      cancelBtn.disabled = false;
      showError("");
      setStatus("");
    }

    openBtn.addEventListener("click", () => {
      form.hidden = false;
      openBtn.hidden = true;
      textarea.focus();
    });

    cancelBtn.addEventListener("click", () => reset());

    submitBtn.addEventListener("click", async () => {
      const body = (textarea.value || "").trim();
      if (!body) { showError("type something first"); return; }
      showError("");
      submitBtn.disabled = true;
      cancelBtn.disabled = true;
      setStatus("saving…");
      try {
        const res = await fetch("/api/text", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
        document.dispatchEvent(new CustomEvent("recorder:saved", { detail: json }));
        reset();
      } catch (e) {
        showError(`save failed: ${e.message || e}`);
        setStatus("");
        submitBtn.disabled = false;
        cancelBtn.disabled = false;
      }
    });

    reset();
  }

  function textEntryTemplate() {
    return `
      <p class="txt-help">type a note — thoughts, quotes, snippets. lands in <code>inbox/</code> as a <code>.txt</code> file; <code>mind ingest</code> promotes it into the wiki.</p>
      <div class="txt-controls">
        <button type="button" class="txt-open">new text entry</button>
      </div>
      <div class="txt-form" hidden>
        <textarea class="txt-body" rows="6" spellcheck="true" placeholder="type here…"></textarea>
        <div class="txt-actions">
          <button type="button" class="txt-submit">submit</button>
          <button type="button" class="txt-cancel">cancel</button>
          <span class="txt-status"></span>
        </div>
        <p class="txt-error" hidden></p>
      </div>
    `;
  }

  function imageTemplate() {
    return `
      <p class="img-help">pick an image from your phone — documents, receipts, whiteboards. optionally rename it and attach an instruction (e.g. <em>"extract only items and prices"</em>) for the ingest agent. clips land in <code>inbox/</code>; <code>mind ingest</code> promotes them into the wiki.</p>
      <div class="img-controls">
        <label class="img-file-label">
          <input type="file" class="img-file" accept="image/*">
          <span class="img-file-btn">choose image</span>
        </label>
        <label class="img-file-label">
          <input type="file" class="img-file img-file-camera" accept="image/*" capture="environment">
          <span class="img-file-btn">take photo</span>
        </label>
        <span class="img-status"></span>
      </div>
      <img class="img-preview" hidden alt="selected image preview">
      <div class="img-fields" hidden>
        <label class="img-field">
          <span>rename (optional)</span>
          <input type="text" class="img-rename" placeholder="trader-joes-receipt" autocomplete="off" spellcheck="false">
        </label>
        <label class="img-field">
          <span>instructions for ingest agent (optional)</span>
          <textarea class="img-instructions" rows="2" placeholder="e.g. extract only items and costs, skip headers. leave blank for full transcription." maxlength="500" spellcheck="true"></textarea>
        </label>
      </div>
      <div class="img-actions" hidden>
        <button type="button" class="img-upload" disabled>upload</button>
        <button type="button" class="img-clear" disabled>clear</button>
      </div>
      <p class="img-error" hidden></p>
    `;
  }

  function template(variant) {
    const help = variant === "compact"
      ? ""
      : `<p class="record-help">capture → review → save. clips land in <code>inbox/</code> and are parsed on save; nothing hits the wiki until you run <code>mind ingest inbox/&lt;file&gt;</code>.</p>`;
    return `
      ${help}
      <div class="rec-controls">
        <button type="button" class="rec-btn">record</button>
        <span class="rec-status">idle</span>
      </div>
      <div class="rec-overflow">
        <div class="rec-review" hidden>
          <audio class="rec-playback" controls preload="auto"></audio>
          <div class="rec-review-actions">
            <button type="button" class="rec-save">save</button>
            <button type="button" class="rec-discard">discard</button>
          </div>
        </div>
        <div class="rec-result" hidden></div>
        <pre class="rec-transcript" hidden></pre>
        <div class="rec-toast" hidden></div>
        <p class="rec-error" hidden></p>
      </div>
    `;
  }

  async function refreshPending(listEl) {
    try {
      const res = await fetch("/api/pending", { cache: "no-store" });
      const json = await res.json();
      renderPending(listEl, json.items || []);
    } catch (e) {
      listEl.innerHTML = `<p class="rec-error">failed to load pending: ${e.message || e}</p>`;
    }
  }

  function renderPending(listEl, items) {
    if (items.length === 0) {
      listEl.innerHTML = `<p class="empty">nothing pending.</p>`;
      return;
    }
    const rows = items.map((it) => {
      const kb = (it.bytes / 1024).toFixed(1);
      const kind = it.kind || "other";
      // For images, show the user's ingest instructions (what the agent will
      // honor). For audio/text, show the transcript/body preview. All three are editable.
      const editSource = kind === "image" ? (it.instructions || "") : (it.transcript || "");
      const previewText = kind === "image"
        ? (it.instructions ? it.instructions : "no instructions yet — agent will do a full transcription")
        : (it.transcript || "");
      const previewClass = kind === "image" ? "pending-instructions" : "pending-transcript";
      const preview = (kind === "image" || it.transcript)
        ? `<div class="${previewClass}" data-full="${escapeAttr(editSource)}">${escapeHtml(truncate(previewText, 240))}</div>`
        : "";
      let player = "";
      if (kind === "audio") {
        player = `<audio class="pending-audio" controls preload="none" src="/api/record/${encodeURIComponent(it.filename)}/audio"></audio>`;
      } else if (kind === "image") {
        player = `<img class="pending-image" loading="lazy" src="/api/image/${encodeURIComponent(it.filename)}" alt="${escapeAttr(it.filename)}">`;
      }
      const cmd = `mind ingest ${it.path}`;
      const editable = it.state === "parsed" && (kind === "audio" || kind === "image" || kind === "text");
      const editLabel = kind === "image" ? "edit instructions" : kind === "text" ? "edit text" : "edit transcript";
      const editBtn = editable
        ? `<button type="button" class="pending-edit">${editLabel}</button>`
        : "";
      return `
        <li class="pending-row" data-filename="${escapeAttr(it.filename)}" data-kind="${escapeAttr(kind)}">
          <div class="pending-header">
            <span class="pending-state pending-state-${it.state}">${it.state}</span>
            <code class="pending-path">${escapeHtml(it.path)}</code>
            <span class="pending-meta">${kb} kB · ${escapeHtml(it.mtime)}</span>
          </div>
          ${player}
          ${preview}
          <div class="pending-actions">
            ${editBtn}
            <button type="button" class="pending-copy" data-cmd="${escapeAttr(cmd)}">copy ingest cmd</button>
            <button type="button" class="pending-delete">delete</button>
          </div>
        </li>`;
    });
    listEl.innerHTML = `<ul class="pending-ul">${rows.join("")}</ul>`;
    listEl.querySelectorAll(".pending-delete").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        const row = ev.target.closest("[data-filename]");
        const fn = row?.dataset.filename;
        if (!fn) return;
        if (!confirm(`delete ${fn} from inbox/ ?`)) return;
        try {
          const res = await fetch(`/api/record/${encodeURIComponent(fn)}`, { method: "DELETE" });
          const json = await res.json().catch(() => ({}));
          if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
          document.dispatchEvent(new CustomEvent("recorder:deleted", { detail: json }));
        } catch (e) {
          alert(`delete failed: ${e.message || e}`);
        }
      });
    });
    listEl.querySelectorAll(".pending-copy").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        const cmd = ev.currentTarget.dataset.cmd;
        try {
          await navigator.clipboard.writeText(cmd);
          const prev = btn.textContent;
          btn.textContent = "copied";
          setTimeout(() => { btn.textContent = prev; }, 1200);
        } catch {
          btn.textContent = cmd;
        }
      });
    });
    listEl.querySelectorAll(".pending-edit").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        const row = ev.target.closest("[data-filename]");
        if (!row || row.querySelector(".pending-editor")) return;
        const rowKind = row.dataset.kind || "audio";
        const previewEl = row.querySelector(rowKind === "image" ? ".pending-instructions" : ".pending-transcript");
        const full = previewEl ? previewEl.dataset.full || "" : "";
        const rows = rowKind === "image" ? 3 : 8;
        const placeholder = rowKind === "image"
          ? "instructions for the ingest agent (e.g. extract only items and costs). leave blank for a full transcription."
          : "";
        const maxAttr = rowKind === "image" ? ` maxlength="500"` : "";
        const editor = document.createElement("div");
        editor.className = "pending-editor";
        editor.innerHTML = `
          <textarea class="pending-textarea" rows="${rows}"${maxAttr} spellcheck="true" placeholder="${escapeAttr(placeholder)}"></textarea>
          <div class="pending-editor-actions">
            <button type="button" class="pending-save">save</button>
            <button type="button" class="pending-cancel">cancel</button>
            <span class="pending-editor-status" hidden></span>
          </div>
        `;
        editor.querySelector(".pending-textarea").value = full;
        if (previewEl) previewEl.hidden = true;
        btn.hidden = true;
        row.insertBefore(editor, row.querySelector(".pending-actions"));
        const textarea = editor.querySelector(".pending-textarea");
        const saveBtn = editor.querySelector(".pending-save");
        const cancelBtn = editor.querySelector(".pending-cancel");
        const statusEl = editor.querySelector(".pending-editor-status");
        textarea.focus();
        cancelBtn.addEventListener("click", () => {
          editor.remove();
          if (previewEl) previewEl.hidden = false;
          btn.hidden = false;
        });
        saveBtn.addEventListener("click", async () => {
          const fn = row.dataset.filename;
          const saveUrl = rowKind === "image"
            ? `/api/image/${encodeURIComponent(fn)}/instructions`
            : rowKind === "text"
              ? `/api/text/${encodeURIComponent(fn)}`
              : `/api/record/${encodeURIComponent(fn)}/transcript`;
          const payload = rowKind === "image"
            ? { instructions: textarea.value }
            : { body: textarea.value };
          saveBtn.disabled = true;
          cancelBtn.disabled = true;
          statusEl.hidden = false;
          statusEl.textContent = "saving…";
          try {
            const res = await fetch(saveUrl, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            });
            const json = await res.json().catch(() => ({}));
            if (!res.ok || !json.ok) throw new Error(json.detail || `HTTP ${res.status}`);
            document.dispatchEvent(new CustomEvent("recorder:parsed", { detail: json }));
          } catch (e) {
            statusEl.textContent = `save failed: ${e.message || e}`;
            saveBtn.disabled = false;
            cancelBtn.disabled = false;
          }
        });
      });
    });
  }

  function truncate(s, n) {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  function escapeAttr(s) { return escapeHtml(s); }

  async function transcodeToWav(blob, targetRate) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error("no AudioContext");
    const ctx = new AC();
    let decoded;
    try {
      const buf = await blob.arrayBuffer();
      decoded = await ctx.decodeAudioData(buf.slice(0));
    } finally {
      try { await ctx.close(); } catch {}
    }
    // Mix down to mono at source rate, then resample to target rate via
    // OfflineAudioContext (uses the browser's polyphase resampler).
    const srcRate = decoded.sampleRate;
    const frames = decoded.length;
    const mono = new Float32Array(frames);
    for (let c = 0; c < decoded.numberOfChannels; c++) {
      const data = decoded.getChannelData(c);
      for (let i = 0; i < frames; i++) mono[i] += data[i];
    }
    if (decoded.numberOfChannels > 1) {
      const inv = 1 / decoded.numberOfChannels;
      for (let i = 0; i < frames; i++) mono[i] *= inv;
    }
    const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    let resampled = mono;
    if (OAC && srcRate !== targetRate) {
      const outFrames = Math.ceil((frames * targetRate) / srcRate);
      const off = new OAC(1, outFrames, targetRate);
      const monoBuf = off.createBuffer(1, frames, srcRate);
      monoBuf.getChannelData(0).set(mono);
      const src = off.createBufferSource();
      src.buffer = monoBuf;
      src.connect(off.destination);
      src.start(0);
      const rendered = await off.startRendering();
      resampled = rendered.getChannelData(0);
    }
    return encodeWav(resampled, targetRate);
  }

  function encodeWav(samples, rate) {
    const bytesPerSample = 2;
    const blockAlign = bytesPerSample;
    const byteRate = rate * blockAlign;
    const dataSize = samples.length * bytesPerSample;
    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);
    writeStr(view, 0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    writeStr(view, 8, "WAVE");
    writeStr(view, 12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, rate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, 16, true);
    writeStr(view, 36, "data");
    view.setUint32(40, dataSize, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buffer], { type: "audio/wav" });
  }

  function writeStr(view, off, str) {
    for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
  }
})();
