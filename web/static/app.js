(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  const pickBtn = document.getElementById("pickBtn");
  const targetKbInput = document.getElementById("targetKbInput");
  const presets = document.getElementById("presets");
  const errorBanner = document.getElementById("errorBanner");
  const emptyState = document.getElementById("emptyState");
  const jobList = document.getElementById("jobList");
  const summaryLine = document.getElementById("summaryLine");
  const zipBtn = document.getElementById("zipBtn");
  const clearBtn = document.getElementById("clearBtn");
  const serverPill = document.getElementById("serverPill");
  const serverStatusText = document.getElementById("serverStatusText");
  const maxUploadHint = document.getElementById("maxUploadHint");

  /** @type {Map<string, any>} */
  const jobs = new Map();
  /** @type {Map<string, EventSource>} */
  const streams = new Map();

  function formatSize(bytes) {
    if (bytes == null || Number.isNaN(bytes)) return "—";
    if (bytes >= 1_000_000) {
      return `${(bytes / 1_000_000).toFixed(2)} МБ (${Math.round(bytes / 1000).toLocaleString("uk-UA")} КБ)`;
    }
    return `${(bytes / 1000).toFixed(1)} КБ`;
  }

  function formatShortSize(bytes) {
    if (bytes == null || Number.isNaN(bytes)) return "—";
    if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(2)} МБ`;
    return `${(bytes / 1000).toFixed(1)} КБ`;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showError(msg) {
    if (!msg) {
      errorBanner.hidden = true;
      errorBanner.textContent = "";
      return;
    }
    errorBanner.textContent = msg;
    errorBanner.hidden = false;
  }

  async function loadConfig() {
    try {
      const res = await fetch("api/config");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const cfg = await res.json();
      targetKbInput.value = cfg.default_target_kb;
      targetKbInput.min = cfg.min_target_kb;
      targetKbInput.max = cfg.max_target_kb;
      maxUploadHint.textContent = `До ${cfg.max_upload_mb} МБ / файл · ${cfg.workers} воркер(и)`;
      serverPill.classList.remove("offline");
      serverPill.classList.add("online");
      serverStatusText.textContent = "Готовий до роботи";
    } catch {
      serverPill.classList.remove("online");
      serverPill.classList.add("offline");
      serverStatusText.textContent = "Бекенд недоступний";
    }
  }

  function updateSummary() {
    const all = Array.from(jobs.values());
    emptyState.hidden = all.length > 0;
    clearBtn.hidden = all.length === 0;

    if (all.length === 0) {
      summaryLine.textContent = "Файлів ще немає";
      zipBtn.hidden = true;
      return;
    }

    const done = all.filter((j) => j.status === "done");
    const active = all.filter((j) => j.status === "queued" || j.status === "processing");
    const totalOrig = done.reduce((acc, j) => acc + (j.orig_bytes || 0), 0);
    const totalOut = done.reduce((acc, j) => acc + (j.out_bytes || 0), 0);
    const savedPct = totalOrig > 0 ? ((1 - totalOut / totalOrig) * 100).toFixed(1) : "0.0";

    let text = `Всього: ${all.length} · Готово: ${done.length}`;
    if (active.length > 0) text += ` · В обробці: ${active.length}`;
    if (done.length > 0) {
      text += ` · ${formatShortSize(totalOrig)} → ${formatShortSize(totalOut)} (-${savedPct}%)`;
    }
    summaryLine.textContent = text;
    zipBtn.hidden = done.length < 2;
  }

  function renderJobCard(job) {
    let card = document.getElementById(`job-${job.id}`);
    if (!card) {
      card = document.createElement("article");
      card.id = `job-${job.id}`;
      card.className = "job-card";
      jobList.prepend(card);
    }

    let badgeHtml = `<span class="badge badge-queued">У черзі</span>`;
    if (job.status === "processing") {
      badgeHtml = `<span class="badge badge-processing"><span class="spinner"></span>Оптимізація...</span>`;
    } else if (job.status === "done") {
      badgeHtml = `<span class="badge badge-done">Готово · ${job.elapsed_sec ?? 0} с</span>`;
    } else if (job.status === "error") {
      badgeHtml = `<span class="badge badge-error">Помилка</span>`;
    }

    const pagesHtml = job.pages != null ? `<span class="metric">Сторінок: <strong>${job.pages}</strong></span>` : "";
    const origHtml = `<span class="metric">Було: <strong>${formatSize(job.orig_bytes)}</strong></span>`;
    let afterHtml = "";
    let savingHtml = "";

    if (job.status === "done") {
      afterHtml = `<span class="metric">Стало: <strong>${formatSize(job.out_bytes)}</strong></span>`;
      const pct = Number(job.compression_pct || 0);
      const sign = pct > 0 ? "-" : "";
      savingHtml = `<span class="saving-pill">${sign}${pct.toFixed(1)}%</span>`;
    }

    let bottomHtml = "";
    if (job.status === "done") {
      const strat = escapeHtml(job.strategy_label || job.strategy_id || "");
      const params = escapeHtml(job.params_summary || "");
      bottomHtml = `
        <div class="job-bottom">
          <div class="strategy-text" title="${params}">${strat} · ${params}</div>
          <a class="btn btn-primary" href="${escapeHtml(job.download_url)}" download="${escapeHtml(job.filename)}">
            Завантажити PDF
          </a>
        </div>
      `;
    } else if (job.status === "error") {
      bottomHtml = `
        <div class="job-bottom">
          <div class="error-text">${escapeHtml(job.error || "Невідома помилка")}</div>
        </div>
      `;
    }

    card.innerHTML = `
      <div class="job-top">
        <div class="job-name">${escapeHtml(job.filename)}</div>
        ${badgeHtml}
      </div>
      <div class="job-metrics">
        ${pagesHtml}
        ${origHtml}
        ${afterHtml}
        ${savingHtml}
      </div>
      ${bottomHtml}
    `;
  }

  function watchJob(jobId) {
    if (streams.has(jobId)) return;
    const es = new EventSource(`api/jobs/${encodeURIComponent(jobId)}/events`);
    streams.set(jobId, es);

    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        jobs.set(data.id, data);
        renderJobCard(data);
        updateSummary();
        if (data.status === "done" || data.status === "error") {
          es.close();
          streams.delete(jobId);
        }
      } catch {
        // ignore malformed keepalive
      }
    };

    es.onerror = () => {
      es.close();
      streams.delete(jobId);
      // Fallback single poll in case SSE dropped behind proxy
      setTimeout(async () => {
        const current = jobs.get(jobId);
        if (!current || current.status === "done" || current.status === "error") return;
        try {
          const r = await fetch(`api/jobs/${encodeURIComponent(jobId)}`);
          if (!r.ok) return;
          const data = await r.json();
          jobs.set(data.id, data);
          renderJobCard(data);
          updateSummary();
          if (data.status !== "done" && data.status !== "error") {
            watchJob(jobId);
          }
        } catch {
          // ignore
        }
      }, 1500);
    };
  }

  async function uploadFiles(fileList) {
    showError("");
    const pdfs = Array.from(fileList || []).filter(
      (f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
    );
    if (pdfs.length === 0) {
      showError("Оберіть хоча б один PDF-файл.");
      return;
    }

    const fd = new FormData();
    for (const f of pdfs) {
      fd.append("files", f, f.name);
    }
    const targetKb = parseInt(targetKbInput.value, 10);
    if (!Number.isNaN(targetKb) && targetKb > 0) {
      fd.append("target_kb", String(targetKb));
    }

    pickBtn.disabled = true;
    try {
      const res = await fetch("api/jobs", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showError(data.detail || `Помилка завантаження (HTTP ${res.status})`);
        return;
      }
      for (const job of data.jobs || []) {
        jobs.set(job.id, job);
        renderJobCard(job);
        watchJob(job.id);
      }
      updateSummary();
    } catch (err) {
      showError(`Не вдалося надіслати файли: ${err.message}`);
    } finally {
      pickBtn.disabled = false;
      fileInput.value = "";
    }
  }

  // Event listeners
  pickBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length > 0) {
      uploadFiles(fileInput.files);
    }
  });

  ["dragenter", "dragover"].forEach((evName) => {
    dropzone.addEventListener(evName, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((evName) => {
    dropzone.addEventListener(evName, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length > 0) {
      uploadFiles(e.dataTransfer.files);
    }
  });

  presets.addEventListener("click", (e) => {
    const btn = e.target.closest(".chip");
    if (!btn) return;
    targetKbInput.value = btn.dataset.kb;
    presets.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    btn.classList.add("active");
  });

  targetKbInput.addEventListener("input", () => {
    const val = targetKbInput.value;
    presets.querySelectorAll(".chip").forEach((c) => {
      c.classList.toggle("active", c.dataset.kb === val);
    });
  });

  zipBtn.addEventListener("click", async () => {
    const doneIds = Array.from(jobs.values())
      .filter((j) => j.status === "done")
      .map((j) => j.id);
    if (doneIds.length === 0) return;

    zipBtn.disabled = true;
    try {
      const res = await fetch("api/download-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: doneIds }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "optimized_pdfs.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showError(`Не вдалося сформувати ZIP: ${err.message}`);
    } finally {
      zipBtn.disabled = false;
    }
  });

  clearBtn.addEventListener("click", () => {
    for (const es of streams.values()) es.close();
    streams.clear();
    jobs.clear();
    jobList.innerHTML = "";
    showError("");
    updateSummary();
  });

  loadConfig();
})();

