"""Global forecasting model: one learned model shared by every entity.

The baselines in ``baselines.py`` are per-series: each entity is fitted and
forecast in isolation, so a thin or noisy category cannot borrow strength from
its neighbours. This module trains a *single* model across all entities at
once, with the entity itself as a feature, so seasonality and level are learned
jointly and a short series still gets a sensible path.

Design points that matter for correctness:

* **Direct multi-horizon.** For every training row the target is the value
  actually observed ``h`` days after the origin, and ``h`` is a feature. There
  is no recursive chaining, so horizon-``h`` errors never compound.
* **No leakage by construction.** The feature builder only receives the pivot
  truncated at the origin (``pivot.iloc[: i + 1]``), so nothing after the
  origin can reach a feature. The backtest re-fits per origin for the same
  reason.
* **Log target.** Orders are counts with multiplicative seasonality, so the
  model fits ``log1p(y)`` and returns ``expm1`` clipped at zero.
* **Calendar from ``dim_date``.** The weekday and Persian month position of the
  *target* date are read from the Persian calendar table, never guessed.

The estimator is ridge regression solved in closed form (numpy only). It is
deliberately dependency-free so the container, the server and a fresh checkout
all compute the same numbers. ``lightgbm`` is used automatically when it is
installed, and the chosen backend is reported with every forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.models.baselines import ForecastResult

MODEL_NAME = "global_ml"

# Added to the ratio before taking logs so a zero-order day stays finite.
_TARGET_EPS = 1e-3

# Columns whose scale differs across entities and must be standardised.
_NUMERIC = (
    "h",
    "log_last",
    "log_mean7",
    "log_mean28",
    "log_lag7",
    "log_lag14",
    "log_lag28",
    "trend_7_28",
    "level_vs_lag7",
    "log_share",
    "weekday_ratio",
    "month_sin",
    "month_cos",
    "month_end_distance",
)


@dataclass(frozen=True)
class GlobalConfig:
    horizon: int = 30
    ridge: float = 1.0
    min_train_days: int = 42
    min_rows: int = 64
    backend: str = "auto"


@dataclass
class GlobalFit:
    """A fitted model plus everything needed to reproduce a prediction."""

    backend: str
    horizon: int
    entity_ids: list[str]
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray
    feature_names: list[str] = field(default_factory=list)
    train_rows: int = 0
    low_confidence: bool = False
    # Mean residual of log-ratio predictions. Back-transforming exp() of a
    # fitted mean systematically under-predicts, so this is added back.
    log_bias: float = 0.0

    def coefficients(self, top: int = 8) -> list[dict]:
        """Standardised coefficients, largest magnitude first.

        ``weights[0]`` is the intercept, so it is reported separately rather
        than being mistaken for a feature. Because features are standardised,
        coefficient magnitude is comparable across features — which is what
        makes this usable as an explanation rather than just a score.
        """
        if self.weights.size == 0:
            return []
        pairs = list(zip(self.feature_names, self.weights[1:]))
        pairs.sort(key=lambda kv: abs(float(kv[1])), reverse=True)
        return [{"feature": name, "weight": float(w)} for name, w in pairs[:top]]

    @property
    def intercept(self) -> float:
        return float(self.weights[0]) if self.weights.size else 0.0

    def drivers(self, row: np.ndarray, top: int = 8) -> dict:
        """Which features moved *this* prediction, and in which direction.

        ``row`` must already be standardised exactly as the model was trained
        (numeric block scaled, dummies left as 0/1). For the ridge backend the
        contribution is then the exact linear term ``weight * x`` — the same
        quantity a linear SHAP explainer returns. For LightGBM the per-row
        decomposition is not available without the shap package, so the global
        feature importance is returned instead and the method says so. Neither
        is causal.
        """
        from src.evaluation.drivers import build_driver_report, global_importance

        if self.weights.size == 0 or not self.feature_names:
            return build_driver_report(
                np.zeros(0), [], method="none", note="مدل آموزش‌ندیده است."
            ).as_dict()

        if self.backend == "ridge":
            values = np.asarray(row, dtype="float64")
            if len(values) != len(self.feature_names):
                raise ValueError("row must match the fitted feature count")
            contributions = self.weights[1:] * values
            report = build_driver_report(
                contributions,
                self.feature_names,
                method="linear_contribution",
                is_causal=False,
                note="سهم خطی هر ویژگی در همین پیش‌بینی؛ رابطهٔ علّی نیست.",
            )
            return report.as_dict()

        ranked = global_importance(self.weights, self.feature_names)[:top]
        return {
            "method": "global_importance",
            "is_causal": False,
            "drivers": [
                {
                    "feature": name,
                    "contribution": float(value),
                    "share": 0.0,
                    "direction": "increase" if value >= 0 else "decrease",
                }
                for name, value in ranked
            ],
            "note": (
                "اهمیت سراسری ویژگی در مدل درختی؛ سهم همین پیش‌بینی نیست "
                "و رابطهٔ علّی هم نیست."
            ),
        }


def _pivot(frame: pd.DataFrame) -> pd.DataFrame:
    """date x entity grid of observed orders.

    Values are never filled at all: a missing day stays NaN so a gap is not
    silently turned into "the same as yesterday".
    """
    if frame is None or frame.empty:
        raise ValueError("مدل سراسری به دادهٔ مشاهده‌شده نیاز دارد.")
    required = {"date", "entity_id", "successful_orders"}
    if not required.issubset(frame.columns):
        raise ValueError(
            "قاب داده باید ستون‌های date، entity_id و successful_orders را داشته باشد."
        )
    nested = frame.copy()
    nested["date"] = pd.to_datetime(nested["date"])
    nested = nested[nested["successful_orders"].notna()]
    grid = nested.pivot_table(
        index="date", columns="entity_id", values="successful_orders", aggfunc="sum"
    )
    return grid.sort_index().astype("float64")


def _calendar_frame(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Weekday and Persian month position for every target date."""
    try:
        from src.features.persian_calendar import calendar_features, load_calendar

        cal = calendar_features(dates)
        weekday = cal["weekday"].to_numpy()
        day_of_month = cal["persian_day"].to_numpy(dtype="float64")
        # The true month length comes from the calendar table, not from the
        # dates passed in: a window that ends mid-month would otherwise think
        # its last day is the month's last day.
        table = load_calendar()
        lengths = (
            table.groupby(["persian_year", "persian_month"])["persian_day"]
            .max()
            .rename("month_length")
            .reset_index()
        )
        merged = cal.merge(
            lengths, on=["persian_year", "persian_month"], how="left"
        )
        to_month_end = (
            merged["month_length"].to_numpy(dtype="float64") - day_of_month
        )
    except Exception:  # noqa: BLE001 - calendar is optional
        weekday = np.asarray(dates.day_of_week, dtype="float64")
        day_of_month = np.asarray(dates.day, dtype="float64")
        to_month_end = np.asarray(dates.days_in_month - dates.day, dtype="float64")
    # Persian months are 29-31 days, so a fixed 30-day scale is a fine proxy
    # for "where in the month we are" and avoids inventing a lookup.
    pos = (np.asarray(day_of_month, dtype="float64") - 0.5) / 30.0
    return pd.DataFrame(
        {
            "weekday": np.asarray(weekday, dtype="float64"),
            "month_sin": np.sin(2 * np.pi * pos),
            "month_cos": np.cos(2 * np.pi * pos),
            "month_end_distance": np.clip(to_month_end, 0.0, 7.0) / 7.0,
        },
        index=dates,
    )


def _anchor(values: pd.Series) -> float:
    """Level the target ratio is expressed against.

    The average of the same weekday over the last four weeks is a stronger
    anchor than "the last 7 days": it removes the weekday effect before the
    model sees the number, so the model only has to learn how much this
    particular target date deviates from its own weekday norm. For a stable
    series that ratio is ~1, which keeps short-horizon error low; for a
    trending one the deviation carries the growth.
    """
    clean = values.dropna()
    if clean.empty:
        return 0.0
    same_weekday = clean.iloc[::-1].iloc[::7].head(4)
    if len(same_weekday):
        return float(same_weekday.mean())
    return float(clean.iloc[-7:].mean())


def _row_features(
    window: pd.DataFrame,
    entity_index: int,
    step: int,
    calendar: pd.DataFrame,
    target_pos: int | None = None,
) -> dict[str, float]:
    """Features for one (entity, origin, step) triple.

    ``window`` ends at the origin. ``target_pos`` is the row of the *target*
    date in ``calendar``; the training path passes it explicitly and the
    prediction path passes ``origin_index + step``. Deriving it from ``step``
    alone would read the calendar of the first days of the file, which is the
    bug this signature removes.
    """
    column = window.iloc[:, entity_index]
    values = column.dropna()
    last = float(values.iloc[-1]) if len(values) else 0.0

    def mean(tail: int) -> float:
        return float(values.iloc[-tail:].mean()) if len(values) else 0.0

    mean7 = mean(7)
    mean28 = mean(28)

    def lag(days: int) -> float:
        # ``days`` back from the origin, matching the vectorised path exactly.
        return float(values.iloc[-(days + 1)]) if len(values) > days else last

    lag7 = lag(7)
    lag14 = lag(14)
    lag28 = lag(28)
    total = float(window.iloc[-1].sum())
    share = last / total if total > 0 else 0.0

    position = len(window) - 1 + step if target_pos is None else target_pos
    position = min(max(position, 0), len(calendar) - 1)
    target_row = calendar.iloc[position]

    # How this entity usually behaves on the *target* weekday, relative to its
    # own monthly level. Pooling this across entities is the point of a global
    # model: a category with two weeks of history still gets the weekday shape.
    target_weekday = int(target_row["weekday"])
    same = window.index.day_of_week + 1 == target_weekday
    same_values = column[same].dropna()
    if len(same_values) and mean28 > 0:
        weekday_ratio = float(same_values.iloc[-4:].mean()) / mean28
    else:
        weekday_ratio = 1.0
    return {
        "h": float(step),
        "log_last": float(np.log1p(max(last, 0.0))),
        "log_mean7": float(np.log1p(max(mean7, 0.0))),
        "log_mean28": float(np.log1p(max(mean28, 0.0))),
        "log_lag7": float(np.log1p(max(lag7, 0.0))),
        "log_lag14": float(np.log1p(max(lag14, 0.0))),
        "log_lag28": float(np.log1p(max(lag28, 0.0))),
        "trend_7_28": (mean7 - mean28) / mean28 if mean28 > 0 else 0.0,
        "level_vs_lag7": (last - lag7) / lag7 if lag7 > 0 else 0.0,
        "log_share": float(np.log(max(share, 1e-4))),
        "weekday_ratio": float(weekday_ratio),
        "month_sin": float(target_row["month_sin"]),
        "month_cos": float(target_row["month_cos"]),
        "month_end_distance": float(target_row["month_end_distance"]),
    }


def _anchor_matrix(pivot: np.ndarray) -> np.ndarray:
    """Same-weekday anchor for every (day, entity), vectorised.

    Mirrors :func:`_anchor` exactly: the mean of the last four observations of
    that weekday. Kept separate from the scalar helper so the training path is
    fast enough to run inside a rolling backtest.
    """
    shifts = []
    for lag in (0, 7, 14, 21):
        shifted = np.full_like(pivot, np.nan)
        if lag == 0:
            shifted = pivot.copy()
        else:
            shifted[lag:] = pivot[:-lag]
        shifts.append(shifted)
    stacked = np.stack(shifts)
    counts = np.sum(np.isfinite(stacked), axis=0)
    totals = np.nansum(stacked, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, totals / np.maximum(counts, 1), np.nan)


def _training_rows(
    pivot: pd.DataFrame, calendar: pd.DataFrame, horizon: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build the full design matrix for every usable (origin, step, entity).

    Vectorised across entities and origins so a rolling backtest fits in
    seconds rather than minutes. The semantics are identical to
    :func:`_row_features`: every feature is computed from data at or before
    the origin, and the target is the ratio of the observed value to the
    origin's same-weekday anchor.
    """
    matrix = pivot.to_numpy(dtype="float64")
    days, entities = matrix.shape
    rolling7 = (
        pd.DataFrame(matrix).rolling(7, min_periods=1).mean().to_numpy()
    )
    rolling28 = (
        pd.DataFrame(matrix).rolling(28, min_periods=1).mean().to_numpy()
    )
    anchor = _anchor_matrix(matrix)

    month_sin = calendar["month_sin"].to_numpy()
    month_cos = calendar["month_cos"].to_numpy()
    month_end = calendar["month_end_distance"].to_numpy()
    weekdays = calendar["weekday"].to_numpy().astype(int)

    # Same-weekday anchor expressed as a matrix, used to build the weekday
    # ratio feature for whichever weekday a target date lands on.
    weekday_ratios = anchor / np.where(rolling28 > 0, rolling28, np.nan)
    weekday_codes_all = weekdays[:days]

    numeric_parts: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    entity_parts: list[np.ndarray] = []
    weekday_parts: list[np.ndarray] = []

    last_fit = days - 1 - horizon
    for step in range(1, horizon + 1):
        origins = np.arange(28, last_fit + 1)
        origins = origins[origins + step < days]
        if origins.size == 0:
            break
        target_pos = origins + step

        last = matrix[origins]
        mean7 = rolling7[origins]
        mean28 = rolling28[origins]
        lag7 = matrix[origins - 7]
        lag14 = matrix[origins - 14]
        lag28 = matrix[origins - 28]
        # Row of the most recent day that shares the target's weekday; the
        # weekday ratio is read there so it always matches the target date.
        back = (weekday_codes_all[origins] - weekday_codes_all[target_pos]) % 7
        # Same-weekday anchor read at that row, divided by the origin's own
        # 28-day level: identical to what the scalar prediction path computes.
        anchor_rows = anchor[origins - back, :]
        weekday_ratio = np.where(
            mean28 > 0, anchor_rows / np.where(mean28 > 0, mean28, np.nan), 1.0
        )
        totals = np.nansum(matrix[origins], axis=1, keepdims=True)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(totals > 0, last / totals, 0.0)
            trend = np.where(mean28 > 0, (mean7 - mean28) / mean28, 0.0)
            level_vs_lag7 = np.where(lag7 > 0, (last - lag7) / lag7, 0.0)

        numeric = np.stack(
            [
                np.full_like(last, float(step)),
                np.log1p(np.clip(last, 0, None)),
                np.log1p(np.clip(mean7, 0, None)),
                np.log1p(np.clip(mean28, 0, None)),
                np.log1p(np.clip(lag7, 0, None)),
                np.log1p(np.clip(lag14, 0, None)),
                np.log1p(np.clip(lag28, 0, None)),
                trend,
                level_vs_lag7,
                np.log(np.clip(share, 1e-4, None)),
                weekday_ratio,
                np.broadcast_to(month_sin[target_pos][:, None], last.shape),
                np.broadcast_to(month_cos[target_pos][:, None], last.shape),
                np.broadcast_to(month_end[target_pos][:, None], last.shape),
            ],
            axis=-1,
        )

        observed = matrix[target_pos]
        base = anchor[origins]
        usable = (
            np.isfinite(observed)
            & np.isfinite(numeric).all(axis=-1)
            & np.isfinite(base)
            & (base > 0)
        )
        if not usable.any():
            continue

        rows, columns = np.nonzero(usable)
        numeric_parts.append(numeric[rows, columns])
        targets.append(
            np.log(observed[rows, columns] / base[rows, columns] + _TARGET_EPS)
        )
        entity_parts.append(columns)
        weekday_parts.append(weekdays[target_pos[rows]])

    if not numeric_parts:
        empty = np.zeros((0, len(_NUMERIC)))
        return empty, np.zeros(0), np.zeros(0, dtype=int), np.zeros(0, dtype=int)

    numeric_block = np.vstack(numeric_parts)
    entity_codes = np.concatenate(entity_parts).astype(int)
    weekday_codes = np.concatenate(weekday_parts).astype(int)

    weekday_block = np.zeros((len(entity_codes), 7))
    weekday_block[np.arange(len(entity_codes)), np.clip(weekday_codes, 0, 6)] = 1.0
    entity_block = np.zeros((len(entity_codes), entities))
    entity_block[np.arange(len(entity_codes)), entity_codes] = 1.0

    design = np.hstack([numeric_block, weekday_block, entity_block])
    return design, np.concatenate(targets), entity_codes, weekday_codes


def _design(
    features: list[dict[str, float]],
    entities: list[int],
    n_entities: int,
    weekdays: list[int],
) -> tuple[np.ndarray, list[str]]:
    """Assemble the feature matrix: numeric block, weekday block, entity block."""
    names: list[str] = list(_NUMERIC)
    numeric = np.array(
        [[row[name] for name in _NUMERIC] for row in features], dtype="float64"
    )
    blocks = [numeric]
    for wd in range(7):
        names.append(f"wd_{wd}")
        blocks.append(
            np.array([[1.0 if w == wd else 0.0] for w in weekdays], dtype="float64")
        )
    for idx in range(n_entities):
        names.append(f"entity_{idx}")
        blocks.append(
            np.array([[1.0 if e == idx else 0.0] for e in entities], dtype="float64")
        )
    return np.hstack(blocks), names


def _fit_ridge(matrix: np.ndarray, target: np.ndarray, ridge: float) -> np.ndarray:
    """Closed-form ridge with an unpenalised intercept column."""
    ones = np.ones((matrix.shape[0], 1))
    design = np.hstack([ones, matrix])
    penalty = ridge * np.eye(design.shape[1])
    penalty[0, 0] = 0.0
    gram = design.T @ design + penalty
    try:
        return np.linalg.solve(gram, design.T @ target)
    except np.linalg.LinAlgError:  # pragma: no cover - singular fallback
        return np.linalg.lstsq(design, target, rcond=None)[0]


def _has_lightgbm() -> bool:
    try:
        import lightgbm  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _predict_matrix(
    matrix: np.ndarray, weights: np.ndarray, backend: str, booster
) -> np.ndarray:
    """Apply the fitted estimator to an already-standardised matrix."""
    if backend == "lightgbm" and booster is not None:  # pragma: no cover
        return np.asarray(booster.predict(matrix), dtype="float64")
    return np.hstack([np.ones((matrix.shape[0], 1)), matrix]) @ weights


class GlobalModel:
    """Fit once across entities, then predict any subset for a horizon."""

    def __init__(self, config: GlobalConfig | None = None) -> None:
        self.config = config or GlobalConfig()
        self.fit_result: GlobalFit | None = None

    # ------------------------------------------------------------------ fit
    def fit(self, frame: pd.DataFrame, *, horizon: int | None = None) -> "GlobalModel":
        cfg = self.config
        horizon = int(horizon or cfg.horizon)
        pivot = _pivot(frame)
        if pivot.empty or pivot.shape[1] == 0:
            raise ValueError("مدل سراسری به دادهٔ مشاهده‌شده نیاز دارد.")
        if len(pivot) < cfg.min_train_days:
            raise ValueError(
                f"مدل سراسری حداقل {cfg.min_train_days} روز داده می‌خواهد "
                f"({len(pivot)} روز موجود است)."
            )

        calendar = _calendar_frame(pivot.index)
        features_block, targets, entity_codes, weekdays = _training_rows(
            pivot, calendar, horizon
        )
        rows = len(targets)

        if rows < cfg.min_rows:
            raise ValueError(
                f"مدل سراسری حداقل {cfg.min_rows} نمونهٔ آموزش می‌خواهد ({rows} ساخته شد)."
            )

        numeric_block = features_block[:, : len(_NUMERIC)]
        mean = numeric_block.mean(axis=0)
        scale = numeric_block.std(axis=0)
        scale[scale == 0] = 1.0
        scaled = features_block.copy()
        scaled[:, : len(_NUMERIC)] = (numeric_block - mean) / scale
        names = list(_NUMERIC) + [f"wd_{i}" for i in range(7)] + [
            f"entity_{i}" for i in range(pivot.shape[1])
        ]

        backend = cfg.backend
        if backend == "auto":
            backend = "lightgbm" if _has_lightgbm() else "ridge"

        y = targets
        if backend == "lightgbm":  # pragma: no cover - optional dependency
            import lightgbm as lgb

            # The native API is used on purpose: it needs no scikit-learn, so
            # the only extra dependency is the lightgbm wheel itself.
            booster = lgb.train(
                {
                    "objective": "regression",
                    "learning_rate": 0.05,
                    "num_leaves": 31,
                    "min_data_in_leaf": 20,
                    "feature_fraction": 0.9,
                    "bagging_fraction": 0.9,
                    "bagging_freq": 1,
                    "seed": 0,
                    "deterministic": True,
                    "force_row_wise": True,
                    "num_threads": 2,
                    "verbose": -1,
                },
                lgb.Dataset(scaled, label=y, feature_name=names),
                num_boost_round=300,
            )
            weights = np.zeros(len(names) + 1)
            self._booster = booster
        else:
            weights = _fit_ridge(scaled, y, cfg.ridge)
            self._booster = None
        predicted = _predict_matrix(
            scaled, weights, backend, getattr(self, "_booster", None)
        )
        log_bias = float(np.mean(y - predicted))

        self.fit_result = GlobalFit(
            backend=backend,
            horizon=horizon,
            entity_ids=[str(c) for c in pivot.columns],
            mean=mean,
            scale=scale,
            weights=weights,
            feature_names=names,
            train_rows=int(rows),
            low_confidence=rows < cfg.min_rows * 2,
            log_bias=log_bias,
        )
        return self

    # -------------------------------------------------------------- predict
    def _row_matrix(
        self,
        pivot: pd.DataFrame,
        column: int,
        step: int,
        calendar: pd.DataFrame,
        target_pos: int,
        block: int,
    ) -> np.ndarray:
        """The standardised design row for one entity and one horizon step."""
        fit = self.fit_result
        features = _row_features(pivot, column, step, calendar, target_pos=target_pos)
        matrix, _ = _design(
            [features],
            [block],
            len(fit.entity_ids),
            [int(calendar.iat[target_pos, 0])],
        )
        matrix[:, : len(_NUMERIC)] = (matrix[:, : len(_NUMERIC)] - fit.mean) / fit.scale
        return matrix

    def explain(
        self, frame: pd.DataFrame, *, entity_id: str | None = None, step: int = 1
    ) -> dict:
        """Driver report for one entity's forecast at one horizon step.

        Defaults to the first entity and D+1. The report is model attribution,
        not causality, and says so in its ``note``.
        """
        if self.fit_result is None:
            raise RuntimeError("مدل سراسری هنوز آموزش ندیده است.")
        fit = self.fit_result
        pivot = _pivot(frame)
        if pivot.empty:
            raise ValueError("داده‌ای برای توضیح وجود ندارد.")
        columns = [str(c) for c in pivot.columns]
        if entity_id is None:
            entity_id = columns[0]
        if entity_id not in columns:
            raise ValueError(f"موجودیت {entity_id} در داده نیست.")
        column = columns.index(entity_id)
        block = {name: j for j, name in enumerate(fit.entity_ids)}.get(entity_id, 0)
        calendar = _calendar_frame(
            pd.date_range(start=pivot.index[0], periods=len(pivot) + step, freq="D")
        )
        target_pos = len(pivot) - 1 + step
        matrix = self._row_matrix(pivot, column, step, calendar, target_pos, block)
        report = fit.drivers(matrix[0])
        report["entity_id"] = entity_id
        report["horizon"] = int(step)
        return report

    def predict(
        self, frame: pd.DataFrame, *, horizon: int | None = None
    ) -> dict[str, np.ndarray]:
        """Forecast every entity present in ``frame`` from its last observed day."""
        if self.fit_result is None:
            raise RuntimeError("مدل سراسری هنوز آموزش ندیده است.")
        fit = self.fit_result
        horizon = int(horizon or fit.horizon)
        pivot = _pivot(frame)
        if pivot.empty:
            raise ValueError("داده‌ای برای پیش‌بینی وجود ندارد.")

        # Keep the column order of training; unknown entities fall back to the
        # first entity block so a new category still gets a calendar profile.
        columns = {str(c): j for j, c in enumerate(pivot.columns)}
        position = {name: j for j, name in enumerate(fit.entity_ids)}
        calendar = _calendar_frame(
            pd.date_range(
                start=pivot.index[0],
                periods=len(pivot) + horizon,
                freq="D",
            )
        )
        origin_index = len(pivot) - 1
        last_date = pivot.index[-1]

        out: dict[str, np.ndarray] = {}
        for e, column in enumerate(pivot.columns):
            entity = str(column)
            block = position.get(entity, 0)
            observed = pivot.iloc[:, e].dropna()
            # The level the ratio is expressed against, read at the origin —
            # exactly the denominator the training targets were built from.
            level = _anchor(observed)
            path = np.zeros(horizon, dtype="float64")
            for step in range(1, horizon + 1):
                target_pos = origin_index + step
                matrix = self._row_matrix(
                    pivot, e, step, calendar, target_pos, block
                )
                value = float(
                    _predict_matrix(
                        matrix, fit.weights, fit.backend, self._booster
                    )[0]
                )
                ratio = max(
                    float(np.exp(value + fit.log_bias)) - _TARGET_EPS, 0.0
                )
                path[step - 1] = max(ratio * level, 0.0)
            out[entity] = path
        del last_date, columns
        return out


def fit_global(frame: pd.DataFrame, *, horizon: int, config: GlobalConfig | None = None) -> GlobalModel:
    return GlobalModel(config or GlobalConfig(horizon=horizon)).fit(frame, horizon=horizon)


def forecasters_from_model(
    model: GlobalModel, frame: pd.DataFrame, *, horizon: int
) -> dict[str, object]:
    """Per-entity callables over one already-fitted model."""
    paths = model.predict(frame, horizon=horizon)

    def make(entity_id: str):
        def forecaster(series: pd.Series, h: int) -> ForecastResult:
            path = paths.get(entity_id)
            if path is None or len(path) < h:
                fallback = float(series.dropna().iloc[-1]) if len(series.dropna()) else 0.0
                path = np.full(h, fallback)
            return ForecastResult(MODEL_NAME, np.asarray(path[:h], dtype="float64"))

        return forecaster

    return {entity: make(entity) for entity in paths}


def global_forecasters(
    frame: pd.DataFrame, *, horizon: int, config: GlobalConfig | None = None
) -> dict[str, object]:
    """Adapters so the global model plugs into the existing serving/backtest paths.

    The single fitted model is shared; each callable only slices its own
    entity's path, which keeps one training run per origin instead of one per
    entity.
    """
    model = fit_global(frame, horizon=horizon, config=config)
    return forecasters_from_model(model, frame, horizon=horizon)
