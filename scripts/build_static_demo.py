#!/usr/bin/env python
"""Render a static, self-contained demo of the studio for GitHub Pages.

GitHub Pages serves static files only, so the FastAPI app cannot run there.
This script runs the real pipeline on a synthetic workbook and bakes the
result into a single HTML page: the numbers, the chart and the tables are
genuine pipeline output, just pre-computed at build time.

Usage:
    python scripts/build_static_demo.py --out docs
"""

from __future__ import annotations

import argparse
import html
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import run_pipeline

ENTITIES = [
    ("FASHION", 420, 1_450_000),
    ("H&B", 260, 980_000),
    ("GOLD", 130, 4_200_000),
    ("DIGITAL", 180, 1_100_000),
    ("FMCG", 150, 620_000),
    ("Home", 110, 2_300_000),
    ("Services", 95, 1_800_000),
    ("Snapp-Venture", 60, 700_000),
]

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_number(value, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "—"
    return f"{value:,.{digits}f}".translate(FA_DIGITS)


def build_workbook(path: Path, days: int = 180, seed: int = 11) -> Path:
    """A workbook shaped like the commercial data-model export."""
    rng = np.random.default_rng(seed)
    start = date.today() - timedelta(days=days)
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        weekday = day.weekday()
        # Friday is the Iranian weekend: a deep, real dip.
        weekly = 0.58 if weekday == 4 else 1.0 + 0.06 * np.sin(2 * np.pi * i / 7)
        trend = 1 + 0.0018 * i
        for vertical, base, aov in ENTITIES:
            for method in ("INSTALLMENT", "PAY_NOW"):
                orders = max(
                    0,
                    int(round(base * weekly * trend * (1 + rng.normal(0, 0.05)) / 2)),
                )
                rows.append(
                    {
                        "vertical": vertical,
                        "Date": day.isoformat(),
                        "category": "ventures" if vertical == "Snapp-Venture" else "general",
                        "Year": day.year,
                        "Month": day.month,
                        "payment_method_type": method,
                        "Orders": orders,
                        "GMV": int(orders * aov * (1 + rng.normal(0, 0.03))),
                        "Active Merchants": 5,
                    }
                )
    frame = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Sheet1", index=False)
    return path


def svg_chart(history: list[float], forecast: list[float], lower: list[float],
              upper: list[float], labels: list[str]) -> str:
    """A native SVG line chart: history, forecast and a 90% band."""
    W, H = 900, 320
    pad = {"top": 20, "right": 18, "bottom": 34, "left": 74}
    inner_w = W - pad["left"] - pad["right"]
    inner_h = H - pad["top"] - pad["bottom"]

    values = history + forecast + upper
    top = max(values) * 1.08 or 1.0
    total = len(history) + len(forecast)

    def x(i: int) -> float:
        return pad["left"] + (i / max(total - 1, 1)) * inner_w

    def y(v: float) -> float:
        return pad["top"] + inner_h - (v / top) * inner_h

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="نمودار پیش‌بینی">']

    for i in range(5):
        value = top / 4 * i
        yy = y(value)
        parts.append(
            f'<line x1="{pad["left"]}" x2="{W - pad["right"]}" y1="{yy:.1f}" y2="{yy:.1f}" '
            'stroke="#eef1f6" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad["left"] - 10}" y="{yy + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#8a93a6">{fa_number(value)}</text>'
        )

    split = len(history) - 1
    upper_pts = "L".join(f"{x(split + i):.1f},{y(v):.1f}" for i, v in enumerate(upper))
    lower_pts = "L".join(
        f"{x(split + i):.1f},{y(v):.1f}" for i, v in reversed(list(enumerate(lower)))
    )
    parts.append(f'<path d="M{upper_pts}L{lower_pts}Z" fill="#cfdcf7" opacity="0.55"/>')

    hist_path = "L".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(history))
    parts.append(
        f'<path d="M{hist_path}" fill="none" stroke="#55617a" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    fc_path = "L".join(f"{x(split + i):.1f},{y(v):.1f}" for i, v in enumerate(forecast))
    parts.append(
        f'<path d="M{fc_path}" fill="none" stroke="#1c5fd4" stroke-width="2.2" '
        'stroke-dasharray="5 4" stroke-linejoin="round"/>'
    )
    parts.append(
        f'<line x1="{x(split):.1f}" x2="{x(split):.1f}" y1="{pad["top"]}" '
        f'y2="{pad["top"] + inner_h}" stroke="#cfd5e1" stroke-dasharray="3 3"/>'
    )

    for index, text in ((0, labels[0]), (split, "امروز"), (total - 1, labels[-1])):
        parts.append(
            f'<text x="{x(index):.1f}" y="{H - 10}" text-anchor="middle" '
            f'font-size="11" fill="#8a93a6">{html.escape(text)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def render(result, out_dir: Path) -> Path:
    summary = result.summary()
    assert result.mart is not None and result.forecast is not None

    mart = result.mart
    total_id = "TOTAL"
    history = mart[
        (mart["entity_id"] == total_id) & mart["successful_orders"].notna()
    ].sort_values("date").tail(90)
    forecast = result.forecast[
        (result.forecast["entity_id"] == total_id)
        & result.forecast["reconciled_yhat"].notna()
    ].sort_values("horizon")

    labels = [str(d) for d in history["date"]] + [str(d) for d in forecast["target_date"]]
    chart = svg_chart(
        [float(v) for v in history["successful_orders"]],
        [float(v) for v in forecast["reconciled_yhat"]],
        [float(v) for v in forecast["lower_90"]],
        [float(v) for v in forecast["upper_90"]],
        labels,
    )

    d1 = summary["total_forecast"].get("D+1")
    d7 = summary["total_forecast"].get("D+7")
    d30 = summary["total_forecast"].get(f"D+{summary['horizon']}")
    gmv1 = summary["total_gmv_forecast"].get("D+1")
    aov1 = summary["total_aov_forecast"].get("D+1")

    kpis = [
        ("آخرین مقدار واقعی", fa_number(summary["total_last_actual"]),
         f"سفارش موفق در {summary['date_end']}", ""),
        ("پیش‌بینی فردا (D+1)", fa_number(d1), "روز اول افق", "kpi-accent"),
        ("پیش‌بینی هفته آینده (D+7)", fa_number(d7), "روز هفتم افق", ""),
        ("پیش‌بینی روز ۳۰", fa_number(d30), "انتهای افق", "kpi-good"),
        ("GMV فردا (D+1)", fa_number(gmv1), "سفارش × AOV", "kpi-accent"),
        ("AOV فردا (D+1)", fa_number(aov1), "میانگین ارزش هر سفارش", ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><span class="kpi-label">{html.escape(label)}</span>'
        f'<span class="kpi-value">{value}</span>'
        f'<span class="kpi-sub">{html.escape(sub)}</span></div>'
        for label, value, sub, cls in kpis
    )

    gmv_by_entity: dict[str, dict] = {}
    if result.gmv_forecast is not None:
        for entity_id, chunk in result.gmv_forecast.groupby("entity_id"):
            chunk = chunk.sort_values("horizon")
            gmv_by_entity[str(entity_id)] = {
                "gmv_d1": float(chunk["gmv_yhat"].iloc[0]),
                "gmv_total": float(chunk["gmv_yhat"].sum()),
                "aov_d1": float(chunk["aov_yhat"].iloc[0]),
            }

    rows = []
    for entity_id, chunk in result.forecast.groupby("entity_id"):
        chunk = chunk.sort_values("horizon")
        by_h = chunk.set_index("horizon")["reconciled_yhat"]
        extra = gmv_by_entity.get(str(entity_id), {})
        rows.append(
            {
                "id": str(entity_id),
                "type": str(chunk["entity_type"].iloc[0]),
                "d1": by_h.get(1),
                "d7": by_h.get(7),
                "d30": by_h.get(len(by_h)),
                "total": by_h.sum(),
                "gmv_d1": extra.get("gmv_d1"),
                "gmv_total": extra.get("gmv_total"),
                "aov_d1": extra.get("aov_d1"),
            }
        )
    rows.sort(key=lambda r: (r["type"] != "TOTAL", r["type"], r["id"]))

    type_label = {"TOTAL": "کل", "VENTURE": "ونچر", "VERTICAL": "ورتیکال"}
    body_rows = "".join(
        f'<tr class="{"is-total" if r["type"] == "TOTAL" else ""}">'
        f'<td>{html.escape(r["id"])}</td>'
        f'<td>{type_label.get(r["type"], r["type"])}</td>'
        f'<td>{fa_number(r["d1"])}</td><td>{fa_number(r["d7"])}</td>'
        f'<td>{fa_number(r["d30"])}</td><td>{fa_number(r["total"])}</td>'
        f'<td>{fa_number(r["gmv_d1"])}</td><td>{fa_number(r["gmv_total"])}</td>'
        f'<td>{fa_number(r["aov_d1"])}</td></tr>'
        for r in rows
    )

    model_rows = "".join(
        f'<tr class="{"is-total" if i == 0 else ""}">'
        f'<td>{html.escape(m["model"])}</td>'
        f'<td>{fa_number(m["wape"] * 100, 1)}٪</td>'
        f'<td>{fa_number(m["bias"] * 100, 1)}٪</td></tr>'
        for i, m in enumerate(summary["backtest_ranking"])
    ) or '<tr><td colspan="3" class="empty">تاریخچه کافی برای بک‌تست نبود.</td></tr>'

    warning_items = "".join(
        f'<li class="is-warn">{html.escape(w)}</li>' for w in summary["warnings"]
    ) or '<li class="empty">هیچ هشداری ثبت نشد.</li>'

    page = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>استودیو پیش‌بینی سفارش | دموی استاتیک</title>
<link rel="stylesheet" href="styles.css"/>
</head>
<body>
<a class="skip-link" href="#main">پرش به محتوای اصلی</a>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark" aria-hidden="true"></span>
    <div>
      <h1>استودیو پیش‌بینی سفارش</h1>
      <p class="brand-sub">موتور پیش‌بینی روزانهٔ سفارش، AOV و GMV</p>
    </div>
  </div>
  <nav class="topnav" aria-label="ناوبری اصلی">
    <a href="#results">نتیجه</a>
    <a href="#quality">کیفیت داده</a>
    <a href="https://github.com/raoufkhoshnazarsp/Order-Forecasting-Engine">مخزن</a>
  </nav>
</header>

<main id="main">
  <section class="hero">
    <h2>از فایل اکسل تا پیش‌بینی ۳۰ روز آینده</h2>
    <p>
      این صفحه یک <strong>دموی استاتیک</strong> است: اعداد و نمودار زیر خروجی واقعی موتور
      روی یک فایل نمونهٔ ۱۸۰ روزه‌اند که هنگام ساخت همین صفحه محاسبه شده‌اند.
      برای اجرای زنده با فایل خودتان، اپلیکیشن را از مخزن اجرا کنید.
    </p>
    <p class="muted">
      مدل انتخابی: <strong>{html.escape(summary["model"])}</strong> ·
      بازهٔ داده: {summary["date_start"]} تا {summary["date_end"]} ·
      افق: {fa_number(summary["horizon"])} روز
    </p>
  </section>

  <section id="results">
    <div class="kpi-grid">{kpi_html}</div>

    <div class="card">
      <div class="card-head">
        <h3>مسیر پیش‌بینی کل کسب‌وکار — سفارش موفق</h3>
        <span class="badge">بازهٔ ۹۰٪</span>
      </div>
      <div class="chart-wrap">
        {chart}
        <div class="legend">
          <span><i class="dot dot-actual"></i>واقعی</span>
          <span><i class="dot dot-forecast"></i>پیش‌بینی</span>
          <span><i class="dot dot-band"></i>بازهٔ ۹۰٪</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h3>پیش‌بینی به تفکیک موجودیت</h3>
        <span class="muted">مقادیر گردشده و سازگار با جمع کل</span>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr>
            <th scope="col">موجودیت</th><th scope="col">نوع</th>
            <th scope="col">سفارش D+1</th><th scope="col">سفارش D+7</th>
            <th scope="col">سفارش روز آخر</th><th scope="col">جمع سفارش دوره</th>
            <th scope="col">GMV D+1</th><th scope="col">جمع GMV دوره</th>
            <th scope="col">AOV D+1</th>
          </tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
      </div>
    </div>

    <div class="card" id="quality">
      <div class="card-head"><h3>کیفیت داده و مدل</h3></div>
      <div class="split">
        <div>
          <h4>رتبه‌بندی مدل‌ها (میانگین بک‌تست)</h4>
          <div class="table-wrap">
            <table class="table table-compact">
              <thead><tr><th scope="col">مدل</th><th scope="col">WAPE</th><th scope="col">Bias</th></tr></thead>
              <tbody>{model_rows}</tbody>
            </table>
          </div>
        </div>
        <div>
          <h4>هشدارها</h4>
          <ul class="warning-list">{warning_items}</ul>
        </div>
      </div>
    </div>
  </section>
</main>

<footer class="footer">
  <span>دموی استاتیک — خروجی واقعی موتور، محاسبه‌شده هنگام ساخت صفحه</span>
  <span>BNPL Forecasting Engine</span>
</footer>
</body>
</html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")

    css_source = Path(__file__).resolve().parents[1] / "src" / "serving" / "static" / "styles.css"
    shutil.copyfile(css_source, out_dir / "styles.css")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        workbook = build_workbook(Path(tmp) / "demo.xlsx", days=args.days)
        result = run_pipeline(workbook, horizon=30, model="auto")

    index = render(result, Path(args.out))
    print(f"Static demo written to {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
