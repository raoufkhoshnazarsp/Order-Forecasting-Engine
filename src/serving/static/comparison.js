/* Comparison dashboard — vanilla JS, no build step. */
(function () {
  "use strict";

  const form = document.getElementById("comparison-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file");
  const fileName = document.getElementById("file-name");
  const checkBtn = document.getElementById("check-btn");
  const submitBtn = document.getElementById("submit-btn");
  const approveBtn = document.getElementById("approve-btn");
  const gate = document.getElementById("quality-gate");
  const gateStatus = document.getElementById("quality-status");
  const gateSummary = document.getElementById("quality-summary");
  const gatePlan = document.getElementById("quality-plan");
  const gateChecks = document.getElementById("quality-checks");
  const gateHint = document.getElementById("quality-hint");
  const errorBox = document.getElementById("error-box");
  const progress = document.getElementById("progress");
  const progressText = progress.querySelector("span");
  const results = document.getElementById("results");
  const statusPill = document.getElementById("status-pill");

  const fa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
  const fa1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });

  const VERTICAL_LABELS = {
    DIGITAL: "دیجیتال",
    FASHION: "مد و پوشاک",
    FMCG: "کالای مصرفی",
    GOLD: "طلا",
    "H&B": "سلامت و زیبایی",
    HOME: "خانه",
    SERVICES: "خدمات",
    "SNAPP-VENTURE": "اسنپ ونچر",
    "SNAPP-PAY": "اسنپ پی",
    VERTICALS: "جمع ورتیکال‌ها",
  };
  const SCENARIO_LABELS = {
    pessimistic: "بدبینانه",
    realistic: "واقع‌بینانه",
    optimistic: "خوش‌بینانه",
  };
  const METRIC_LABELS = { orders: "سفارش", gmv: "GMV", aov: "AOV" };

  let qualityToken = null;
  let qualityFileKey = null;
  let payload = null;
  let pastMetric = "orders";
  let chartMetric = "orders";
  let scenarioMonth = null;

  const MONTH_SCOPE = { current: "باقی این ماه", next: "ماه آینده" };

  function scenarioMonths(data) {
    if ((data.scenario_months || []).length) return data.scenario_months;
    return data.next_month
      ? [{ month: data.next_month, scope: "next", days: null }]
      : [];
  }

  function scenarioTotal(data, month, scenario) {
    return (data.scenarios || []).find(
      (r) => r.month === month && r.vertical === "VERTICALS" && r.scenario === scenario
    );
  }

  function methodValue() {
    const select = document.getElementById("payment-method");
    return select ? select.value : "";
  }

  function formatNumber(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return fa.format(value);
  }

  function formatMetric(value, metric) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    if (metric === "aov") return fa.format(value);
    return fa.format(value);
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function setBusy(on, message) {
    progress.hidden = !on;
    if (progressText) progressText.textContent = message || "در حال پردازش…";
    checkBtn.disabled = on;
    submitBtn.disabled = on || !qualityToken;
  }

  function resetGate() {
    qualityToken = null;
    qualityFileKey = null;
    gate.hidden = true;
    gateChecks.innerHTML = "";
    gatePlan.textContent = "";
    approveBtn.hidden = true;
    gateHint.textContent = "";
    submitBtn.disabled = true;
  }

  function setFile(file) {
    if (!file) return;
    if (!/\.(xlsx|xlsm|xls)$/i.test(file.name)) {
      showError("فقط فایل اکسل با پسوند xlsx، xlsm یا xls پذیرفته می‌شود.");
      return;
    }
    clearError();
    fileName.textContent = file.name;
    fileName.hidden = false;
    resetGate();
  }

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));

  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (file) {
      fileInput.files = event.dataTransfer.files;
      setFile(file);
    }
  });

  const STATUS_LABEL = { pass: "سالم", warn: "هشدار", fail: "خطا" };

  function renderMethods(report) {
    const select = document.getElementById("payment-method");
    if (!select) return;
    const list = (report && report.payment_methods) || [];
    const chosen = (report && report.payment_method) || select.value || "";
    select.innerHTML = '<option value="">همهٔ روش‌ها</option>';
    list.forEach((method) => {
      const option = document.createElement("option");
      option.value = method;
      option.textContent = method;
      select.appendChild(option);
    });
    if (chosen && list.includes(chosen)) select.value = chosen;
    select.title = list.length ? "روش‌های موجود: " + list.join("، ") : "";
  }

  function renderGate(report) {
    gate.hidden = false;
    gate.dataset.token = report.token || "";
    gateStatus.textContent = STATUS_LABEL[report.status] || report.status;
    gateStatus.className = "badge badge-" + report.status;
    const counts = report.counts || {};
    gateSummary.textContent =
      `${report.days} روز داده · ${counts.pass || 0} بررسی سالم · ` +
      `${counts.warn || 0} هشدار · ${counts.fail || 0} خطا`;

    const plan = report.plan;
    gatePlan.textContent = plan
      ? `افق: ${plan.horizon} روز — از ${plan.origin} تا ${plan.target}. ${plan.reason}`
      : "";
    if (report.window && report.window.message) {
      gatePlan.textContent += " " + report.window.message;
    }

    gateChecks.innerHTML = "";
    (report.checks || []).forEach((check) => {
      const li = document.createElement("li");
      li.className = "check check-" + check.status;
      const title = document.createElement("span");
      title.className = "check-title";
      title.textContent = check.title;
      const detail = document.createElement("span");
      detail.className = "check-detail";
      detail.textContent = check.detail;
      li.append(title, detail);
      gateChecks.appendChild(li);
    });

    approveBtn.hidden = Boolean(report.blocked);
    gateHint.textContent = report.blocked
      ? "تا رفع خطاها امکان محاسبه وجود ندارد."
      : "پس از بررسی، تأیید کنید تا داشبورد ساخته شود.";
    qualityToken = null;
    submitBtn.disabled = true;
  }

  async function runQualityCheck() {
    clearError();
    const file = fileInput.files[0];
    if (!file) {
      showError("ابتدا فایل اکسل را انتخاب کنید.");
      return;
    }
    const body = new FormData();
    body.append("file", file);
    body.append("horizon", document.getElementById("horizon").value);
    body.append("payment_method", methodValue());

    setBusy(true, "در حال بررسی کیفیت داده…");
    try {
      const response = await fetch("/api/quality", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "بررسی کیفیت داده ناموفق بود.");
      qualityFileKey = file.name + ":" + file.size + ":" + file.lastModified;
      renderMethods(data);
      renderGate(data);
      gate.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      resetGate();
      showError(error.message || "خطای نامشخص در بررسی کیفیت داده.");
    } finally {
      setBusy(false);
    }
  }

  checkBtn.addEventListener("click", runQualityCheck);

  approveBtn.addEventListener("click", () => {
    const file = fileInput.files[0];
    const key = file ? file.name + ":" + file.size + ":" + file.lastModified : null;
    if (!key || key !== qualityFileKey) {
      resetGate();
      showError("فایل تغییر کرده است؛ کیفیت داده را دوباره بررسی کنید.");
      return;
    }
    qualityToken = gate.dataset.token || null;
    approveBtn.hidden = true;
    gateHint.textContent = "کیفیت داده تأیید شد؛ اکنون داشبورد را بسازید.";
    submitBtn.disabled = false;
  });

  let sessionRun = null;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    if (sessionRun) {
      buildFromSession(sessionRun, document.getElementById("months").value);
      return;
    }
    const file = fileInput.files[0];
    if (!file) {
      showError("ابتدا فایل اکسل را انتخاب کنید.");
      return;
    }

    const body = new FormData();
    body.append("file", file);
    body.append("horizon", document.getElementById("horizon").value);
    body.append("months", document.getElementById("months").value);
    body.append("quality_token", qualityToken || "");
    body.append("payment_method", methodValue());

    setBusy(true, "در حال ساخت داشبورد مقایسه‌ای…");
    results.hidden = true;
    try {
      const response = await fetch("/api/comparison", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "ساخت داشبورد ناموفق بود.");
      payload = data;
      render(data);
      results.hidden = false;
      statusPill.textContent = "داشبورد آماده است";
      statusPill.className = "pill";
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showError(error.message || "خطای نامشخص در ساخت داشبورد.");
    } finally {
      setBusy(false);
    }
  });

  const methodSelect = document.getElementById("payment-method");
  if (methodSelect) {
    methodSelect.addEventListener("change", () => {
      // A different method means a different dataset behind the same file, so
      // the gate has to run again before the dashboard may use it.
      resetGate();
      if (fileInput.files[0]) runQualityCheck();
    });
  }

  document.getElementById("months").addEventListener("change", (event) => {
    if (sessionRun) buildFromSession(sessionRun, event.target.value);
  });

  function verticalLabel(name) {
    return VERTICAL_LABELS[name] || name;
  }

  function render(data) {
    renderKpis(data);
    renderPastTable(data);
    renderScenarioTable(data);
    renderChart(data);
    renderValidity(data);
  }

  function renderKpis(data) {
    const container = document.getElementById("kpis");
    container.innerHTML = "";
    const past = data.past_months || [];
    const lastMonth = past[past.length - 1];
    const lastRows = (data.months || []).filter((r) => r.month === lastMonth);
    const lastTotal = lastRows.find((r) => r.vertical === "VERTICALS");
    const prevMonth = past[past.length - 2];
    const prevTotal = (data.months || []).find(
      (r) => r.month === prevMonth && r.vertical === "VERTICALS"
    );
    const growth =
      lastTotal && prevTotal && prevTotal.orders > 0
        ? (lastTotal.orders - prevTotal.orders) / prevTotal.orders
        : null;

    const cards = [
      {
        label: `سفارش ${lastMonth || "—"}`,
        value: lastTotal ? formatNumber(lastTotal.orders) : "—",
        sub: "آخرین ماه کامل",
        cls: "kpi-accent",
      },
      {
        label: "رشد ماه‌به‌ماه",
        value: growth === null ? "—" : fa1.format(growth * 100) + "٪",
        sub: prevMonth ? `نسبت به ${prevMonth}` : "ماه قبل موجود نیست",
        cls: growth !== null && growth < 0 ? "kpi-warn" : "kpi-good",
      },
    ];

    // One card per calendar month the horizon touches: the rest of this month
    // and the following month in full, each with its own scenario range.
    scenarioMonths(data).forEach((entry) => {
      const realistic = scenarioTotal(data, entry.month, "realistic");
      const pessimistic = scenarioTotal(data, entry.month, "pessimistic");
      const optimistic = scenarioTotal(data, entry.month, "optimistic");
      cards.push({
        label: `پیش‌بینی ${entry.month}`,
        value: realistic ? formatNumber(realistic.orders) : "—",
        sub: `${MONTH_SCOPE[entry.scope] || "ماه"}${entry.days ? " · " + fa.format(entry.days) + " روز" : ""}`
          + (pessimistic && optimistic
            ? ` · ${formatNumber(pessimistic.orders)} – ${formatNumber(optimistic.orders)}`
            : ""),
        cls: "kpi-accent",
      });
    });

    cards.forEach((card) => {
      const el = document.createElement("div");
      el.className = "kpi " + card.cls;
      el.innerHTML =
        '<span class="kpi-label"></span><span class="kpi-value"></span><span class="kpi-sub"></span>';
      el.querySelector(".kpi-label").textContent = card.label;
      el.querySelector(".kpi-value").textContent = card.value;
      el.querySelector(".kpi-sub").textContent = card.sub;
      container.appendChild(el);
    });
  }

  function renderPastTable(data) {
    const table = document.getElementById("past-table");
    const head = table.querySelector("thead");
    const body = table.querySelector("tbody");
    const months = data.past_months || [];
    const metric = pastMetric;

    head.innerHTML = "";
    const headRow = document.createElement("tr");
    ["ورتیکال", ...months].forEach((label) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = label;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);

    const order = [
      "DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES",
      "SNAPP-VENTURE", "SNAPP-PAY", "VERTICALS",
    ];

    body.innerHTML = "";
    order.forEach((vertical) => {
      const tr = document.createElement("tr");
      if (vertical === "VERTICALS") tr.className = "is-total";
      const nameCell = document.createElement("td");
      nameCell.textContent = verticalLabel(vertical);
      tr.appendChild(nameCell);
      months.forEach((month) => {
        const row = (data.months || []).find(
          (r) => r.month === month && r.vertical === vertical
        );
        const td = document.createElement("td");
        td.textContent = row ? formatMetric(row[metric], metric) : "—";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function renderScenarioTable(data) {
    const table = document.getElementById("scenario-table");
    const head = table.querySelector("thead");
    const body = table.querySelector("tbody");
    const badge = document.getElementById("scenario-badge");
    const note = document.getElementById("scenario-note");

    const months = scenarioMonths(data);
    if (!scenarioMonth || !months.some((m) => m.month === scenarioMonth)) {
      scenarioMonth = months.length ? months[0].month : null;
    }
    const active = months.find((m) => m.month === scenarioMonth);
    badge.textContent = active
      ? `${MONTH_SCOPE[active.scope] || "ماه"} · ${active.month}` +
        (active.days ? ` (${fa.format(active.days)} روز)` : "")
      : "—";
    note.textContent = (data.validity && data.validity.note) || "";

    const chips = document.getElementById("scenario-months");
    if (chips) {
      chips.innerHTML = "";
      months.forEach((entry) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chip";
        button.setAttribute("aria-pressed", String(entry.month === scenarioMonth));
        button.innerHTML = "<strong></strong><small></small>";
        button.querySelector("strong").textContent = entry.month;
        button.querySelector("small").textContent =
          (MONTH_SCOPE[entry.scope] || "") + (entry.days ? ` · ${fa.format(entry.days)} روز` : "");
        button.addEventListener("click", () => {
          scenarioMonth = entry.month;
          if (payload) renderScenarioTable(payload);
        });
        chips.appendChild(button);
      });
    }

    head.innerHTML = "";
    const headRow = document.createElement("tr");
    ["ورتیکال", "سفارش بدبینانه", "سفارش واقع‌بینانه", "سفارش خوش‌بینانه",
     "GMV بدبینانه", "GMV واقع‌بینانه", "GMV خوش‌بینانه"].forEach((label) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = label;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);

    const order = [
      "DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES",
      "SNAPP-VENTURE", "SNAPP-PAY", "VERTICALS",
    ];

    body.innerHTML = "";
    order.forEach((vertical) => {
      const rows = (data.scenarios || []).filter(
        (r) => r.vertical === vertical && r.month === scenarioMonth
      );
      if (!rows.length) return;
      const byScenario = {};
      rows.forEach((r) => { byScenario[r.scenario] = r; });
      const tr = document.createElement("tr");
      if (vertical === "VERTICALS") tr.className = "is-total";
      const nameCell = document.createElement("td");
      nameCell.textContent = verticalLabel(vertical);
      tr.appendChild(nameCell);
      ["pessimistic", "realistic", "optimistic"].forEach((scenario) => {
        const td = document.createElement("td");
        td.textContent = byScenario[scenario]
          ? formatNumber(byScenario[scenario].orders)
          : "—";
        tr.appendChild(td);
      });
      ["pessimistic", "realistic", "optimistic"].forEach((scenario) => {
        const td = document.createElement("td");
        td.textContent = byScenario[scenario]
          ? formatNumber(byScenario[scenario].gmv)
          : "—";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function renderChart(data) {
    const svg = document.getElementById("chart");
    svg.innerHTML = "";
    const months = data.past_months || [];
    const metric = chartMetric;

    const pastTotals = months.map((month) => {
      const row = (data.months || []).find(
        (r) => r.month === month && r.vertical === "VERTICALS"
      );
      return row ? row[metric] : 0;
    });
    // Every scenario month gets its own group, so the rest of this month is not
    // visually merged into the following month.
    const bars = [];
    scenarioMonths(data).forEach((entry) => {
      ["pessimistic", "realistic", "optimistic"].forEach((scenario) => {
        const row = scenarioTotal(data, entry.month, scenario);
        bars.push({
          label: `${SCENARIO_LABELS[scenario]}\n${entry.month}`,
          value: row ? row[metric] : 0,
          scenario,
        });
      });
    });

    const labels = [...months, ...bars.map((b) => b.label)];
    const values = [...pastTotals, ...bars.map((b) => b.value)];
    const maxValue = Math.max.apply(null, values.concat([1])) * 1.1;

    const W = 800;
    const H = 320;
    const pad = { top: 20, right: 16, bottom: 46, left: 64 };
    const innerW = W - pad.left - pad.right;
    const innerH = H - pad.top - pad.bottom;
    const slot = innerW / Math.max(values.length, 1);
    const barW = Math.min(slot * 0.62, 64);

    const ns = "http://www.w3.org/2000/svg";
    const make = (tag, attrs) => {
      const el = document.createElementNS(ns, tag);
      Object.keys(attrs).forEach((key) => el.setAttribute(key, attrs[key]));
      return el;
    };

    for (let i = 0; i <= 4; i += 1) {
      const value = (maxValue / 4) * i;
      const yy = pad.top + innerH - (value / maxValue) * innerH;
      svg.appendChild(
        make("line", {
          x1: pad.left, x2: W - pad.right, y1: yy, y2: yy,
          stroke: "#2c3a54", "stroke-width": 1,
        })
      );
      const label = make("text", { x: pad.left - 8, y: yy + 4, "text-anchor": "end" });
      label.textContent = fa.format(Math.round(value));
      svg.appendChild(label);
    }

    const colors = {
      past: "#9aa8bd",
      pessimistic: "#db4f4a",
      realistic: "#4cceac",
      optimistic: "#6870fa",
    };

    values.forEach((value, index) => {
      const x = pad.left + slot * index + (slot - barW) / 2;
      const height = (value / maxValue) * innerH;
      const y = pad.top + innerH - height;
      const isPast = index < months.length;
      const bar = isPast ? null : bars[index - months.length];
      svg.appendChild(
        make("rect", {
          x, y, width: barW, height: Math.max(height, 0),
          rx: 4,
          fill: isPast ? colors.past : colors[bar.scenario],
          opacity: isPast ? "0.75" : "0.95",
        })
      );
      const parts = isPast ? [labels[index]] : bar.label.split("\n");
      const label = make("text", {
        x: x + barW / 2, y: H - 34, "text-anchor": "middle",
      });
      parts.forEach((part, line) => {
        const tspan = make("tspan", { x: x + barW / 2, dy: line === 0 ? 0 : 12 });
        tspan.textContent = part;
        label.appendChild(tspan);
      });
      svg.appendChild(label);
    });

    const axis = make("text", {
      x: W - pad.right, y: H - 6, "text-anchor": "end",
    });
    axis.textContent = METRIC_LABELS[metric];
    svg.appendChild(axis);
  }

  function renderValidity(data) {
    const note = document.getElementById("validity-note");
    const tbody = document.querySelector("#validity-table tbody");
    const warnings = document.getElementById("warnings");
    const validity = data.validity || {};

    note.textContent = validity.note || "";
    tbody.innerHTML = "";
    const ranking = validity.ranking || [];
    if (!ranking.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">بک‌تست کافی نبود.</td></tr>';
    } else {
      ranking.forEach((row, index) => {
        const tr = document.createElement("tr");
        if (index === 0) tr.className = "is-total";
        [row.model, fa1.format(row.wape * 100) + "٪", fa1.format(row.bias * 100) + "٪"]
          .forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
          });
        tbody.appendChild(tr);
      });
    }

    warnings.innerHTML = "";
    const list = data.warnings || [];
    if (!list.length) {
      warnings.innerHTML = '<li class="empty">هیچ هشداری ثبت نشد.</li>';
    } else {
      list.forEach((text) => {
        const li = document.createElement("li");
        li.className = "is-warn";
        li.textContent = text;
        warnings.appendChild(li);
      });
    }
  }

  document.querySelectorAll("#past-switch .seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      pastMetric = btn.dataset.metric;
      document.querySelectorAll("#past-switch .seg").forEach((other) => {
        other.classList.toggle("is-active", other === btn);
      });
      if (payload) renderPastTable(payload);
    });
  });

  document.querySelectorAll("#chart-switch .seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      chartMetric = btn.dataset.metric;
      document.querySelectorAll("#chart-switch .seg").forEach((other) => {
        other.classList.toggle("is-active", other === btn);
      });
      if (payload) renderChart(payload);
    });
  });

  /* ---------- cross-page state ---------- */
  const sessionStrip = document.getElementById("session-strip");
  const sourceBadge = document.getElementById("source-badge");

  async function loadStatus() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      const entry = data.last_run;
      const lastRun = document.getElementById("last-run");
      if (entry && lastRun) {
        const parts = [];
        if (entry.file) parts.push("آخرین اجرا: " + entry.file);
        if (entry.origin) parts.push("مبدأ " + entry.origin);
        if (entry.horizon) parts.push("افق " + fa.format(entry.horizon) + " روز");
        if (parts.length) {
          lastRun.textContent = parts.join(" · ");
          lastRun.hidden = false;
        }
      }
    } catch (err) {
      /* the strip is a convenience; never block the page on it */
    }
  }

  /* A link from the run flow carries ?run_id=; the file and its approval are
     already on the server, so the page never asks the user to upload twice. */
  async function loadFromSession() {
    const runId = new URLSearchParams(location.search).get("run_id");
    if (!runId) return;
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "نشست پیدا نشد.");
      const record = data.run || {};
      sessionRun = { runId, token: (data.quality && data.quality.token) || "" };
      renderMethods(data.quality);
      sourceBadge.textContent = "فایل این اجرا";
      sourceBadge.className = "badge badge-pass";
      document.getElementById("dropzone").hidden = true;
      document.getElementById("check-btn").hidden = true;
      document.getElementById("submit-btn").disabled = false;
      sessionStrip.hidden = false;
      sessionStrip.innerHTML =
        `این داشبورد از فایل <strong>${record.file || runId}</strong> ساخته می‌شود` +
        (record.origin ? ` · مبدأ ${record.origin}` : "") +
        (record.horizon ? ` · افق ${fa.format(record.horizon)} روز` : "") +
        ' · <a href="/">بازگشت به جریان</a>';
      if (data.comparison) {
        payload = data.comparison;
        render(payload);
        results.hidden = false;
        statusPill.textContent = "داشبورد آماده است";
        statusPill.className = "pill";
        return;
      }
      buildFromSession(sessionRun, document.getElementById("months").value);
    } catch (error) {
      sourceBadge.textContent = "نشست نامعتبر";
      sourceBadge.className = "badge badge-fail";
      showError(error.message);
    }
  }

  async function buildFromSession(run, months) {
    clearError();
    setBusy(true, "در حال ساخت داشبورد از همان فایل…");
    results.hidden = true;
    try {
      const body = new FormData();
      body.append("quality_token", run.token);
      body.append("months", String(months));
      const response = await fetch(`/api/runs/${encodeURIComponent(run.runId)}/comparison`, {
        method: "POST",
        body,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "ساخت داشبورد ناموفق بود.");
      payload = data;
      render(data);
      results.hidden = false;
      statusPill.textContent = "داشبورد آماده است";
      statusPill.className = "pill";
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showError(error.message || "خطای نامشخص در ساخت داشبورد.");
    } finally {
      setBusy(false);
    }
  }

  loadStatus();
  loadFromSession();
})();
