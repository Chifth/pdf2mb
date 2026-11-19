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
  const themeToggle = document.getElementById("themeToggle");

  const overallProgressWrap = document.getElementById("overallProgressWrap");
  const overallProgressText = document.getElementById("overallProgressText");
  const overallProgressPct = document.getElementById("overallProgressPct");
  const overallProgressFill = document.getElementById("overallProgressFill");

  /** @type {Map<string, any>} */
  const jobs = new Map();
  /** @type {Map<string, EventSource>} */
  const streams = new Map();
  let uploadState = { active: false, pct: 0, count: 0 };
  let seqCounter = 0;

  const STATUS_RANK = {
    processing: 0,
    queued: 1,
    error: 2,
    done: 3,
  };

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

  // Theme switcher
  function initTheme() {
    if (!themeToggle) return;
    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("pdf2mb_theme", next);
      } catch (_) {}
    });
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
    } catch {
      // Handled by pollServerStatus
    }
  }

  async function pollServerStatus() {
    try {
      const res = await fetch("api/status", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const st = await res.json();
      serverPill.classList.remove("offline", "online", "busy");
      if (!st.ready) {
        serverPill.classList.add("offline");
        serverStatusText.textContent = "Запуск сервісу...";
        return;
      }
      const activeTotal = Number(st.active_total || 0);
      if (activeTotal > 0) {
        serverPill.classList.add("busy");
        serverStatusText.textContent = `Документів в черзі: ${activeTotal}`;
      } else {
        serverPill.classList.add("online");
        serverStatusText.textContent = "Готовий до роботи";
      }
    } catch {
      serverPill.classList.remove("online", "busy");
      serverPill.classList.add("offline");
      serverStatusText.textContent = "Бекенд недоступний";
    }
  }

  function updateOverallProgress(all) {
    if (uploadState.active) {
      overallProgressWrap.hidden = false;
      emptyState.hidden = true;
      overallProgressText.textContent = `Завантаження файлів (${uploadState.count}) на сервер...`;
      overallProgressPct.textContent = `${uploadState.pct}%`;
      overallProgressFill.style.width = `${Math.max(4, uploadState.pct)}%`;
      overallProgressFill.classList.add("is-active");
      overallProgressFill.classList.remove("is-complete");
      return;
    }

    if (all.length === 0) {
      overallProgressWrap.hidden = true;
      overallProgressFill.style.width = "0%";
      overallProgressFill.classList.remove("is-active", "is-complete");
      return;
    }

    overallProgressWrap.hidden = false;
    const finished = all.filter((j) => j.status === "done" || j.status === "error").length;
    const processing = all.filter((j) => j.status === "processing").length;
    const queued = all.filter((j) => j.status === "queued").length;

    const rawRatio = (finished + processing * 0.35) / all.length;
    const pct = finished === all.length ? 100 : Math.min(97, Math.max(4, Math.round(rawRatio * 100)));

    if (finished === all.length) {
      const doneCount = all.filter((j) => j.status === "done").length;
      overallProgressText.textContent = `Опрацювання завершено · ${doneCount} з ${all.length} готово`;
      overallProgressPct.textContent = "100%";
      overallProgressFill.style.width = "100%";
      overallProgressFill.classList.remove("is-active");
      overallProgressFill.classList.add("is-complete");
    } else {
      overallProgressText.textContent = `В роботі: ${processing} · У черзі: ${queued} · Готово: ${finished}/${all.length}`;
      overallProgressPct.textContent = `${pct}%`;
      overallProgressFill.style.width = `${pct}%`;
      overallProgressFill.classList.add("is-active");
      overallProgressFill.classList.remove("is-complete");
    }
  }

  function updateSummary() {
    const all = Array.from(jobs.values());
    emptyState.hidden = all.length > 0 || uploadState.active;
    clearBtn.hidden = all.length === 0;

    updateOverallProgress(all);

    if (all.length === 0) {
      summaryLine.textContent = uploadState.active ? "Завантаження файлів..." : "Файлів ще немає";
      zipBtn.hidden = true;
      return;
    }

    const done = all.filter((j) => j.status === "done");
    const active = all.filter((j) => j.status === "queued" || j.status === "processing");
    const totalOrig = done.reduce((acc, j) => acc + (j.orig_bytes || 0), 0);
    const totalOut = done.reduce((acc, j) => acc + (j.out_bytes || 0), 0);
    const savedPct = totalOrig > 0 ? ((1 - totalOut / totalOrig) * 100).toFixed(1) : "0.0";

    let text = `Всього: ${all.length} · Готово: ${done.length}`;
    if (active.length > 0) text += ` · Активних: ${active.length}`;
    if (done.length > 0) {
      text += ` · ${formatShortSize(totalOrig)} → ${formatShortSize(totalOut)} (-${savedPct}%)`;
    }
    summaryLine.textContent = text;
    zipBtn.hidden = done.length < 2;
  }

  function sortJobCards() {
    const sorted = Array.from(jobs.values()).sort((a, b) => {
      const ra = STATUS_RANK[a.status] ?? 99;
      const rb = STATUS_RANK[b.status] ?? 99;
      if (ra !== rb) return ra - rb;
      // Active/queued keep FIFO order; finished/done show latest first inside bottom block
      if (a.status === "done" || a.status === "error") {
        return (b._seq || 0) - (a._seq || 0);
      }
      return (a._seq || 0) - (b._seq || 0);
    });

    for (const job of sorted) {
      const el = document.getElementById(`job-${job.id}`);
      if (el) jobList.appendChild(el);
    }
  }

  function renderJobCard(job) {
    if (job._seq == null) {
      const prev = jobs.get(job.id);
      job._seq = prev && prev._seq != null ? prev._seq : ++seqCounter;
    }

    let card = document.getElementById(`job-${job.id}`);
    if (!card) {
      card = document.createElement("article");
      card.id = `job-${job.id}`;
      card.className = "job-card";
      jobList.appendChild(card);
    }

    card.classList.toggle("is-processing", job.status === "processing");
    card.classList.toggle("is-queued", job.status === "queued");
    card.classList.toggle("is-done", job.status === "done");
    card.classList.toggle("is-error", job.status === "error");

    let badgeHtml = `<span class="badge badge-queued">У черзі</span>`;
    if (job.status === "processing") {
      badgeHtml = `<span class="badge badge-processing"><span class="spinner"></span>Оптимізація...</span>`;
    } else if (job.status === "done") {
      badgeHtml = `<span class="badge badge-done">${job.elapsed_sec ?? 0} с</span>`;
    } else if (job.status === "error") {
      badgeHtml = `<span class="badge badge-error">Помилка</span>`;
    }

    const downloadBtnHtml =
      job.status === "done"
        ? `<a class="btn btn-primary btn-sm" href="${escapeHtml(job.download_url)}" download="${escapeHtml(job.filename)}">Завантажити</a>`
        : "";

    const metaItems = [];
    if (job.pages != null) {
      metaItems.push(`<span class="meta-item">${job.pages} стор.</span>`);
    }
    if (job.status === "done") {
      const pct = Number(job.compression_pct || 0);
      const sign = pct > 0 ? "-" : "";
      metaItems.push(
        `<span class="meta-item">${formatShortSize(job.orig_bytes)} → <strong>${formatShortSize(job.out_bytes)}</strong></span>`
      );
      metaItems.push(`<span class="saving-pill">${sign}${pct.toFixed(1)}%</span>`);
      if (job.strategy_label || job.strategy_id) {
        const strat = escapeHtml(job.strategy_label || job.strategy_id);
        const params = escapeHtml(job.params_summary || "");
        metaItems.push(`<span class="meta-strategy" title="${params}">${strat}</span>`);
      }
    } else {
      metaItems.push(`<span class="meta-item">${formatShortSize(job.orig_bytes)}</span>`);
    }

    const errorHtml =
      job.status === "error"
        ? `<div class="error-text">${escapeHtml(job.error || "Невідома помилка")}</div>`
        : "";

    card.innerHTML = `
      <div class="job-main-row">
        <div class="job-title-wrap">
          <span class="file-icon" aria-hidden="true">PDF</span>
          <div class="job-name">${escapeHtml(job.filename)}</div>
        </div>
        <div class="job-actions-right">
          ${badgeHtml}
          ${downloadBtnHtml}
        </div>
      </div>
      <div class="job-meta-row">
        ${metaItems.join("")}
      </div>
      ${errorHtml}
    `;

    sortJobCards();
  }

  function watchJob(jobId) {
    if (streams.has(jobId)) return;
    const es = new EventSource(`api/jobs/${encodeURIComponent(jobId)}/events`);
    streams.set(jobId, es);

    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const prev = jobs.get(data.id);
        if (prev && prev._seq != null) data._seq = prev._seq;
        jobs.set(data.id, data);
        renderJobCard(data);
        updateSummary();
        if (data.status === "done" || data.status === "error") {
          es.close();
          streams.delete(jobId);
          pollServerStatus();
        }
      } catch {
        // ignore keepalive
      }
    };

    es.onerror = () => {
      es.close();
      streams.delete(jobId);
      setTimeout(async () => {
        const current = jobs.get(jobId);
        if (!current || current.status === "done" || current.status === "error") return;
        try {
          const r = await fetch(`api/jobs/${encodeURIComponent(jobId)}`);
          if (!r.ok) return;
          const data = await r.json();
          if (current._seq != null) data._seq = current._seq;
          jobs.set(data.id, data);
          renderJobCard(data);
          updateSummary();
          if (data.status !== "done" && data.status !== "error") {
            watchJob(jobId);
          } else {
            pollServerStatus();
          }
        } catch {
          // ignore
        }
      }, 1500);
    };
  }

  function postJobsWithProgress(formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "api/jobs", true);
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable && ev.total > 0) {
          onProgress(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = () => {
        let data = {};
        try {
          data = JSON.parse(xhr.responseText || "{}");
        } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(data);
        } else {
          reject(new Error(data.detail || `Помилка завантаження (HTTP ${xhr.status})`));
        }
      };
      xhr.onerror = () => reject(new Error("Помилка мережі під час завантаження файлів."));
      xhr.send(formData);
    });
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
    uploadState = { active: true, pct: 0, count: pdfs.length };
    updateSummary();

    try {
      const data = await postJobsWithProgress(fd, (pct) => {
        uploadState.pct = pct;
        updateSummary();
      });
      uploadState = { active: false, pct: 100, count: 0 };

      for (const job of data.jobs || []) {
        job._seq = ++seqCounter;
        jobs.set(job.id, job);
        renderJobCard(job);
        watchJob(job.id);
      }
      updateSummary();
      pollServerStatus();
    } catch (err) {
      uploadState = { active: false, pct: 0, count: 0 };
      showError(err.message || "Не вдалося надіслати файли.");
      updateSummary();
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

  initTheme();
  loadConfig();
  pollServerStatus();
  setInterval(pollServerStatus, 2500);
})();
