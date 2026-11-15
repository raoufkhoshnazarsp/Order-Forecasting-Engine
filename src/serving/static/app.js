/* BNPL Forecast Studio — vanilla JS, no build step. */
(function () {
  "use strict";

  const form = document.getElementById("forecast-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file");
  const fileName = document.getElementById("file-name");
  const submitBtn = document.getElementById("submit-btn");
  const deliveryBtn = document.getElementById("delivery-btn");
  const checkBtn = document.getElementById("check-btn");
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

  const fa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
  const fa1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });

  let lastChart = null;
  let qualityToken = null;
  let qualityFileKey = null;
  let currentMetric = "orders";

  function formatNumber(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return fa.format(value);
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function setBusy(on, message) {
    progress.hidden = !on;
    if (progressText) {
      progressText.textContent = message || "در حال پردازش فایل و اجرای بک‌تست…";
    }
    checkBtn.disabled = on;
    deliveryBtn.disabled = on || !qualityToken;
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
    deliveryBtn.disabled = true;
  }

  const STATUS_LABEL = { pass: "سالم", warn: "هشدار", fail: "خطا" };

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
    if (plan) {
      gatePlan.textContent =
        `افق: ${plan.horizon} روز — از ${plan.origin} تا ${plan.target}. ${plan.reason}`;
    } else {
      gatePlan.textContent = "";
    }
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

    if (report.blocked) {
      approveBtn.hidden = true;
      gateHint.textContent = "تا رفع خطاها امکان محاسبه وجود ندارد.";
      qualityToken = null;
    } else {
      approveBtn.hidden = false;
      gateHint.textContent = "پس از بررسی، تأیید کنید تا محاسبه اجرا شود.";
      qualityToken = null;
    }
    submitBtn.disabled = true;
    deliveryBtn.disabled = true;
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

    setBusy(true, "در حال بررسی کیفیت داده…");
    try {
      const response = await fetch("/api/quality", { method: "POST", body });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "بررسی کیفیت داده ناموفق بود.");
      }
      qualityFileKey = file.name + ":" + file.size + ":" + file.lastModified;
      renderGate(payload);
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
    gateHint.textContent = "کیفیت داده تأیید شد؛ اکنون می‌توانید پیش‌بینی را اجرا کنید.";
    submitBtn.disabled = false;
    deliveryBtn.disabled = false;
  });

  function clearError() {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function setFile(file) {
    if (!file) return;
    const ok = /\.(xlsx|xlsm|xls)$/i.test(file.name);
    if (!ok) {
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

  deliveryBtn.addEventListener("click", async () => {
    clearError();
    const file = fileInput.files[0];
    if (!file) {
      showError("ابتدا فایل اکسل را انتخاب کنید.");
      return;
    }

    const body = new FormData();
    body.append("file", file);
    body.append("horizon", document.getElementById("horizon").value);
    body.append("model", document.getElementById("model").value);
    body.append("quality_token", qualityToken || "");

    setBusy(true, "در حال ساخت فایل تحویل…");
    try {
      const response = await fetch("/api/delivery", { method: "POST", body });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "ساخت فایل تحویل ناموفق بود.");
      }
      const blob = await response.blob();
      const stamp = (response.headers.get("X-Forecast-Rows") || "").trim();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `forecast-delivery${stamp ? "-" + stamp + "rows" : ""}.xlsx`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 4000);
    } catch (error) {
      showError(error.message || "خطای نامشخص در ساخت فایل تحویل.");
    } finally {
      setBusy(false);
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const file = fileInput.files[0];
    if (!file) {
      showError("ابتدا فایل اکسل را انتخاب کنید.");
      return;
    }

    const body = new FormData();
    body.append("file", file);
    body.append("horizon", document.getElementById("horizon").value);
    body.append("model", document.getElementById("model").value);
    body.append("quality_token", qualityToken || "");

    setBusy(true, "در حال خواندن فایل، اعتبارسنجی داده و اجرای بک‌تست…");
    results.hidden = true;

    try {
      const response = await fetch("/api/forecast", { method: "POST", body });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "پردازش فایل ناموفق بود.");
      }
      render(payload);
      results.hidden = false;
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showError(error.message || "خطای نامشخص در پردازش فایل.");
    } finally {
      setBusy(false);
    }
  });

  function render(data) {
    renderKpis(data);
    lastChart = data.chart;
    currentMetric = "orders";
    document.querySelectorAll("#metric-switch .seg").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.metric === "orders");
    });
    renderChart(data.chart, currentMetric);
    renderEntityTable(data.entities_table);
    renderModelTable(data.backtest_report, data.backtest_ranking);
    renderWarnings(data.warnings);
    renderSheets(data.sheets);
    document.getElementById("chart-badge").textContent =
      "مدل: " + modelLabel(data.model) + " · افق " + formatNumber(data.horizon) + " روز";
  }

  document.querySelectorAll("#metric-switch .seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!lastChart) return;
      currentMetric = btn.dataset.metric;
      document.querySelectorAll("#metric-switch .seg").forEach((other) => {
        other.classList.toggle("is-active", other === btn);
      });
      renderChart(lastChart, currentMetric);
    });
  });

  function modelLabel(name) {
    const labels = {
      auto: "خودکار",
      seasonal_naive: "فصلی ساده",
      naive: "ساده",
      moving_average: "میانگین متحرک",
      ets: "ETS",
      drift: "روند خطی",
      calendar_profile: "پروفایل تقویم شمسی",
    };
    return labels[name] || name;
  }

  function renderKpis(data) {
    const container = document.getElementById("kpis");
    container.innerHTML = "";
    const d1 = data.total_forecast["D+1"];
    const d7 = data.total_forecast["D+7"];
    const last = data.total_forecast["D+" + data.horizon];
    const change =
      data.total_last_actual && d1
        ? ((d1 - data.total_last_actual) / data.total_last_actual) * 100
        : null;

    const gmvD1 = data.total_gmv_forecast ? data.total_gmv_forecast["D+1"] : null;
    const aovD1 = data.total_aov_forecast ? data.total_aov_forecast["D+1"] : null;

    const cards = [
      {
        label: "آخرین مقدار واقعی",
        value: formatNumber(data.total_last_actual),
        sub: "سفارش موفق در " + data.date_end,
        cls: "",
      },
      {
        label: "پیش‌بینی فردا (D+1)",
        value: formatNumber(d1),
        sub: change === null ? "—" : (change >= 0 ? "▲ " : "▼ ") + fa1.format(Math.abs(change)) + "٪ نسبت به آخرین روز",
        cls: "kpi-accent",
      },
      {
        label: "پیش‌بینی هفته آینده (D+7)",
        value: formatNumber(d7),
        sub: "روز هفتم افق",
        cls: "",
      },
      {
        label: "پیش‌بینی روز " + formatNumber(data.horizon),
        value: formatNumber(last),
        sub: "انتهای افق پیش‌بینی",
        cls: "kpi-good",
      },
      {
        label: "GMV فردا (D+1)",
        value: gmvD1 === null ? "—" : formatNumber(gmvD1),
        sub: "سفارش × AOV",
        cls: "kpi-accent",
      },
      {
        label: "AOV فردا (D+1)",
        value: aovD1 === null ? "—" : formatNumber(aovD1),
        sub: "میانگین ارزش هر سفارش",
        cls: "",
      },
      {
        label: "موجودیت‌های فعال",
        value: formatNumber(data.entities.length),
        sub: formatNumber(data.mart_rows) + " ردیف در داده روزانه",
        cls: "",
      },
    ];

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

  function renderChart(chart, metric) {
    const svg = document.getElementById("chart");
    svg.innerHTML = "";
    if (!chart) return;

    let series = chart;
    if (metric === "gmv" || metric === "aov") {
      series = chart[metric];
    }
    if (!series || !series.history || !series.history.values.length) {
      svg.innerHTML = '<text x="400" y="150" text-anchor="middle" fill="#8b9cb3">داده‌ای برای این شاخص موجود نیست</text>';
      return;
    }

    const W = 800;
    const H = 300;
    const pad = { top: 18, right: 16, bottom: 30, left: 52 };
    const innerW = W - pad.left - pad.right;
    const innerH = H - pad.top - pad.bottom;

    const history = series.history;
    const forecast = series.forecast;
    const hasBand = Array.isArray(forecast.upper) && Array.isArray(forecast.lower);
    const allValues = history.values
      .concat(forecast.values)
      .concat(hasBand ? forecast.upper : [])
      .filter((v) => v !== null && v !== undefined);
    const maxValue = Math.max.apply(null, allValues) * 1.08 || 1;
    const totalPoints = history.values.length + forecast.values.length;

    const x = (index) => pad.left + (index / Math.max(totalPoints - 1, 1)) * innerW;
    const y = (value) => pad.top + innerH - (value / maxValue) * innerH;

    const ns = "http://www.w3.org/2000/svg";
    const make = (tag, attrs) => {
      const el = document.createElementNS(ns, tag);
      Object.keys(attrs).forEach((key) => el.setAttribute(key, attrs[key]));
      return el;
    };

    // horizontal grid + y labels
    for (let i = 0; i <= 4; i += 1) {
      const value = (maxValue / 4) * i;
      const yy = y(value);
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

    const histIndex = history.values.length - 1;

    // 90% band for the forecast
    if (hasBand) {
      const upper = forecast.upper.map((v, i) => [x(histIndex + i), y(v)]);
      const lower = forecast.lower.map((v, i) => [x(histIndex + i), y(v)]).reverse();
      const bandPath =
        "M" + upper.map((p) => p.join(",")).join("L") +
        "L" + lower.map((p) => p.join(",")).join("L") + "Z";
      svg.appendChild(make("path", { d: bandPath, fill: "#6870fa", opacity: "0.55" }));
    }

    const linePath = (values, offset) =>
      "M" + values.map((v, i) => [x(offset + i), y(v)].join(",")).join("L");

    svg.appendChild(
      make("path", {
        d: linePath(history.values, 0),
        fill: "none", stroke: "#9aa8bd", "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      })
    );
    svg.appendChild(
      make("path", {
        d: linePath(forecast.values, histIndex),
        fill: "none", stroke: "#4cceac", "stroke-width": 2.2,
        "stroke-dasharray": "5 4", "stroke-linejoin": "round",
      })
    );

    // divider between actual and forecast
    svg.appendChild(
      make("line", {
        x1: x(histIndex), x2: x(histIndex), y1: pad.top, y2: pad.top + innerH,
        stroke: "#3a4a67", "stroke-width": 1, "stroke-dasharray": "3 3",
      })
    );

    // x labels: first, split, last
    [
      { index: 0, text: history.labels[0] },
      { index: histIndex, text: "امروز" },
      { index: totalPoints - 1, text: forecast.labels[forecast.labels.length - 1] },
    ].forEach((item) => {
      const label = make("text", {
        x: x(item.index), y: H - 8, "text-anchor": "middle",
      });
      label.textContent = item.text;
      svg.appendChild(label);
    });
  }

  function renderEntityTable(rows) {
    const tbody = document.querySelector("#entity-table tbody");
    tbody.innerHTML = "";
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      if (row.entity_type === "TOTAL") tr.className = "is-total";
      const typeLabel = row.entity_type === "TOTAL" ? "کل" : row.entity_type === "VENTURE" ? "ونچر" : "ورتیکال";
      [row.entity_id, typeLabel, formatNumber(row.d1), formatNumber(row.d7),
       formatNumber(row.d30), formatNumber(row.total_30),
       formatNumber(row.gmv_d1), formatNumber(row.gmv_total_30),
       formatNumber(row.aov_d1)].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function renderModelTable(report, ranking) {
    const tbody = document.querySelector("#model-table tbody");
    const meta = document.getElementById("backtest-meta");
    const note = document.getElementById("backtest-note");
    tbody.innerHTML = "";
    meta.textContent = "";
    note.textContent = "";

    const rows = (report && report.models && report.models.length)
      ? report.models
      : (ranking || []).map((r) => ({ ...r, coverage_90: null, windows: null, points: null }));

    if (!rows.length) {
      tbody.innerHTML =
        '<tr><td colspan="6" class="empty">تاریخچه کافی برای بک‌تست نبود.</td></tr>';
      if (report && report.note) note.textContent = report.note;
      return;
    }

    if (report && report.windows) {
      meta.textContent =
        `${formatNumber(report.windows)} پنجره · دورهٔ آزمون ${report.test_start} تا ` +
        `${report.test_end} · ${formatNumber(report.points)} نقطه · افق ${formatNumber(report.horizon)} روز`;
    }

    rows.forEach((row, index) => {
      const tr = document.createElement("tr");
      if (index === 0) tr.className = "is-total";
      const coverage = row.coverage_90 === null || row.coverage_90 === undefined
        ? "—"
        : fa1.format(row.coverage_90 * 100) + "٪";
      [
        modelLabel(row.model),
        fa1.format(row.wape * 100) + "٪",
        fa1.format(row.bias * 100) + "٪",
        coverage,
        row.windows === null || row.windows === undefined ? "—" : formatNumber(row.windows),
        row.points === null || row.points === undefined ? "—" : formatNumber(row.points),
      ].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });

    if (report && report.note) note.textContent = report.note;
  }

  function renderWarnings(warnings) {
    const list = document.getElementById("warnings");
    list.innerHTML = "";
    if (!warnings || !warnings.length) {
      list.innerHTML = '<li class="empty">هیچ هشداری ثبت نشد.</li>';
      return;
    }
    warnings.forEach((text) => {
      const li = document.createElement("li");
      li.className = "is-warn";
      li.textContent = text;
      list.appendChild(li);
    });
  }

  function renderSheets(sheets) {
    const list = document.getElementById("sheets");
    list.innerHTML = "";
    (sheets || []).forEach((sheet) => {
      const li = document.createElement("li");
      const layout = sheet.layout === "raw_orders" ? "سفارش خام" :
        sheet.layout === "daily_aggregated" ? "روزانه تجمیع‌شده" :
        sheet.layout === "daily_metrics" ? "مدل داده تجاری (سفارش + GMV)" : "نامشخص";
      li.innerHTML = "<code></code> — " + layout + " · " + formatNumber(sheet.rows_used) + " سفارش";
      li.querySelector("code").textContent = sheet.sheet;
      list.appendChild(li);
    });
    if (!list.children.length) {
      list.innerHTML = '<li class="empty">برگه‌ای خوانده نشد.</li>';
    }
  }
})();
