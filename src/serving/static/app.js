/* Forecast Studio flow — one session, one upload, every step reusable. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
  const fa1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });
  const modelNames = {
    auto: "خودکار", seasonal_naive: "فصلی ساده", naive: "ساده",
    moving_average: "میانگین متحرک", ets: "ETS", drift: "روند خطی",
    calendar_profile: "پروفایل تقویم شمسی", global_ml: "مدل سراسری ML",
  };
  const typeNames = { TOTAL: "کل", VERTICAL: "ورتیکال", VENTURE: "ونچر" };
  const statusNames = { pass: "سالم", warn: "هشدار", fail: "خطا" };
  const checkHelp = {
    zeros: "صفر واقعی با روزی که سفارشی نداشته تفاوت دارد؛ روز بدون سفارش را صفر ثبت کنید، ردیف را حذف نکنید.",
    continuity: "تاریخ‌های گم‌شده را در فایل کامل کنید یا علت کسب‌وکاری آن‌ها را بررسی کنید.",
    history: "برای بک‌تست قابل اتکا، تاریخچهٔ بیشتری با همان ساختار وارد کنید.",
    aov: "ستون سفارش و GMV را با یک مبنای واحد بررسی کنید.",
    partial_day: "روز آخر اگر کامل ثبت نشده، یا کامل بارگذاری کنید یا در فایل حذفش کنید.",
    verticals: "اگر یک ورتیکال واقعاً سفارش نداشته، خالی بودنش درست است؛ فقط خودتان تأیید کنید.",
  };
  const state = {
    runId: null, token: null, quality: null, preview: null,
    forecast: null, comparison: null, sensitivity: null,
    entity: "TOTAL", metric: "orders", step: 0,
  };

  function n(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return fa.format(Number(value));
  }
  function n1(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return fa1.format(Number(value));
  }
  function pct(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return fa1.format(Number(value) * 100) + "٪";
  }
  function esc(value) {
    const el = document.createElement("span");
    el.textContent = value == null ? "" : String(value);
    return el.innerHTML;
  }
  function modelLabel(value) { return modelNames[value] || value || "—"; }
  function stamp(value) { return value ? String(value).replace("T", " ").slice(0, 16) : "—"; }
  function clear(id) { const el = $(id); if (el) { el.hidden = true; el.textContent = ""; } }
  function show(id, text) { const el = $(id); if (el) { el.textContent = text; el.hidden = false; } }
  function announce(text) { const el = $("announcer"); if (el) el.textContent = text; }
  function setBusy(id, button, on, label) {
    const progress = $(id);
    if (progress) {
      progress.hidden = !on;
      const text = progress.querySelector("span");
      if (text && label) text.textContent = label;
    }
    if (button) $(button).disabled = on;
  }
  async function api(path, options) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "درخواست انجام نشد.");
    return body;
  }
  function form(fields) {
    const body = new FormData();
    Object.entries(fields).forEach(([key, value]) => body.append(key, value));
    return body;
  }

  /* ---------- stepper and status ---------- */
  const stepHtml = {
    overview: "overview", data: "step-data", quality: "step-quality", setup: "step-setup",
    backtest: "step-backtest", drivers: "step-drivers", result: "step-result", deliver: "step-deliver",
  };

  const STEP_INDEX = {
    overview: 0, data: 1, quality: 2, setup: 3, backtest: 4, drivers: 5, result: 6, deliver: 7,
  };

  function maxUnlocked() {
    if (state.comparison || state.sensitivity) return 7;
    if (state.forecast) return 6;
    if (state.quality) return state.quality.blocked ? 2 : 3;
    if (state.preview) return 1;
    return 0;
  }

  function renderSteps() {
    const unlocked = maxUnlocked();
    const current = Math.min(state.step, unlocked);
    const buttons = Array.from(document.querySelectorAll("#steps [data-go]"));
    buttons.forEach((button, index) => {
      const reachable = index <= unlocked;
      button.classList.toggle("is-current", index === current);
      button.classList.toggle("is-done", index < current && reachable);
      button.classList.toggle("is-locked", !reachable);
      button.disabled = !reachable;
      if (index === current) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    });

    const cards = [
      ["۱", "داده", state.preview ? `${n(state.preview.days)} روز · ${n((state.preview.entities || []).length)} موجودیت` : "فایل بارگذاری نشده", "data", !!state.preview],
      ["۲", "کیفیت", state.quality ? (state.quality.blocked ? "خطای مسدودکننده" : `${n(state.quality.counts?.warn)} هشدار · ${n(state.quality.counts?.pass)} سالم`) : "در انتظار داده", "quality", !!state.quality && !state.quality.blocked],
      ["۳", "پیش‌بینی و اعتبار", state.forecast ? `مدل ${modelLabel(state.forecast.model)} · افق ${n(state.forecast.horizon)}` : "پس از تأیید کیفیت", "result", !!state.forecast],
      ["۴", "تحویل", state.comparison || state.sensitivity ? "داشبورد و سناریو آماده" : "پس از پیش‌بینی", "deliver", !!(state.comparison || state.sensitivity)],
    ];
    const box = $("status-cards");
    if (box) {
      box.innerHTML = cards.map(([num, title, sub, go, done]) => `
        <button class="fact" type="button" data-go="${go}">
          <span class="fact-label">${num} · ${title} ${done ? '<span class="tick" aria-label="انجام شد"></span>' : ""}</span>
          <span class="fact-sub">${esc(sub)}</span>
        </button>`).join("");
      box.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
    }
  }

  function go(target) {
    const id = stepHtml[target] || "step-" + target;
    const el = $(id);
    if (!el || el.hidden) return;
    state.step = STEP_INDEX[target] ?? state.step;
    renderSteps();
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    announce("مرحله: " + target);
  }

  /* ---------- 1 · data preview ---------- */
  function renderPreview(preview, file) {
    state.preview = preview;
    $("preview").hidden = false;
    $("preview-file").textContent = file || "";
    const facts = [
      ["ردیف خوانده‌شده", n(preview.rows), "پس از نرمال‌سازی"],
      ["بازهٔ تاریخ", `${preview.date_start || "—"} تا ${preview.date_end || "—"}`, `${n(preview.days)} روز`],
      ["موجودیت", n((preview.entities || []).length), "ورتیکال و ونچر"],
      ["سفارش واقعی", n(preview.orders_total), "جمع فایل"],
      ["GMV واقعی", n(preview.gmv_total), "واحد فایل"],
    ];
    $("preview-facts").innerHTML = facts.map((f) => `
      <div class="fact"><span class="fact-label">${f[0]}</span>
      <strong class="fact-value">${esc(f[1])}</strong>
      <span class="fact-sub">${esc(f[2])}</span></div>`).join("");
    $("sheets").innerHTML = (preview.sheets || []).map((s) => `
      <li><strong>${esc(s.sheet)}</strong> — ${esc(s.layout)}
      <span class="muted">${esc(s.layout_hint || "")} · ${n(s.rows_used)} ردیف</span></li>`).join("")
      || "<li>برگه‌ای خوانده نشد.</li>";
    $("sheets-meta").textContent = n((preview.sheets || []).length) + " برگه";
    $("sample-table").querySelector("tbody").innerHTML = (preview.sample || []).map((r) => `
      <tr><td>${esc(r.date)}</td><td class="name">${esc(r.entity_id)}</td>
      <td>${esc(typeNames[r.entity_type] || r.entity_type)}</td>
      <td class="num">${n(r.successful_orders)}</td><td class="num">${n(r.gmv)}</td></tr>`).join("")
      || '<tr class="empty-row"><td colspan="5">نمونه‌ای موجود نیست.</td></tr>';
    $("entity-chips").innerHTML = (preview.entities || []).slice(0, 12).map((e) => `
      <span class="chip"><strong>${esc(e.entity_id)}</strong>
      <small>${esc(typeNames[e.entity_type] || e.entity_type)} · ${n(e.orders)} سفارش</small></span>`).join("");
    $("data-step-status").textContent = "پیش‌نمایش آماده";
    if (state.step < 1) state.step = 1;
    renderSteps();
  }

  /* ---------- 2 · quality ---------- */
  function renderQuality(report) {
    state.quality = report;
    state.token = report.token || null;
    $("quality-empty").hidden = true;
    const badge = $("quality-badge");
    badge.hidden = false;
    badge.textContent = statusNames[report.status] || report.status;
    badge.className = "badge badge-" + report.status;

    const plan = report.plan || null;
    $("quality-plan").hidden = !plan;
    if (plan) {
      $("quality-plan").textContent =
        `افق ${n(plan.horizon)} روز · از ${plan.origin} تا ${plan.target} · ${plan.reason || ""}`;
    }

    const counts = report.counts || {};
    $("quality-counts").hidden = false;
    $("quality-counts").innerHTML = [
      ["سالم", counts.pass || 0, "pass"], ["هشدار", counts.warn || 0, "warn"], ["خطا", counts.fail || 0, "fail"],
    ].map((x) => `
      <div class="fact"><span class="fact-label">${x[0]}</span>
      <strong class="fact-value">${n(x[1])}</strong>
      <span class="fact-sub">${x[2] === "pass" ? "بررسی" : x[2] === "warn" ? "نیازمند پذیرش" : "مسدودکننده"}</span></div>`).join("");

    $("quality-checks").innerHTML = (report.checks || []).map((c) => `
      <li class="check check-${c.status}">
        <span class="check-title">${esc(c.title)} · ${statusNames[c.status] || c.status}</span>
        <span class="check-detail">${esc(c.detail)}</span>
        ${checkHelp[c.name] ? `<span class="check-help">${esc(checkHelp[c.name])}</span>` : ""}
      </li>`).join("");

    const blocked = !!report.blocked;
    $("quality-actions").hidden = false;
    $("quality-blocked").hidden = !blocked;
    if (blocked) {
      $("quality-blocked").textContent =
        "ادامه متوقف است: خطاهای بالا را در فایل اصلاح کنید و دوباره بررسی کنید.";
    }
    const hasWarnings = (counts.warn || 0) > 0;
    $("ack-row").hidden = blocked || !hasWarnings;
    $("ack").checked = false;
    $("approve-btn").disabled = blocked || hasWarnings;
    $("quality-hint").textContent = blocked
      ? "ابتدا خطاهای مسدودکننده را رفع کنید."
      : hasWarnings
        ? "برای ادامه، پذیرش هشدارها الزامی است."
        : "گزارش کیفیت آمادهٔ تأیید است.";
    $("setup-target").textContent = plan ? `هدف محاسبه‌شده: ${plan.target}` : "";
    if (!blocked && state.step < 2) state.step = 2;
    renderSteps();
  }

  /* ---------- 4 · backtest ---------- */
  function renderBacktest(data) {
    const view = data.backtest_view || {};
    const chosen = view.chosen || {};
    $("backtest-badge").textContent = "مدل: " + modelLabel(view.model);
    $("backtest-facts").innerHTML = [
      ["مدل انتخاب‌شده", modelLabel(view.model), data.model === "auto" ? "برندهٔ انتخاب خودکار" : "انتخاب دستی"],
      ["پنجره‌های آزمون", n(view.windows), "rolling-origin"],
      ["نقاط ارزیابی", n(view.points), view.test_start && view.test_end ? `${view.test_start} تا ${view.test_end}` : ""],
    ].map((x) => `
      <div class="fact"><span class="fact-label">${x[0]}</span>
      <strong class="fact-value">${esc(x[1])}</strong>
      <span class="fact-sub">${esc(x[2])}</span></div>`).join("");

    $("why-model").textContent = chosen.wape == null
      ? "امتیاز بک‌تست کافی برای مقایسهٔ مدل‌ها وجود ندارد؛ ساده‌ترین مدل استفاده شده است."
      : `مدل ${modelLabel(view.model)} با WAPE ${pct(chosen.wape)} انتخاب شده است. در بک‌تست rolling-origin، مدلی که کمترین خطا روی دادهٔ دیده‌نشده را داشته برنده است.`;

    $("model-meta").textContent = view.test_start && view.test_end
      ? `${view.test_start} تا ${view.test_end}` : "";
    $("model-table").querySelector("tbody").innerHTML = (view.models || []).map((r, i) => `
      <tr class="${i === 0 ? "is-total" : ""}">
        <td class="name">${esc(modelLabel(r.model))}</td>
        <td>${pct(r.wape)}</td><td>${pct(r.bias)}</td>
        <td>${pct(r.coverage_90)}</td><td>${n(r.windows)}</td></tr>`).join("")
      || '<tr class="empty-row"><td colspan="5">تاریخچه برای بک‌تست کافی نبود.</td></tr>';
    $("backtest-note").textContent = view.note || "";

    const recon = view.reconciliation || [];
    $("reconciliation-box").hidden = !recon.length;
    const grouped = {};
    recon.forEach((r) => {
      grouped[r.method] = grouped[r.method] || {};
      grouped[r.method][r.level] = r.wape;
    });
    $("reconciliation-table").querySelector("tbody").innerHTML = Object.entries(grouped).map(([method, vals]) => `
      <tr><td class="name">${esc(method)}</td>
      <td>${pct(vals.TOTAL)}</td><td>${pct(vals.LEAF)}</td></tr>`).join("");
    $("reconciliation-note").textContent =
      "این جدول فقط برای انتخاب روش فنی است؛ جمع کل و برگ‌ها در خروجی همیشه سازگار می‌مانند.";
    $("backtest-chart-note").textContent =
      "نمودار تطبیق تاریخچه با پیش‌بینی مدل برنده در قدم بعد (نتیجه) دیده می‌شود.";
  }

  /* ---------- 5 · drivers ---------- */
  function renderDrivers(data) {
    const report = data.global_model && data.global_model.drivers;
    if (!report || !report.drivers || !report.drivers.length) {
      $("drivers-empty").hidden = false;
      return;
    }
    $("drivers-box").hidden = false;
    $("drivers-note").textContent =
      (report.method === "global_importance" ? "اهمیت سراسری ویژگی" : "سهم ویژگی در همین پیش‌بینی")
      + "؛ این اعداد تخصیص مدل هستند، نه رابطهٔ علّی.";
    $("driver-list").innerHTML = report.drivers.map((d) => `
      <li class="driver ${d.direction === "decrease" ? "is-down" : "is-up"}">
        <span class="driver-name">${esc(d.feature)}</span>
        <span class="driver-bar"><span style="width:${Math.max(2, Math.round(d.share * 100))}%"></span></span>
        <span class="driver-share">${pct(d.share)}</span></li>`).join("");

    const changed = ((data.regimes && data.regimes.entities) || [])
      .filter((e) => (e.changepoints || []).length);
    if (changed.length) {
      $("regimes-box").hidden = false;
      $("regime-list").innerHTML = changed.map((e) => `
        <li class="regime">${esc(e.entity_id)} — ${n(e.days_since_changepoint)} روز از آخرین تغییر سطح</li>`).join("");
      $("regimes-note").textContent =
        "از دادهٔ قبل از مبدأ محاسبه شده است؛ توصیف تاریخچه است، نه پیش‌بینی آینده.";
    }
  }

  /* ---------- 6 · result ---------- */
  function renderKpis(data) {
    const total = data.total_forecast || {};
    const horizon = Number(data.horizon || 30);
    const sum = (obj) => Object.values(obj || {}).reduce((acc, v) => acc + Number(v || 0), 0);
    const d1 = total["D+1"];
    const end = total["D+" + horizon];
    const base = data.total_last_actual;
    const change = base && d1 ? (d1 - base) / base : null;
    const cards = [
      ["جمع سفارش دوره", n(sum(total)), `افق ${n(horizon)} روز`, "kpi-lead", null],
      ["جمع GMV دوره", data.total_gmv_forecast ? n(sum(data.total_gmv_forecast)) : "—", "برآورد ارزش ناخالص", "kpi-lead", null],
      ["پیش‌بینی فردا", n(d1), change == null ? "بدون مبنای مقایسه"
        : `${pct(Math.abs(change))} نسبت به آخرین روز`, "kpi-accent",
        change == null ? null : (change >= 0 ? "up" : "down")],
      ["پیش‌بینی روز " + n(horizon), n(end), "انتهای افق", "", null],
      ["مدل انتخاب‌شده", modelLabel(data.model), "بر اساس بک‌تست", "", null],
    ];
    $("kpis").innerHTML = cards.map((c) => `
      <div class="kpi ${c[3]}"><span class="kpi-label">${c[0]}</span>
      <strong class="kpi-value">${esc(c[1])}</strong>
      <span class="kpi-sub">${c[4] ? `<span class="trend trend-${c[4]}" aria-hidden="true"></span>` : ""}${esc(c[2])}</span></div>`).join("");
    $("result-sub").textContent =
      `${data.date_start || ""} تا ${data.date_end || ""} · افق ${n(horizon)} روز · مدل ${modelLabel(data.model)}`;
  }

  function renderEntitySwitch() {
    const entities = Object.keys((state.forecast && state.forecast.chart && state.forecast.chart.entities) || {});
    if (!entities.includes(state.entity)) state.entity = "TOTAL";
    const box = $("entity-switch");
    box.innerHTML = entities.map((e) => `
      <button type="button" class="chip" data-entity="${esc(e)}" aria-pressed="${e === state.entity}">
        <strong>${esc(e)}</strong><small>${e === "TOTAL" ? "کل کسب‌وکار" : "موجودیت"}</small></button>`).join("");
    box.querySelectorAll("[data-entity]").forEach((b) => b.addEventListener("click", () => {
      state.entity = b.dataset.entity;
      renderEntitySwitch();
      renderChart();
    }));
  }

  function chartSeries() {
    const entities = (state.forecast && state.forecast.chart && state.forecast.chart.entities) || {};
    return (entities[state.entity] || {})[state.metric] || null;
  }

  function renderChartTable(series) {
    const labels = [].concat(series.history.labels || [], series.forecast.labels || []);
    const values = [].concat(series.history.values || [], series.forecast.values || []);
    const lows = new Array((series.history.values || []).length).fill(null).concat(series.forecast.lower || []);
    const highs = new Array((series.history.values || []).length).fill(null).concat(series.forecast.upper || []);
    $("chart-table").querySelector("tbody").innerHTML = labels.map((label, i) => `
      <tr><td>${esc(label)}</td><td class="num">${n(values[i])}</td>
      <td class="num">${n(lows[i])}</td><td class="num">${n(highs[i])}</td></tr>`).join("");
  }

  function renderChart() {
    const svg = $("chart");
    svg.innerHTML = "";
    const series = chartSeries();
    if (!series || !series.history || !series.history.values.length) {
      svg.innerHTML = '<text x="410" y="150" text-anchor="middle">داده‌ای برای این شاخص موجود نیست</text>';
      $("chart-table").querySelector("tbody").innerHTML = "";
      return;
    }
    const history = series.history.values;
    const forecast = series.forecast.values || [];
    const upper = series.forecast.upper || [];
    const lower = series.forecast.lower || [];
    const max = Math.max.apply(null, history.concat(forecast, upper).filter((v) => v != null).concat([1])) * 1.1;
    const W = 820, H = 300, pad = { l: 55, r: 15, t: 18, b: 32 };
    const total = history.length + forecast.length;
    const x = (i) => pad.l + (i / Math.max(total - 1, 1)) * (W - pad.l - pad.r);
    const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - Number(v || 0) / max);
    const ns = "http://www.w3.org/2000/svg";
    const node = (tag, attrs) => {
      const el = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
      return el;
    };
    for (let i = 0; i <= 4; i += 1) {
      const yy = y((max / 4) * i);
      svg.appendChild(node("line", { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, class: "grid" }));
      const label = node("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" });
      label.textContent = n((max / 4) * i);
      svg.appendChild(label);
    }
    if (upper.length && lower.length) {
      const up = upper.map((v, i) => `${x(history.length - 1 + i)},${y(v)}`);
      const lo = lower.map((v, i) => `${x(history.length - 1 + i)},${y(v)}`).reverse();
      svg.appendChild(node("path", { d: "M" + up.join("L") + "L" + lo.join("L") + "Z", class: "band" }));
    }
    const line = (values, offset) => "M" + values.map((v, i) => `${x(offset + i)},${y(v)}`).join("L");
    svg.appendChild(node("path", { d: line(history, 0), class: "actual" }));
    svg.appendChild(node("path", { d: line(forecast, history.length - 1), class: "forecast" }));
    svg.appendChild(node("line", {
      x1: x(history.length - 1), x2: x(history.length - 1), y1: pad.t, y2: H - pad.b, class: "split",
    }));
    [
      [0, series.history.labels[0]],
      [history.length - 1, "امروز"],
      [total - 1, series.forecast.labels[series.forecast.labels.length - 1]],
    ].forEach(([i, text]) => {
      const label = node("text", { x: x(i), y: H - 8, "text-anchor": "middle" });
      label.textContent = text || "";
      svg.appendChild(label);
    });
    renderChartTable(series);
  }

  function renderEntityTable(data) {
    const rows = data.entities_table || [];
    const horizon = Number(data.horizon || 30);
    const points = Array.from(new Set([1, Math.min(7, horizon), horizon]));
    $("entity-table").querySelector("thead").innerHTML = `
      <tr><th>موجودیت</th><th>نوع</th>
      ${points.map((d) => `<th>سفارش D+${n(d)}</th>`).join("")}
      <th>جمع ${n(horizon)} روزه</th><th>GMV دوره</th><th>AOV D+1</th></tr>`;
    $("entity-table").querySelector("tbody").innerHTML = rows.map((r) => `
      <tr class="${r.entity_type === "TOTAL" ? "is-total" : ""}">
        <td class="name">${esc(r.entity_id)}</td>
        <td>${esc(typeNames[r.entity_type] || r.entity_type)}</td>
        ${points.map((d) => `<td class="num">${n((r.points || {})["d" + d])}</td>`).join("")}
        <td class="num">${n(r.total)}</td><td class="num">${n(r.gmv_total)}</td>
        <td class="num">${n(r.aov_d1)}</td></tr>`).join("");
  }

  function renderWarnings(data) {
    const warnings = data.warnings || [];
    $("warnings-meta").textContent = n(warnings.length) + " مورد";
    $("warnings").innerHTML = warnings.length
      ? warnings.map((w) => `<li>${esc(w)}</li>`).join("")
      : '<li class="muted">هیچ هشداری ثبت نشد.</li>';
  }

  function renderForecast(data) {
    state.forecast = data;
    ["step-backtest", "step-drivers", "step-result"].forEach((id) => { $(id).hidden = false; });
    renderKpis(data);
    renderBacktest(data);
    renderDrivers(data);
    renderEntitySwitch();
    renderEntityTable(data);
    renderChart();
    renderWarnings(data);
    populateSensitivity();
    state.step = 4;
    renderSteps();
    announce("پیش‌بینی و بک‌تست آماده شد.");
  }

  /* ---------- 7 · deliver ---------- */
  function renderComparison(data) {
    state.comparison = data;
    $("monthly-box").hidden = false;
    const scenarios = data.scenarios || [];
    const totals = scenarios.filter((r) => r.vertical === "VERTICALS");
    $("monthly-kpis").innerHTML = [
      ["ماه آینده", data.next_month, "سه سناریو"],
      ["ماه‌های واقعی", n((data.past_months || []).length), "ماه کامل"],
      ["مدل", modelLabel(data.model), data.validity && data.validity.low_confidence ? "اعتبار کم" : "اعتبار کافی"],
    ].map((x) => `
      <div class="fact"><span class="fact-label">${x[0]}</span>
      <strong class="fact-value">${esc(x[1])}</strong>
      <span class="fact-sub">${esc(x[2])}</span></div>`).join("");
    $("monthly-table").querySelector("thead").innerHTML =
      "<tr><th>سناریو</th><th>ورتیکال</th><th>سفارش</th><th>GMV</th><th>انحراف نسبی</th></tr>";
    $("monthly-table").querySelector("tbody").innerHTML = totals.map((r) => `
      <tr class="${r.scenario === "realistic" ? "is-total" : ""}">
        <td>${esc(r.month)} · ${esc(r.scenario)}</td>
        <td class="name">${esc(r.vertical)}</td>
        <td class="num">${n(r.orders)}</td><td class="num">${n(r.gmv)}</td>
        <td>${pct(r.sigma_rel)}</td></tr>`).join("");
    $("monthly-note").textContent = (data.validity && data.validity.note) || "";
    $("monthly-link").hidden = false;
    $("monthly-link").href = "/comparison?run_id=" + encodeURIComponent(state.runId || "");
    state.step = 7;
    renderSteps();
  }

  function renderPackage(info) {
    $("package-box").hidden = false;
    $("package-facts").innerHTML = [
      ["ردیف خروجی", n(info.rows), "قالب تحویل"],
      ["روزهای پیش‌بینی", n(info.days), "از مبدأ تا هدف"],
      ["دسته‌ها", n(info.categories), "ردیف‌های دسته‌ای"],
    ].map((x) => `
      <div class="fact"><span class="fact-label">${x[0]}</span>
      <strong class="fact-value">${esc(x[1])}</strong>
      <span class="fact-sub">${esc(x[2])}</span></div>`).join("");
    const sample = info.sample || [];
    $("package-table").querySelector("thead").innerHTML = sample.length
      ? "<tr>" + Object.keys(sample[0]).map((k) => `<th>${esc(k)}</th>`).join("") + "</tr>" : "";
    $("package-table").querySelector("tbody").innerHTML = sample.map((r) => `
      <tr>${Object.values(r).map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("");
  }

  function populateSensitivity() {
    const entities = Object.keys((state.forecast && state.forecast.chart && state.forecast.chart.entities) || {})
      .filter((e) => e !== "TOTAL");
    $("sens-entity").innerHTML = entities.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
    $("sensitivity-box").hidden = !entities.length;
  }

  function renderSensitivity(result) {
    state.sensitivity = result;
    const direction = result.delta >= 0 ? "delta-up" : "delta-down";
    $("sens-result").innerHTML = `
      <span class="fact-label">اثر فرض روی ${esc(result.entity_id)} با تغییر ${n1(result.delta_pct)}٪</span>
      <strong class="fact-value">${n(result.scenario_total)} سفارش</strong>
      <span class="fact-sub">پایه: ${n(result.base_total)} ·
        تغییر کل: <b class="${direction}">${n(result.delta)}</b> (${pct(result.delta_share)})</span>
      ${result.scenario_gmv != null
        ? `<span class="fact-sub">GMV: ${n(result.base_gmv)} → ${n(result.scenario_gmv)}</span>` : ""}`;
  }

  /* ---------- actions ---------- */
  async function upload() {
    clear("upload-error");
    const file = $("file").files[0];
    if (!file) { show("upload-error", "ابتدا فایل اکسل را انتخاب کنید."); return; }
    setBusy("progress", "upload-btn", true, "در حال خواندن فایل و اجرای بررسی‌های کیفیت…");
    try {
      const data = await api("/api/runs", { method: "POST", body: form({ file, horizon: $("horizon").value }) });
      state.runId = data.run.run_id;
      state.token = data.token;
      renderQuality(data);
      renderPreview(data.preview, state.quality.source);
      go("data");
      announce("فایل خوانده شد؛ پیش‌نمایش و گزارش کیفیت آماده است.");
    } catch (error) {
      show("upload-error", error.message);
    } finally {
      setBusy("progress", "upload-btn", false);
    }
  }

  async function runForecast() {
    clear("run-error");
    if (!state.runId || !state.token) {
      show("run-error", "ابتدا فایل را بارگذاری و کیفیت آن را تأیید کنید.");
      return;
    }
    setBusy("run-progress", "run-btn", true, "در حال اجرای پیش‌بینی، بک‌تست و سناریوها…");
    try {
      const data = await api(`/api/runs/${state.runId}/forecast`, {
        method: "POST",
        body: form({
          quality_token: state.token,
          model: $("setup-model").value,
          horizon: $("setup-horizon").value,
        }),
      });
      renderForecast(data);
      go("backtest");
    } catch (error) {
      show("run-error", error.message);
    } finally {
      setBusy("run-progress", "run-btn", false);
    }
  }

  async function monthly() {
    clear("deliver-error");
    setBusy("deliver-progress", "monthly-btn", true, "در حال ساخت داشبورد ماهانه از همان فایل…");
    try {
      const data = await api(`/api/runs/${state.runId}/comparison`, {
        method: "POST",
        body: form({ quality_token: state.token, months: 6 }),
      });
      renderComparison(data);
    } catch (error) {
      show("deliver-error", error.message);
    } finally {
      setBusy("deliver-progress", "monthly-btn", false);
    }
  }

  async function packageDownload() {
    clear("deliver-error");
    setBusy("deliver-progress", "package-btn", true, "در حال ساخت بستهٔ تحویل…");
    try {
      const preview = await api(`/api/runs/${state.runId}/package/preview`);
      if (!preview.ready) {
        const response = await fetch(`/api/runs/${state.runId}/package`);
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || "ساخت بستهٔ تحویل ناموفق بود.");
        }
        const blob = await response.blob();
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `forecast-${state.runId}.xlsx`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 4000);
        renderPackage(await api(`/api/runs/${state.runId}/package/preview`));
      } else {
        renderPackage(preview);
      }
    } catch (error) {
      show("deliver-error", error.message);
    } finally {
      setBusy("deliver-progress", "package-btn", false);
    }
  }

  async function sensitivity() {
    clear("sensitivity-error");
    try {
      const data = await api(`/api/runs/${state.runId}/sensitivity`, {
        method: "POST",
        body: form({ entity_id: $("sens-entity").value, delta_pct: $("sens-delta").value }),
      });
      renderSensitivity(data);
    } catch (error) {
      show("sensitivity-error", error.message);
    }
  }

  /* ---------- wiring ---------- */
  function bind() {
    $("upload-form").addEventListener("submit", (e) => { e.preventDefault(); upload(); });
    $("dropzone").addEventListener("click", () => $("file").click());
    $("dropzone").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); }
    });
    $("file").addEventListener("change", () => {
      const file = $("file").files[0];
      if (file) { $("file-name").textContent = file.name; $("file-name").hidden = false; }
    });
    ["dragenter", "dragover"].forEach((name) => $("dropzone").addEventListener(name, (e) => {
      e.preventDefault(); $("dropzone").classList.add("dragover");
    }));
    ["dragleave", "drop"].forEach((name) => $("dropzone").addEventListener(name, (e) => {
      e.preventDefault(); $("dropzone").classList.remove("dragover");
    }));
    $("dropzone").addEventListener("drop", (e) => {
      if (e.dataTransfer.files[0]) {
        $("file").files = e.dataTransfer.files;
        $("file").dispatchEvent(new Event("change"));
      }
    });

    $("to-quality").addEventListener("click", () => go("quality"));
    $("recheck-btn").addEventListener("click", () => $("upload-form").requestSubmit());
    $("ack").addEventListener("change", (e) => { $("approve-btn").disabled = !e.target.checked; });
    $("approve-btn").addEventListener("click", () => {
      go("setup");
      announce("کیفیت تأیید شد؛ تنظیم اجرا فعال است.");
    });
    $("setup-horizon").addEventListener("change", (e) => { $("horizon").value = e.target.value; });
    $("setup-model").addEventListener("change", (e) => {
      $("model-override-note").hidden = e.target.value === "auto";
    });
    $("run-btn").addEventListener("click", runForecast);
    $("monthly-btn").addEventListener("click", monthly);
    $("package-btn").addEventListener("click", packageDownload);
    $("sens-btn").addEventListener("click", sensitivity);
    $("sens-reset").addEventListener("click", () => {
      state.sensitivity = null;
      $("sens-result").innerHTML = '<p class="muted">فرض پاک شد؛ عددهای پایه نمایش داده می‌شوند.</p>';
    });
    document.querySelectorAll("[data-sens]").forEach((b) => b.addEventListener("click", () => {
      $("sens-delta").value = b.dataset.sens;
      sensitivity();
    }));
    document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
    document.querySelectorAll("#metric-switch .seg").forEach((b) => b.addEventListener("click", () => {
      state.metric = b.dataset.metric;
      document.querySelectorAll("#metric-switch .seg").forEach((x) => x.classList.toggle("is-active", x === b));
      renderChart();
    }));
  }

  async function loadHistory() {
    try {
      const data = await api("/api/runs?limit=8");
      const runs = data.runs || [];
      $("history-meta").textContent = n(runs.length) + " اجرا";
      const tbody = $("history-table").querySelector("tbody");
      tbody.innerHTML = runs.map((r) => `
        <tr><td>${stamp(r.created_at)}</td><td class="name">${esc(r.file)}</td>
        <td>${esc(r.origin || "—")}</td><td>${n(r.horizon)}</td>
        <td>${esc(modelLabel(r.model))}</td>
        <td>${n(Object.values(r.steps || {}).filter(Boolean).length)} از ۷</td>
        <td><button class="btn btn-secondary btn-sm" data-restore="${esc(r.run_id)}">باز کردن</button></td></tr>`).join("")
        || '<tr class="empty-row"><td colspan="7">اجرای قبلی وجود ندارد.</td></tr>';
      tbody.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", () => restoreRun(b.dataset.restore)));
    } catch (error) {
      /* history is a convenience; the page must work without it */
    }
  }

  async function restoreRun(id) {
    try {
      const data = await api(`/api/runs/${id}`);
      state.runId = data.run.run_id;
      state.quality = data.quality;
      state.token = (data.quality && data.quality.token) || null;
      if (data.preview) renderPreview(data.preview, data.run.file);
      if (data.quality) renderQuality(data.quality);
      if (data.forecast) renderForecast(data.forecast);
      if (data.comparison) renderComparison(data.comparison);
      if (data.sensitivity) renderSensitivity(data.sensitivity);
      announce("اجرای قبلی بازیابی شد.");
      go(data.forecast ? "result" : "data");
    } catch (error) {
      show("upload-error", error.message);
    }
  }

  bind();
  loadHistory();
  renderSteps();
})();
