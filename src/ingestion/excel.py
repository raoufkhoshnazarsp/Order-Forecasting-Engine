"""Excel ingestion: the engine's only data entry point.

No database connection exists anywhere in this project. A workbook is read,
its layout is auto-detected, headers are mapped to canonical fields, and the
result is normalized into the same leaf-fact contract the rest of the engine
already consumes (``date, entity_type, entity_id, successful_orders``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src import config

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_CHAR_MAP = str.maketrans(
    {
        **{p: str(i) for i, p in enumerate(PERSIAN_DIGITS)},
        **{a: str(i) for i, a in enumerate(ARABIC_DIGITS)},
        "ي": "ی",
        "ك": "ک",
        "‌": " ",  # ZWNJ
        " ": " ",
    }
)


class ExcelInputError(ValueError):
    """Raised when a workbook cannot be interpreted safely."""


@dataclass
class SheetReport:
    sheet: str
    layout: str
    rows_read: int
    rows_used: int
    mapped_columns: dict[str, str] = field(default_factory=dict)
    ignored_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "layout": self.layout,
            "rows_read": self.rows_read,
            "rows_used": self.rows_used,
            "mapped_columns": self.mapped_columns,
            "ignored_columns": self.ignored_columns,
            "missing_required": self.missing_required,
            "warnings": self.warnings,
        }


@dataclass
class ExcelLoadResult:
    facts: pd.DataFrame
    reports: list[SheetReport]
    source_file: str

    @property
    def warnings(self) -> list[str]:
        return [w for report in self.reports for w in report.warnings]


def normalize_text(value: Any) -> str:
    """Lowercase, unify Persian characters, collapse separators."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).translate(_CHAR_MAP).strip().lower()
    text = re.sub(r"[\s_\-./\\]+", " ", text)
    return text.strip()


def _header_key(value: Any) -> str:
    return normalize_text(value).replace(" ", "")


def load_excel_config() -> dict[str, Any]:
    return config.load_yaml("excel.yaml")


def build_header_lookup(excel_config: dict[str, Any]) -> dict[str, str]:
    """Map every accepted header spelling to its canonical field name."""
    lookup: dict[str, str] = {}
    for canonical, spellings in excel_config["columns"].items():
        for spelling in spellings:
            lookup[_header_key(spelling)] = canonical
        lookup[_header_key(canonical)] = canonical
    return lookup


def map_columns(
    headers: Iterable[Any], lookup: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Return (canonical -> original header, ignored headers)."""
    mapped: dict[str, str] = {}
    ignored: list[str] = []
    for header in headers:
        key = _header_key(header)
        canonical = lookup.get(key)
        if canonical and canonical not in mapped:
            mapped[canonical] = header
        elif canonical:
            ignored.append(str(header))  # duplicate spelling, first wins
        else:
            ignored.append(str(header))
    return mapped, ignored


def detect_layout(mapped: dict[str, str], excel_config: dict[str, Any]) -> str | None:
    for layout, spec in excel_config["layouts"].items():
        if all(field_name in mapped for field_name in spec["required"]):
            return layout
    return None


def parse_dates(series: pd.Series) -> pd.Series:
    """Parse mixed Excel/string dates, including Persian digits.

    Persian workbooks often store dates in the Jalali calendar. A parsed year
    below 1700 cannot be Gregorian, so those values are converted from Jalali
    (via ``jdatetime``) instead of being taken literally.
    """
    import jdatetime

    cleaned = series.map(
        lambda v: str(v).translate(_CHAR_MAP).strip() if not isinstance(v, (datetime, date)) else v
    )
    parsed = pd.to_datetime(cleaned, errors="coerce", format="mixed", dayfirst=False)

    jalali_mask = parsed.notna() & (parsed.dt.year < 1700)
    if jalali_mask.any():
        def convert(value):
            if pd.isna(value):
                return value
            try:
                return jdatetime.date(value.year, value.month, value.day).togregorian()
            except ValueError:
                return pd.NaT
        parsed.loc[jalali_mask] = parsed.loc[jalali_mask].map(convert)
    return pd.to_datetime(parsed).dt.date


def _numeric(series: pd.Series) -> pd.Series:
    cleaned = series.map(
        lambda v: str(v).translate(_CHAR_MAP).replace(",", "").replace("٬", "").strip()
        if not isinstance(v, (int, float))
        else v
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _read_sheets(path: Path) -> dict[str, pd.DataFrame]:
    try:
        return pd.read_excel(path, sheet_name=None, dtype=object)
    except Exception as exc:  # noqa: BLE001 - surface a clear message
        raise ExcelInputError(f"Could not read workbook {path.name}: {exc}") from exc


def _classify_entity(row: pd.Series, excel_config: dict[str, Any]) -> tuple[str, str]:
    """Resolve (entity_type, entity_id) for a raw order row."""
    if "entity_id" in row and pd.notna(row.get("entity_id")):
        entity_id = str(row["entity_id"]).strip()
        entity_type = str(row.get("entity_type") or "").strip().upper()
        if entity_type not in {"VERTICAL", "VENTURE"}:
            entity_type = "VENTURE" if entity_id.upper().startswith("V") else "VERTICAL"
        return entity_type, entity_id

    vertical = normalize_text(row.get("vertical"))
    category = normalize_text(row.get("category"))
    markers = excel_config["venture_markers"]
    venture_verticals = {normalize_text(v) for v in markers["vertical"]}
    venture_categories = {normalize_text(v) for v in markers["category"]}

    if vertical in venture_verticals or category in venture_categories:
        venture_id = row.get("venture_id")
        if pd.isna(venture_id):
            # A venture bucket without a per-merchant id (the commercial
            # data-model export) is one venture entity named after its vertical.
            if vertical:
                return "VENTURE", str(row.get("vertical")).strip()
            return "UNMAPPED", ""
        return "VENTURE", str(venture_id).strip().lstrip("0") or "0"

    if vertical:
        return "VERTICAL", str(row.get("vertical")).strip()
    return "UNMAPPED", ""


def _raw_orders_to_facts(
    frame: pd.DataFrame, mapped: dict[str, str], excel_config: dict[str, Any], report: SheetReport
) -> pd.DataFrame:
    work = pd.DataFrame({canonical: frame[header] for canonical, header in mapped.items()})
    work["date"] = parse_dates(work["date"])
    before = len(work)
    work = work[work["date"].notna()]
    if len(work) < before:
        report.warnings.append(f"{before - len(work)} rows dropped: unparseable date")

    if "status" in work:
        allowed = {normalize_text(s) for s in excel_config["successful_statuses"]}
        status = work["status"].map(normalize_text)
        keep = status.isin(allowed)
        if not keep.any():
            report.warnings.append(
                "No row matched a successful status; all rows were kept. "
                "Check the status column values."
            )
        else:
            dropped = int((~keep).sum())
            if dropped:
                report.warnings.append(f"{dropped} non-successful rows excluded")
            work = work[keep]

    if "deleted_at" in work:
        deleted = work["deleted_at"].notna() & (work["deleted_at"].astype(str).str.strip() != "")
        if deleted.any():
            report.warnings.append(f"{int(deleted.sum())} soft-deleted rows excluded")
            work = work[~deleted]

    if "order_id" in work:
        before = len(work)
        work = work.drop_duplicates(subset=["order_id", "date"])
        if len(work) < before:
            report.warnings.append(f"{before - len(work)} duplicate order rows removed")

    classified = work.apply(lambda r: _classify_entity(r, excel_config), axis=1)
    work["entity_type"] = [c[0] for c in classified]
    work["entity_id"] = [c[1] for c in classified]

    grouped = (
        work.groupby(["date", "entity_type", "entity_id"], as_index=False)
        .size()
        .rename(columns={"size": "successful_orders"})
    )
    report.rows_used = int(grouped["successful_orders"].sum())
    return grouped


def _aggregated_to_facts(
    frame: pd.DataFrame, mapped: dict[str, str], report: SheetReport
) -> pd.DataFrame:
    work = pd.DataFrame({canonical: frame[header] for canonical, header in mapped.items()})
    work["date"] = parse_dates(work["date"])
    work["successful_orders"] = _numeric(work["successful_orders"])
    before = len(work)
    work = work[work["date"].notna() & work["successful_orders"].notna()]
    if len(work) < before:
        report.warnings.append(f"{before - len(work)} rows dropped: invalid date or order count")

    work["entity_id"] = work["entity_id"].astype(str).str.strip()
    if "entity_type" in work:
        work["entity_type"] = work["entity_type"].astype(str).str.strip().str.upper()
    else:
        work["entity_type"] = "VERTICAL"
    work.loc[~work["entity_type"].isin(["VERTICAL", "VENTURE"]), "entity_type"] = "VERTICAL"

    work["successful_orders"] = work["successful_orders"].round().astype("int64")
    if (work["successful_orders"] < 0).any():
        raise ExcelInputError("Workbook contains negative order counts")

    before = len(work)
    work = work.drop_duplicates(subset=["date", "entity_id"], keep="last")
    if len(work) < before:
        report.warnings.append(f"{before - len(work)} duplicate date/entity rows removed")

    report.rows_used = int(work["successful_orders"].sum())
    return work[["date", "entity_type", "entity_id", "successful_orders"]]


def _daily_metrics_to_facts(
    frame: pd.DataFrame, mapped: dict[str, str], excel_config: dict[str, Any],
    report: SheetReport,
) -> pd.DataFrame:
    """Commercial data-model export: date x vertical x category x payment method.

    Rows are aggregated to the leaf grain (date x entity). GMV is summed
    alongside orders so AOV can be derived later; it is never averaged.
    """
    work = pd.DataFrame({canonical: frame[header] for canonical, header in mapped.items()})
    work["date"] = parse_dates(work["date"])
    work["successful_orders"] = _numeric(work["successful_orders"])
    work["gmv"] = _numeric(work["gmv"])

    before = len(work)
    work = work[work["date"].notna() & work["successful_orders"].notna()]
    if len(work) < before:
        report.warnings.append(f"{before - len(work)} rows dropped: invalid date or order count")

    if (work["successful_orders"] < 0).any() or (work["gmv"] < 0).any():
        raise ExcelInputError("Workbook contains negative order counts or GMV")

    classified = work.apply(lambda r: _classify_entity(r, excel_config), axis=1)
    work["entity_type"] = [c[0] for c in classified]
    work["entity_id"] = [c[1] for c in classified]

    unmapped = work["entity_type"] == "UNMAPPED"
    if unmapped.any():
        report.warnings.append(
            f"{int(work.loc[unmapped, 'successful_orders'].sum())} orders have no vertical "
            "and are reported as UNMAPPED"
        )

    grouped = (
        work.groupby(["date", "entity_type", "entity_id"], as_index=False)
        .agg(successful_orders=("successful_orders", "sum"), gmv=("gmv", "sum"))
    )
    grouped["successful_orders"] = grouped["successful_orders"].round().astype("int64")
    report.rows_used = int(grouped["successful_orders"].sum())
    return grouped


def load_workbook(path: str | Path) -> ExcelLoadResult:
    """Read every sheet, auto-detect layout, and return normalized leaf facts."""
    path = Path(path)
    if not path.exists():
        raise ExcelInputError(f"Workbook not found: {path.resolve()}")
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
        raise ExcelInputError("Only .xlsx, .xlsm and .xls workbooks are supported")

    excel_config = load_excel_config()
    lookup = build_header_lookup(excel_config)
    sheets = _read_sheets(path)

    frames: list[pd.DataFrame] = []
    reports: list[SheetReport] = []
    for sheet_name, raw in sheets.items():
        if raw.empty:
            continue
        raw = raw.dropna(how="all")
        if raw.empty:
            continue
        mapped, ignored = map_columns(raw.columns, lookup)
        layout = detect_layout(mapped, excel_config)
        report = SheetReport(
            sheet=str(sheet_name),
            layout=layout or "unknown",
            rows_read=len(raw),
            rows_used=0,
            mapped_columns={k: str(v) for k, v in mapped.items()},
            ignored_columns=ignored,
        )
        if layout is None:
            required = {
                name: spec["required"] for name, spec in excel_config["layouts"].items()
            }
            report.missing_required = sorted(
                {f for req in required.values() for f in req} - set(mapped)
            )
            report.warnings.append(
                "Sheet skipped: no supported layout detected. "
                f"Missing one of {report.missing_required}"
            )
            reports.append(report)
            continue

        if layout == "raw_orders":
            facts = _raw_orders_to_facts(raw, mapped, excel_config, report)
        elif layout == "daily_metrics":
            facts = _daily_metrics_to_facts(raw, mapped, excel_config, report)
        else:
            facts = _aggregated_to_facts(raw, mapped, report)
        frames.append(facts)
        reports.append(report)

    if not frames:
        detail = "; ".join(
            f"{r.sheet}: {', '.join(r.warnings) or 'no data'}" for r in reports
        )
        raise ExcelInputError(f"No usable sheet found in {path.name}. {detail}")

    facts = pd.concat(frames, ignore_index=True)
    aggregations: dict[str, str] = {"successful_orders": "sum"}
    if "gmv" in facts.columns:
        aggregations["gmv"] = "sum"
    facts = (
        facts.groupby(["date", "entity_type", "entity_id"], as_index=False)
        .agg(aggregations)
        .sort_values(["date", "entity_type", "entity_id"])
        .reset_index(drop=True)
    )
    return ExcelLoadResult(facts=facts, reports=reports, source_file=path.name)


def load_category_facts(
    path: str | Path, *, with_diagnostics: bool = False
) -> pd.DataFrame | tuple[pd.DataFrame, dict[str, int]]:
    """Load the commercial export at its native grain: date x vertical x category.

    The engine's internal contract aggregates to date x entity, but the
    delivery workbook is category-level, so the category must survive
    ingestion. Rows are summed across payment methods only.

    With ``with_diagnostics`` the raw row count and the number of duplicate
    keys collapsed by that aggregation are returned alongside the frame, so
    the quality gate can report them instead of silently losing the signal.
    """
    path = Path(path)
    if not path.exists():
        raise ExcelInputError(f"Workbook not found: {path.resolve()}")

    excel_config = load_excel_config()
    lookup = build_header_lookup(excel_config)
    sheets = _read_sheets(path)

    frames: list[pd.DataFrame] = []
    for _sheet, raw in sheets.items():
        if raw.empty:
            continue
        raw = raw.dropna(how="all")
        mapped, _ignored = map_columns(raw.columns, lookup)
        if detect_layout(mapped, excel_config) != "daily_metrics":
            continue
        work = pd.DataFrame({c: raw[h] for c, h in mapped.items()})
        work["date"] = parse_dates(work["date"])
        work["orders"] = _numeric(work["successful_orders"])
        work["gmv"] = _numeric(work["gmv"])
        work["vertical"] = work["vertical"].astype(str).str.strip()
        work["category"] = (
            work["category"].astype(str).str.strip() if "category" in work else ""
        )
        frames.append(work[["date", "vertical", "category", "orders", "gmv"]])

    if not frames:
        raise ExcelInputError(
            f"No sheet in {path.name} has the daily_metrics layout "
            "(needs date, vertical, orders and GMV)."
        )

    facts = pd.concat(frames, ignore_index=True)
    facts = facts[facts["date"].notna() & facts["orders"].notna()].copy()
    if facts.empty:
        raise ExcelInputError("No usable rows in the workbook")
    facts["date"] = pd.to_datetime(facts["date"])

    keys = ["date", "vertical", "category"]
    diagnostics = {
        "rows_read": int(len(facts)),
        "duplicate_rows": int(facts.duplicated(subset=keys, keep=False).sum()),
    }
    grouped = (
        facts.groupby(keys, as_index=False)
        .agg(orders=("orders", "sum"), gmv=("gmv", "sum"))
        .sort_values(keys)
        .reset_index(drop=True)
    )
    if with_diagnostics:
        return grouped, diagnostics
    return grouped


def template_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two illustrative sheets written by :func:`write_template`."""
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=14, freq="D")
    rows = []
    for day in dates:
        for entity, base in (("FASHION", 120), ("GOLD", 40), ("V001", 25)):
            rows.append(
                {
                    "date": day.date().isoformat(),
                    "entity_id": entity,
                    "entity_type": "VENTURE" if entity.startswith("V") else "VERTICAL",
                    "successful_orders": base,
                }
            )
    daily = pd.DataFrame(rows)
    raw = pd.DataFrame(
        [
            {
                "date": dates[-1].date().isoformat(),
                "order_id": "ORD-1001",
                "venture_id": "4711",
                "status": "SETTLE",
                "merchant_type": "ONLINE",
                "vertical": "FASHION",
                "category": "apparel",
                "wallet_id": "W-1",
                "amount": 1_500_000,
                "voucher_discount_amount": 0,
            },
            {
                "date": dates[-1].date().isoformat(),
                "order_id": "ORD-1002",
                "venture_id": "9001",
                "status": "SETTLE",
                "merchant_type": "ONLINE",
                "vertical": "Snapp-Venture",
                "category": "ventures",
                "wallet_id": "W-2",
                "amount": 800_000,
                "voucher_discount_amount": 50_000,
            },
        ]
    )
    return daily, raw


def write_template(path: str | Path) -> Path:
    """Write a small, documented workbook so users know the expected shape."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    daily, raw = template_frames()
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        daily.to_excel(writer, sheet_name="daily_aggregated", index=False)
        raw.to_excel(writer, sheet_name="raw_orders", index=False)
    return path


def template_bytes() -> bytes:
    """The template workbook rendered in memory (for HTTP download)."""
    import io

    daily, raw = template_frames()
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        daily.to_excel(writer, sheet_name="daily_aggregated", index=False)
        raw.to_excel(writer, sheet_name="raw_orders", index=False)
    return buffer.getvalue()
