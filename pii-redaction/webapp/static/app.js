/* PII Redactor — front end behaviour.
   Progress comes from the pipeline's own stage events over SSE, so the bars
   track real work rather than an invented timer. */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  };

  const LABEL_TEXT = {
    PERSON: "People", ORG: "Companies", LOCATION: "Places", ADDRESS: "Addresses",
    EMAIL: "Emails", PHONE: "Phones", URL: "URLs", NATIONAL_ID: "National IDs",
    BANK_ACCOUNT: "Bank accounts", SSN: "SSNs", CREDIT_CARD: "Cards",
    DOB: "Dates of birth", IP_ADDRESS: "IP addresses",
  };

  const IMAGE_TAG = {
    IMAGE_ID_DOCUMENT: ["Identity document", "id"],
    IMAGE_SIGNATURE: ["Signature", "sig"],
    IMAGE_CODE: ["QR / barcode", ""],
    IMAGE_LOGO: ["Company logo", ""],
    IMAGE_UNCLASSIFIED: ["Unclassified", ""],
    IMAGE_CLEAN: ["Kept", "clean"],
  };

  let source = null;
  let timer = null;
  let startedAt = 0;
  let currentJob = null;
  let previewLoaded = false;

  /* ---------- toast ------------------------------------------------------ */
  let toastTimer = null;
  function toast(message, bad) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("bad", !!bad);
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 5200);
  }

  /* ---------- theme ------------------------------------------------------ */
  const root = document.documentElement;
  const saved = localStorage.getItem("pii-theme");
  if (saved) root.setAttribute("data-theme", saved);
  $("#theme-toggle").addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("pii-theme", next);
  });

  /* ---------- animated numbers ------------------------------------------ */
  function countUp(node, target, decimals, suffix) {
    const duration = 1100;
    const t0 = performance.now();
    const value = Number.isFinite(target) ? target : 0;
    const final = value.toFixed(decimals || 0) + (suffix || "");

    function tick(now) {
      // Clamp at BOTH ends: a clock that reports a time before t0 (headless
      // virtual time, a suspended tab) would otherwise drive the eased factor
      // negative and render "-0" or "-3" on the way up.
      const p = Math.max(0, Math.min(1, (now - t0) / duration));
      const eased = 1 - Math.pow(1 - p, 3);
      node.textContent = (value * eased).toFixed(decimals || 0) + (suffix || "");
      if (p < 1) requestAnimationFrame(tick);
      else node.textContent = final;
    }
    requestAnimationFrame(tick);
    // If rAF never runs (throttled or backgrounded tab) the number must still
    // end up correct rather than frozen at zero.
    setTimeout(() => { node.textContent = final; }, duration + 250);
  }

  function revealNow(node) {
    node.classList.add("in");
    node.querySelectorAll("[data-count]").forEach((n) => {
      if (n.dataset.done) return;
      n.dataset.done = "1";
      countUp(n, parseFloat(n.dataset.count), parseInt(n.dataset.decimals || "0", 10), n.dataset.suffix || "");
    });
  }

  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      revealNow(entry.target);
      io.unobserve(entry.target);
    });
  }, { threshold: 0.15, rootMargin: "0px 0px -5% 0px" });

  const revealed = [];
  document.querySelectorAll(".panel, .hero, #stat-strip").forEach((n) => {
    if (n.id !== "run-panel") n.classList.add("reveal");
    revealed.push(n);
    io.observe(n);
  });

  // Fail-safe: content must never stay invisible because an observer did not
  // fire (headless render, restored scroll position, unsupported browser).
  setTimeout(() => revealed.forEach(revealNow), 1600);

  /* ---------- dropzone --------------------------------------------------- */
  const dropzone = $("#dropzone");
  const fileInput = $("#file-input");

  dropzone.addEventListener("mousemove", (event) => {
    const box = dropzone.getBoundingClientRect();
    const glow = dropzone.querySelector(".dz-glow");
    glow.style.left = `${event.clientX - box.left}px`;
    glow.style.top = `${event.clientY - box.top}px`;
  });

  ["dragenter", "dragover"].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove("dragging"); }));

  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) upload(file);
  });
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); }
  });
  $("#choose-btn").addEventListener("click", (e) => { e.preventDefault(); fileInput.click(); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) upload(fileInput.files[0]); });
  function setBusy(busy) {
    $("#choose-btn").disabled = busy;
    $("#sample-btn").disabled = busy;
  }

  /* ---------- dialogs ------------------------------------------------------ */
  // One modal open at a time, closed by ✕, the scrim or Escape. Body scroll is
  // locked while open so the page behind cannot drift.
  let openModal = null;

  function showModal(id) {
    if (openModal) hideModal();
    openModal = $(id);
    $("#scrim").hidden = false;
    openModal.hidden = false;
    document.body.style.overflow = "hidden";
    const focusable = openModal.querySelector("button, a[href]");
    if (focusable) focusable.focus({ preventScroll: true });
  }

  function hideModal() {
    if (openModal) openModal.hidden = true;
    openModal = null;
    $("#scrim").hidden = true;
    document.body.style.overflow = "";
  }

  $("#scrim").addEventListener("click", hideModal);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openModal) hideModal();
  });
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", hideModal));

  /* ---------- sample library ---------------------------------------------- */
  let libraryLoaded = false;
  let chosenSample = null;

  $("#sample-btn").addEventListener("click", async (e) => {
    e.preventDefault();
    showModal("#modal-library");
    if (libraryLoaded) return;
    try {
      const { samples } = await (await fetch("/api/library")).json();
      const grid = $("#library-grid");
      grid.innerHTML = "";
      if (!samples.length) { grid.append(el("p", "empty", "No sample documents are bundled here.")); return; }
      samples.forEach((sample, i) => grid.append(sampleCard(sample, i)));
      libraryLoaded = true;
    } catch (_) {
      $("#library-grid").innerHTML = "";
      $("#library-grid").append(el("p", "empty", "Could not load the sample library."));
    }
  });

  function sampleCard(sample, index) {
    const card = el("button", "sample");
    card.type = "button";
    card.style.animationDelay = `${index * 55}ms`;
    card.append(el("div", "sample-name", escapeHtml(sample.name)));
    if (!sample.synthetic) card.append(el("span", "sample-flag", "real document"));
    card.append(el("p", "sample-blurb", escapeHtml(sample.blurb)));
    const tags = el("div", "sample-tags");
    (sample.highlights || []).forEach((h) => tags.append(el("span", "chip", escapeHtml(h))));
    tags.append(el("span", "chip size", `${sample.kb} KB`));
    card.append(tags);
    card.addEventListener("click", () => openSample(sample));
    return card;
  }

  /* ---------- preview a library document before running -------------------- */
  async function openSample(sample) {
    chosenSample = sample;
    $("#doc-title").textContent = sample.name;
    $("#doc-sub").textContent = "Original document, exactly as the tool will read it.";
    const body = $("#doc-single").querySelector(".doc-body");
    body.innerHTML = "";
    body.append(el("p", "doc-more", "Loading…"));
    showModal("#modal-doc");
    try {
      const data = await (await fetch(`/api/library/${encodeURIComponent(sample.id)}/preview`)).json();
      fillDoc($("#doc-single"), data);
    } catch (_) {
      body.innerHTML = "";
      body.append(el("p", "doc-more", "Could not read that document."));
    }
  }

  $("#doc-back").addEventListener("click", () => showModal("#modal-library"));

  $("#doc-redact").addEventListener("click", async () => {
    if (!chosenSample) return;
    $("#doc-redact").disabled = true;
    try {
      const response = await fetch(`/api/sample/${encodeURIComponent(chosenSample.id)}`, { method: "POST" });
      if (!response.ok) throw new Error((await response.json()).detail || "Sample unavailable");
      const job = await response.json();
      hideModal();
      setBusy(true);
      begin(job);
    } catch (error) {
      toast(error.message, true);
    } finally {
      $("#doc-redact").disabled = false;
    }
  });

  async function upload(file) {
    if (!file.name.toLowerCase().endsWith(".docx")) { toast("Only .docx files are supported.", true); return; }
    setBusy(true);
    const body = new FormData();
    body.append("file", file);
    try {
      const response = await fetch("/api/redact", { method: "POST", body });
      if (!response.ok) throw new Error((await response.json()).detail || "Upload failed");
      begin(await response.json());
    } catch (error) { setBusy(false); toast(error.message, true); }
  }

  /* ---------- run -------------------------------------------------------- */
  function begin(job) {
    $("#results").hidden = true;
    $("#done-state").hidden = true;
    $("#keys-panel").hidden = true;
    previewLoaded = false;
    $("#pick-state").hidden = true;
    $("#run-panel").hidden = false;
    $("#run-name").textContent = job.name;
    $("#run-panel").scrollIntoView({ behavior: "smooth", block: "center" });

    const list = $("#stages");
    list.innerHTML = "";
    job.stages.forEach((stage) => {
      const li = el("li", "stage");
      li.dataset.key = stage.key;
      li.append(
        el("div", "pip", "✓"),
        el("div", "label", stage.label),
        el("div", "detail", ""),
        el("div", "bar", "<i></i>")
      );
      list.append(li);
    });

    startedAt = performance.now();
    clearInterval(timer);
    timer = setInterval(() => {
      $("#run-elapsed").textContent = ((performance.now() - startedAt) / 1000).toFixed(1) + "s";
    }, 100);

    if (source) source.close();
    source = new EventSource(`/api/events/${job.id || job.job}`);
    source.addEventListener("stage", (event) => onStage(JSON.parse(event.data)));
    source.addEventListener("done", (event) => finish(JSON.parse(event.data).result, job));
    source.addEventListener("error", (event) => {
      let message = "The run failed.";
      try { message = JSON.parse(event.data).message || message; } catch (_) {}
      stop();
      toast(message, true);
    });
  }

  function onStage(data) {
    const list = [...document.querySelectorAll(".stage")];
    const index = list.findIndex((n) => n.dataset.key === data.stage);
    if (index < 0) return;
    list.forEach((node, i) => {
      if (i < index) { node.classList.add("done"); node.classList.remove("active"); node.querySelector("i").style.width = "100%"; }
    });
    const node = list[index];
    node.classList.add("active");
    node.querySelector("i").style.width = `${Math.round((data.fraction || 0) * 100)}%`;
    if (data.detail) node.querySelector(".detail").textContent = data.detail;
    if (data.fraction >= 1) { node.classList.add("done"); node.classList.remove("active"); }
  }

  function stop() {
    clearInterval(timer);
    setBusy(false);
    if (source) { source.close(); source = null; }
  }

  /* ---------- results ---------------------------------------------------- */
  function finish(result, job) {
    document.querySelectorAll(".stage").forEach((node) => {
      node.classList.add("done"); node.classList.remove("active");
      node.querySelector("i").style.width = "100%";
    });
    stop();
    render(result, job.id || job.job);
  }

  function render(result, jobId, scroll = true) {
    const results = $("#results");
    results.hidden = false;
    currentJob = jobId;

    // The upload slot becomes the result slot: the download appears exactly
    // where the file was chosen, and the cross puts the picker back.
    $("#pick-state").hidden = true;
    $("#run-panel").hidden = true;
    $("#done-state").hidden = false;

    const leaks = result.leaks || [];
    $("#done-shield").parentElement.classList.toggle("bad", leaks.length > 0);
    $("#done-title").textContent = leaks.length ? "Leaks detected" : "Redacted";
    $("#done-text").textContent = leaks.length
      ? `${leaks.length} redacted value(s) still appear in the output — for example "${leaks[0].value}".`
      : `${result.name} — no redacted value survives anywhere in the output.`;

    [["#dl-doc", "document"], ["#dl-map", "mapping"], ["#dl-det", "detections"]].forEach(([sel, kind]) => {
      const link = $(sel);
      link.href = `/api/download/${jobId}/${kind}`;
      link.setAttribute("download", "");
    });

    // counters
    const counters = $("#counters");
    counters.innerHTML = "";
    const report = result.report || {};
    const byType = report.detections_by_type || {};
    const cards = [
      ["Total replacements", report.total_detections || 0],
      ...Object.entries(byType).map(([k, v]) => [LABEL_TEXT[k] || k, v]),
      ["Images replaced", report.images_replaced || 0],
      ["Seconds", result.seconds || 0],
    ];
    cards.forEach(([label, value], i) => {
      const card = el("div", "counter");
      card.style.animationDelay = `${i * 45}ms`;
      const b = el("b", null, "0");
      card.append(b, el("span", null, label));
      counters.append(card);
      setTimeout(() => countUp(b, value, String(value).includes(".") ? 1 : 0, ""), i * 45);
    });

    // images
    const images = (result.images || []).filter((img) => img.before || img.after);
    $("#images-panel").hidden = images.length === 0;
    const grid = $("#image-grid");
    grid.innerHTML = "";
    images.forEach((img, i) => grid.append(shotCard(img, i)));

    // diff
    const samples = result.samples || [];
    $("#diff-panel").hidden = samples.length === 0;
    const diff = $("#diff-list");
    diff.innerHTML = "";
    samples.forEach((sample, i) => {
      const row = el("div", "diff-row");
      row.style.animationDelay = `${i * 30}ms`;
      row.append(
        el("span", "k", LABEL_TEXT[sample.label] || sample.label),
        el("span", "from", escapeHtml(sample.original)),
        el("span", "arrow", "→"),
        el("span", "to", escapeHtml(sample.replacement))
      );
      diff.append(row);
    });

    // Land on the download, not on the statistics below it: the button now sits
    // where the file was chosen, and scrolling past it would hide the one thing
    // the visitor came back for.
    if (scroll) $("#drop-panel").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function shotCard(img, index) {
    const [text, cls] = IMAGE_TAG[img.label] || [img.label, ""];
    const card = el("div", "shot");
    card.style.animationDelay = `${index * 55}ms`;

    const compare = el("div", "compare");
    if (img.before) {
      const before = new Image();
      before.src = `data:image/jpeg;base64,${img.before}`;
      before.alt = "Original image";
      compare.append(before);
    }
    const afterWrap = el("div", "after-wrap");
    if (img.after) {
      const after = new Image();
      after.src = `data:image/jpeg;base64,${img.after}`;
      after.alt = "Redacted image";
      afterWrap.append(after);
    }
    compare.append(afterWrap, el("div", "handle"),
      el("span", "tagline tag-before", "Original"), el("span", "tagline tag-after", "Redacted"));

    let dragging = false;
    const setSplit = (clientX) => {
      const box = compare.getBoundingClientRect();
      const pct = Math.max(0, Math.min(100, ((clientX - box.left) / box.width) * 100));
      afterWrap.style.clipPath = `inset(0 0 0 ${pct}%)`;
      compare.querySelector(".handle").style.left = `${pct}%`;
    };
    compare.addEventListener("pointerdown", (e) => { dragging = true; compare.setPointerCapture(e.pointerId); setSplit(e.clientX); });
    compare.addEventListener("pointermove", (e) => { if (dragging) setSplit(e.clientX); });
    compare.addEventListener("pointerup", () => { dragging = false; });
    compare.addEventListener("pointercancel", () => { dragging = false; });

    const meta = el("div", "shot-meta");
    meta.append(el("span", `tag ${cls}`, text));
    meta.append(el("div", "why", escapeHtml(img.reason || "")));
    meta.append(el("div", "ev", escapeHtml(img.evidence || "")));

    card.append(compare, meta);
    return card;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ---------- reset ------------------------------------------------------- */
  function reset() {
    stop();
    hideModal();
    currentJob = null;
    previewLoaded = false;
    $("#results").hidden = true;
    $("#done-state").hidden = true;
    $("#run-panel").hidden = true;
    $("#keys-panel").hidden = true;
    $("#pick-state").hidden = false;
    fileInput.value = "";
    $("#drop-panel").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  $("#reset-btn").addEventListener("click", reset);

  // Downloading the redacted document is the end of the task, so the page
  // returns to a clean picker — after a beat, so the browser has started the
  // download and the visitor sees why the screen changed.
  $("#dl-doc").addEventListener("click", () => {
    toast("Downloading — clearing this run.");
    setTimeout(reset, 1800);
  });

  /* ---------- audit files toggle ----------------------------------------- */
  $("#toggle-keys").addEventListener("click", () => {
    const panel = $("#keys-panel");
    panel.hidden = !panel.hidden;
  });

  /* ---------- before / after dialog ---------------------------------------- */
  $("#preview-btn").addEventListener("click", async () => {
    if (!currentJob) return;
    showModal("#modal-diff");
    if (previewLoaded) return;
    try {
      const [source, redacted] = await Promise.all([
        fetch(`/api/preview/${currentJob}/source`).then((r) => r.json()),
        fetch(`/api/preview/${currentJob}/redacted`).then((r) => r.json()),
      ]);
      fillDoc($("#doc-source"), source);
      fillDoc($("#doc-redacted"), redacted);
      previewLoaded = true;
    } catch (_) {
      toast("Could not load the preview.", true);
    }
  });

  function fillDoc(column, data) {
    const body = column.querySelector(".doc-body");
    body.innerHTML = "";
    // Images go in one wrapping strip. Stacked full-width they pushed every
    // paragraph below the fold, so the document looked empty until you scrolled.
    if ((data.images || []).length) {
      const strip = el("div", "doc-figs");
      data.images.forEach((b64) => {
        const img = new Image();
        img.src = `data:image/jpeg;base64,${b64}`;
        img.className = "doc-img";
        img.alt = "";
        strip.append(img);
      });
      body.append(strip);
    }
    (data.blocks || []).forEach((block) => body.append(el("p", null, escapeHtml(block.text))));
    if (data.truncated) {
      body.append(el("p", "doc-more",
        `… showing the first ${data.blocks.length} of ${data.total} blocks`));
    }
  }

  // Keep the two columns in step while scrolling.
  let syncing = false;
  ["#doc-source", "#doc-redacted"].forEach((sel) => {
    $(sel).addEventListener("scroll", (event) => {
      if (syncing) return;
      syncing = true;
      const other = $(sel === "#doc-source" ? "#doc-redacted" : "#doc-source");
      const el2 = event.currentTarget;
      const ratio = el2.scrollTop / Math.max(1, el2.scrollHeight - el2.clientHeight);
      other.scrollTop = ratio * (other.scrollHeight - other.clientHeight);
      requestAnimationFrame(() => { syncing = false; });
    });
  });

  document.querySelectorAll(".preview-toggle .seg").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".preview-toggle .seg").forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      $("#preview-wrap").dataset.view = button.dataset.view;
    });
  });
})();
