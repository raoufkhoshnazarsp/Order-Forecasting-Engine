/* BNPL Forecast Studio — vanilla JS, no build step. */
(function () {
  "use strict";

  const form = document.getElementById("forecast-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file");
  const fileName = document.getElementById("file-name");
  const submitBtn = document.getElementById("submit-btn");
  const errorBox = document.getElementById("error-box");
  const progress = document.getElementById("progress");
  const results = document.getElementById("results");

  const fa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
  const fa1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });

  let lastChart = null;
  let currentMetric = "orders";

  function formatNumber(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
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

    submitBtn.disabled = true;
    progress.hidden = false;
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
      submitBtn.disabled = false;
      progress.hidden = true;
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
    renderModelTable(data.backtest_ranking);
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
      svg.innerHTML = '<text x="400" y="150" text-anchor="middle" fill="#8a93a6">داده‌ای برای این شاخص موجود نیست</text>';
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
          stroke: "#eef1f6", "stroke-width": 1,
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
      svg.appendChild(make("path", { d: bandPath, fill: "#cfdcf7", opacity: "0.55" }));
    }

    const linePath = (values, offset) =>
      "M" + values.map((v, i) => [x(offset + i), y(v)].join(",")).join("L");

    svg.appendChild(
      make("path", {
        d: linePath(history.values, 0),
        fill: "none", stroke: "#55617a", "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      })
    );
    svg.appendChild(
      make("path", {
        d: linePath(forecast.values, histIndex),
        fill: "none", stroke: "#1c5fd4", "stroke-width": 2.2,
        "stroke-dasharray": "5 4", "stroke-linejoin": "round",
      })
    );

    // divider between actual and forecast
    svg.appendChild(
      make("line", {
        x1: x(histIndex), x2: x(histIndex), y1: pad.top, y2: pad.top + innerH,
        stroke: "#cfd5e1", "stroke-width": 1, "stroke-dasharray": "3 3",
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

  function renderModelTable(ranking) {
    const tbody = document.querySelector("#model-table tbody");
    tbody.innerHTML = "";
    if (!ranking || !ranking.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">تاریخچه کافی برای بک‌تست نبود.</td></tr>';
      return;
    }
    ranking.forEach((row, index) => {
      const tr = document.createElement("tr");
      if (index === 0) tr.className = "is-total";
      [modelLabel(row.model), fa1.format(row.wape * 100) + "٪", fa1.format(row.bias * 100) + "٪"]
        .forEach((value) => {
          const td = document.createElement("td");
          td.textContent = value;
          tr.appendChild(td);
        });
      tbody.appendChild(tr);
    });
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
