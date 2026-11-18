"""Pre-flight data quality gate.

Every run passes through this gate before any forecast is computed. Each check
returns a status:

* ``pass`` — nothing to see.
* ``warn``  — the run may continue, but the user must acknowledge it.
* ``fail``  — the run is blocked; the file cannot be forecast safely.

The gate is deliberately independent of the model: it only looks at the source
workbook, so a failure always means "the data is wrong", never "the model is
unhappy".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.ingestion.excel import (
    ExcelInputError,
    available_payment_methods,
    load_category_facts,
    normalize_method,
)
from src.serving.horizon import history_days, plan_horizon, window_advice

PASS, WARN, FAIL = "pass", "warn", "fail"
_STATUS_RANK = {PASS: 0, WARN: 1, FAIL: 2}

# The seven verticals the delivery format always reports.
CORE_VERTICALS = ("DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES")

MIN_HISTORY_DAYS = 120
RECOMMENDED_HISTORY_DAYS = 400
MAX_GAP_DAYS = 7
PARTIAL_DAY_RATIO = 0.70


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str
    rows: int = 0
    samples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "rows": self.rows,
            "samples": self.samples,
        }


@dataclass
class QualityReport:
    checks: list[Check]
    facts: pd.DataFrame
    source: str
    token: str
    origin: date | None = None
    days: int = 0
    plan: dict | None = None
    window: dict | None = None
    payment_method: str = ""
    payment_methods: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=_STATUS_RANK.get, default=PASS)

    @property
    def blocked(self) -> bool:
        return self.status == FAIL

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "token": self.token,
            "status": self.status,
            "blocked": self.blocked,
            "origin": self.origin.isoformat() if self.origin else None,
            "days": self.days,
            "plan": self.plan,
            "window": self.window,
            "payment_method": self.payment_method,
            "payment_methods": self.payment_methods,
            "counts": {
                s: sum(1 for c in self.checks if c.status == s)
                for s in (PASS, WARN, FAIL)
            },
            "checks": [c.as_dict() for c in self.checks],
        }


def _sample(values) -> list[str]:
    return [str(v) for v in list(values)[:5]]


def _check_columns(facts: pd.DataFrame) -> Check:
    required = {"date", "vertical", "category", "orders", "gmv"}
    missing = sorted(required - set(facts.columns))
    if missing:
        return Check(
            "columns",
            "ستون‌های لازم",
            FAIL,
            f"ستون‌های غایب: {', '.join(missing)}",
        )
    return Check(
        "columns",
        "ستون‌های لازم",
        PASS,
        "همهٔ ستون‌های لازم (تاریخ، ورتیکال، دسته، سفارش، GMV) موجودند.",
    )


def _check_dates(facts: pd.DataFrame) -> Check:
    bad = facts["date"].isna()
    if bad.any():
        return Check(
            "dates",
            "تاریخ‌های نامعتبر",
            FAIL,
            f"{int(bad.sum())} ردیف تاریخ قابل‌خواندن ندارد.",
            rows=int(bad.sum()),
        )
    return Check("dates", "تاریخ‌های نامعتبر", PASS, "همهٔ ردیف‌ها تاریخ معتبر دارند.")


def _check_duplicates(diagnostics: dict[str, int]) -> Check:
    duplicates = int(diagnostics.get("duplicate_rows", 0))
    if duplicates:
        return Check(
            "duplicates",
            "ردیف‌های تکراری",
            WARN,
            f"{duplicates} ردیف با کلید تاریخ×ورتیکال×دسته تکراری است؛ "
            "در ورودی جمع می‌شوند (معمولاً چند روش پرداخت).",
            rows=duplicates,
        )
    return Check("duplicates", "ردیف‌های تکراری", PASS, "هیچ کلید تکراری‌ای وجود ندارد.")


def _check_negatives(facts: pd.DataFrame) -> Check:
    neg = (facts["orders"] < 0) | (facts["gmv"] < 0)
    if neg.any():
        return Check(
            "negatives",
            "مقادیر منفی",
            FAIL,
            f"{int(neg.sum())} ردیف سفارش یا GMV منفی دارد.",
            rows=int(neg.sum()),
            samples=_sample(facts.loc[neg, "date"].dt.date),
        )
    return Check("negatives", "مقادیر منفی", PASS, "هیچ مقدار منفی‌ای وجود ندارد.")


def _check_zeros(facts: pd.DataFrame) -> Check:
    zero = facts["orders"].eq(0)
    if not zero.any():
        return Check("zeros", "صفرهای واقعی", PASS, "هیچ ردیف با سفارش صفر وجود ندارد.")
    share = float(zero.mean())
    status = WARN if share > 0.10 else PASS
    return Check(
        "zeros",
        "صفرهای واقعی",
        status,
        f"{int(zero.sum())} ردیف ({share:.1%}) سفارش صفر دارد؛ صفر واقعی است و گم‌شده شمرده نمی‌شود.",
        rows=int(zero.sum()),
    )


def _check_continuity(facts: pd.DataFrame) -> Check:
    days = pd.Series(sorted(facts["date"].dropna().unique()))
    if len(days) < 2:
        return Check("continuity", "پیوستگی تاریخ", FAIL, "کمتر از دو روز داده وجود دارد.")
    gaps = days.diff().dt.days.dropna()
    worst = int(gaps.max())
    if worst > MAX_GAP_DAYS:
        return Check(
            "continuity",
            "پیوستگی تاریخ",
            FAIL,
            f"بزرگ‌ترین شکاف {worst} روز است (حد مجاز {MAX_GAP_DAYS}).",
            rows=int((gaps > MAX_GAP_DAYS).sum()),
        )
    if worst > 2:
        return Check(
            "continuity",
            "پیوستگی تاریخ",
            WARN,
            f"بزرگ‌ترین شکاف {worst} روز است؛ روزهای غایب صفر نمی‌شوند.",
            rows=int((gaps > 2).sum()),
        )
    return Check("continuity", "پیوستگی تاریخ", PASS, "تاریخ‌ها روزانه و پیوسته‌اند.")


def _check_partial_day(facts: pd.DataFrame) -> Check:
    daily = facts.groupby("date", as_index=False)["orders"].sum().sort_values("date")
    if len(daily) < 8:
        return Check("partial_day", "روز ناقص پایانی", PASS, "تاریخچه برای این بررسی کوتاه است.")
    last = daily.iloc[-1]
    weekday = pd.Timestamp(last["date"]).day_of_week
    same = daily[daily["date"].dt.day_of_week == weekday].iloc[:-1].tail(4)
    if same.empty:
        return Check("partial_day", "روز ناقص پایانی", PASS, "مبنای مقایسه کافی نیست.")
    median = float(same["orders"].median())
    if median > 0 and float(last["orders"]) < PARTIAL_DAY_RATIO * median:
        return Check(
            "partial_day",
            "روز ناقص پایانی",
            WARN,
            f"روز {last['date'].date()} فقط {float(last['orders']) / median:.0%} میانهٔ همان روز هفته است؛ "
            "از آموزش کنار گذاشته می‌شود.",
            rows=1,
            samples=[str(last["date"].date())],
        )
    return Check("partial_day", "روز ناقص پایانی", PASS, "روز پایانی کامل به نظر می‌رسد.")


def _check_verticals(facts: pd.DataFrame) -> Check:
    present = {str(v).strip().upper().replace("_", "-") for v in facts["vertical"].unique()}
    missing = [v for v in CORE_VERTICALS if v not in present]
    if missing:
        return Check(
            "verticals",
            "پوشش ورتیکال‌ها",
            WARN,
            f"این ورتیکال‌ها در فایل نیستند: {', '.join(missing)}",
            rows=len(missing),
            samples=missing,
        )
    return Check(
        "verticals",
        "پوشش ورتیکال‌ها",
        PASS,
        f"هر ۷ ورتیکال اصلی موجودند ({len(present)} ورتیکال در کل).",
    )


def _check_history(facts: pd.DataFrame, horizon: int) -> Check:
    days = int(facts["date"].nunique())
    if days < MIN_HISTORY_DAYS:
        return Check(
            "history",
            "طول تاریخچه",
            WARN,
            f"{days} روز داده موجود است؛ حداقل {MIN_HISTORY_DAYS} روز توصیه می‌شود "
            f"و افق {horizon} روزه ممکن است بی‌ثبات باشد.",
            rows=days,
        )
    if days < RECOMMENDED_HISTORY_DAYS:
        return Check(
            "history",
            "طول تاریخچه",
            PASS,
            f"{days} روز داده؛ کافی برای افق {horizon} روزه، اما برای اعتبارسنجی سالانه "
            f"{RECOMMENDED_HISTORY_DAYS} روز توصیه می‌شود.",
            rows=days,
        )
    return Check(
        "history",
        "طول تاریخچه",
        PASS,
        f"{days} روز داده؛ برای اعتبارسنجی سالانه کافی است.",
        rows=days,
    )


def _check_aov(facts: pd.DataFrame) -> Check:
    rows = facts[facts["orders"] > 0]
    if rows.empty:
        return Check("aov", "سازگاری GMV و سفارش", WARN, "ردیفی با سفارش مثبت وجود ندارد.")
    aov = rows["gmv"] / rows["orders"]
    median = float(aov.median())
    if median <= 0:
        return Check("aov", "سازگاری GMV و سفارش", FAIL, "میانهٔ AOV صفر است؛ GMV با سفارش نمی‌خواند.")
    outliers = (aov > median * 20) | (aov < median / 20)
    if outliers.any():
        return Check(
            "aov",
            "سازگاری GMV و سفارش",
            WARN,
            f"{int(outliers.sum())} ردیف AOV بیش از ۲۰ برابر دور از میانه دارد؛ "
            "احتمال جابه‌جایی ستون سفارش و GMV.",
            rows=int(outliers.sum()),
            samples=_sample(rows.loc[outliers, "date"].dt.date),
        )
    return Check(
        "aov",
        "سازگاری GMV و سفارش",
        PASS,
        f"میانهٔ AOV {median:,.0f} است و هیچ ردیف پرتی وجود ندارد.",
    )


def check_workbook(
    path: str | Path,
    *,
    horizon: int | None = None,
    payment_method: str | None = None,
) -> QualityReport:
    """Run every pre-flight check on a workbook and return the report.

    ``horizon=None`` means "to the end of next calendar month", resolved from
    the last day in the file. An explicit value is honoured but capped.

    ``payment_method`` restricts every check — and every later step that uses
    this report's facts — to one payment method. The token still hashes the
    whole file, so approving the file once covers any slice of it.
    """
    path = Path(path)
    token = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    method = normalize_method(payment_method)
    methods = available_payment_methods(path)

    try:
        facts, diagnostics = load_category_facts(
            path, with_diagnostics=True, payment_method=method or None
        )
    except ExcelInputError as exc:
        return QualityReport(
            checks=[Check("load", "خواندن فایل", FAIL, str(exc))],
            facts=pd.DataFrame(),
            source=path.name,
            token=token,
            payment_method=method,
            payment_methods=methods,
        )

    days = history_days(facts)
    origin_ts = facts["date"].max()
    origin = origin_ts.date() if pd.notna(origin_ts) else None
    plan = plan_horizon(origin, requested=horizon, history_days=days) if origin else None
    effective = plan.horizon if plan else (horizon or 30)

    checks = [
        _check_columns(facts),
        _check_dates(facts),
        _check_duplicates(diagnostics),
        _check_negatives(facts),
        _check_zeros(facts),
        _check_continuity(facts),
        _check_partial_day(facts),
        _check_verticals(facts),
        _check_history(facts, effective),
        _check_aov(facts),
    ]
    return QualityReport(
        checks=checks,
        facts=facts,
        source=path.name,
        token=token,
        origin=origin,
        days=days,
        plan=plan.as_dict() if plan else None,
        window=window_advice(days),
        payment_method=method,
        payment_methods=methods,
    )


def require_quality(
    path: str | Path,
    token: str | None,
    *,
    horizon: int | None = None,
    payment_method: str | None = None,
) -> QualityReport:
    """Re-run the gate and refuse to continue unless the user acknowledged it.

    The token is the hash of the exact file the user reviewed, so a different
    upload can never ride on an earlier approval.
    """
    report = check_workbook(path, horizon=horizon, payment_method=payment_method)
    if report.blocked:
        failed = [c.title for c in report.checks if c.status == FAIL]
        raise QualityGateError(
            "کیفیت داده تأیید نشد: " + "، ".join(failed)
        )
    if token != report.token:
        raise QualityGateError(
            "پیش از محاسبه باید گزارش کیفیت داده را ببینید و تأیید کنید."
        )
    return report


class QualityGateError(ValueError):
    """Raised when the quality gate blocks a run."""
