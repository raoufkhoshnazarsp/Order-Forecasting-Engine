"""What the file actually contains, before any forecast is computed.

The quality gate answers "is this data usable?". The preview answers the
question the user has *first*: "did the engine read my file the way I meant
it?" — which sheets were picked up, which layouts were recognised, the date
range, the entities, and a small sample of real rows.

The loader returns leaf facts at ``date x entity`` with ``successful_orders``
and ``gmv``. The preview reads that grain directly, so it never disagrees with
what the forecast will see.
"""

from __future__ import annotations

import pandas as pd

SAMPLE_ROWS = 8

LAYOUT_LABELS = {
    "daily_metrics": "مدل دادهٔ تجاری (سفارش + GMV)",
    "daily_aggregated": "دادهٔ روزانهٔ تجمیع‌شده",
    "raw_orders": "سفارش‌های خام (یک ردیف = یک سفارش)",
}

LAYOUT_HINTS = {
    "daily_metrics": "هر ردیف یک روز، یک ورتیکال و یک دسته با تعداد سفارش و GMV است.",
    "daily_aggregated": "سفارش‌ها بر اساس روز و موجودیت جمع شده‌اند.",
    "raw_orders": "هر ردیف یک سفارش است؛ موتور خودش جمع می‌زند.",
}


def build_preview(facts: pd.DataFrame | None, sheets: list[dict]) -> dict:
    """A plain, JSON-ready description of the ingested data."""
    sheet_rows = [_sheet_view(s) for s in sheets]
    if facts is None or facts.empty:
        return {
            "rows": 0,
            "sheets": sheet_rows,
            "sample": [],
            "entities": [],
            "verticals": [],
        }

    frame = facts.copy()
    dates = pd.to_datetime(frame["date"])
    order = dates.sort_values()
    gaps = int((order.diff().dt.days > 1).sum()) if len(order) > 1 else 0

    entities: list[dict] = []
    if "entity_id" in frame.columns:
        for entity_id, chunk in frame.groupby("entity_id"):
            entity_type = (
                str(chunk["entity_type"].iloc[0]) if "entity_type" in chunk else "VERTICAL"
            )
            entities.append(
                {
                    "entity_id": str(entity_id),
                    "entity_type": entity_type,
                    "orders": _number(chunk["successful_orders"].sum())
                    if "successful_orders" in chunk
                    else None,
                    "gmv": _number(chunk["gmv"].sum()) if "gmv" in chunk else None,
                }
            )
    entities.sort(key=lambda e: (e["entity_type"] != "VERTICAL", e["entity_id"]))

    sample_cols = [
        c for c in ("date", "entity_type", "entity_id", "successful_orders", "gmv")
        if c in frame.columns
    ]
    tail = frame.sort_values(["date"]).tail(SAMPLE_ROWS)
    sample = [
        {c: (str(row[c]) if c == "date" else _number(row[c])) for c in sample_cols}
        for _, row in tail.iterrows()
    ]

    return {
        "rows": int(len(frame)),
        "days": int(dates.dt.normalize().nunique()),
        "date_start": str(dates.min().date()),
        "date_end": str(dates.max().date()),
        "gaps": gaps,
        "entities": entities,
        "verticals": [
            e["entity_id"] for e in entities if e["entity_type"] == "VERTICAL"
        ],
        "orders_total": _number(frame["successful_orders"].sum())
        if "successful_orders" in frame
        else None,
        "gmv_total": _number(frame["gmv"].sum()) if "gmv" in frame else None,
        "sheets": sheet_rows,
        "sample": sample,
    }


def _sheet_view(sheet: dict) -> dict:
    return {
        "sheet": sheet.get("sheet"),
        "layout": LAYOUT_LABELS.get(sheet.get("layout"), "نامشخص"),
        "layout_hint": LAYOUT_HINTS.get(
            sheet.get("layout"), "چیدمان این برگه شناخته نشد؛ ممکن است نادیده گرفته شود."
        ),
        "rows_used": sheet.get("rows_used"),
        "ignored_columns": sheet.get("ignored_columns") or [],
        "warnings": sheet.get("warnings") or [],
    }


def _number(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(number):
        return None
    return int(number) if number.is_integer() else round(number, 2)
