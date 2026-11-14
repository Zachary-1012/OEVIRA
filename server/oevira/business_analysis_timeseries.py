"""Source-scoped time-series decomposition, change detection and forecast evaluation."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import json
import math
import statistics
from typing import Any

from .business_analysis_economics import (
    _answer,
    _check_cancelled,
    _json_number_to_text,
    _md_table,
    _operating_observation,
    _requested_title,
    _section,
    _source_label,
    _string,
)


def _frequency_contiguous(previous: dict, current: dict) -> bool:
    first, second = previous["period"], current["period"]
    if first["frequency"] != second["frequency"] or first["timezone"] is None or first["timezone"] != second["timezone"]:
        return False
    if previous["_period_end"].toordinal() + 1 != current["_period_start"].toordinal():
        return False
    frequency = first["frequency"]
    if frequency == "custom":
        return (previous["_period_end"] - previous["_period_start"]).days == (
            current["_period_end"] - current["_period_start"]
        ).days
    return True


def _future_periods(last: dict, horizon: int) -> list[dict]:
    frequency = last["period"]["frequency"]
    timezone_name = last["period"]["timezone"]
    start, end = last["_period_start"], last["_period_end"]
    output = []
    for _ in range(horizon):
        start = end + timedelta(days=1)
        if frequency == "day":
            end = start
        elif frequency == "week":
            end = start + timedelta(days=6)
        elif frequency == "month":
            year, month = start.year, start.month
            end = date(year, month, calendar.monthrange(year, month)[1])
        elif frequency == "quarter":
            final_month = start.month + 2
            end = date(start.year, final_month, calendar.monthrange(start.year, final_month)[1])
        elif frequency == "year":
            end = date(start.year, 12, 31)
        elif frequency == "custom":
            duration = (last["_period_end"] - last["_period_start"]).days
            end = start + timedelta(days=duration)
            last = {**last, "_period_start": start, "_period_end": end}
        else:
            raise ValueError("forecast requires a regular daily, weekly, monthly, quarterly, yearly, or custom frequency")
        output.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "frequency": frequency, "timezone": timezone_name,
        })
        last = {**last, "_period_start": start, "_period_end": end,
                "period": {**last["period"], "start": start.isoformat(), "end": end.isoformat()}}
    return output


def _model_prediction(values: list[float], *, seasonal_period: int | None, steps: int,
                      method: str = "arima", start: int = 0, is_cancelled=None):
    import warnings
    import numpy as np
    from statsmodels.tools.sm_exceptions import ConvergenceWarning
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.forecasting.stl import STLForecast
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    _check_cancelled(is_cancelled)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        if method == "arima":
            if seasonal_period is None:
                fitted = ARIMA(np.asarray(values, dtype=float), order=(1, 1, 0)).fit()
            else:
                fitted = STLForecast(
                    np.asarray(values, dtype=float), ARIMA,
                    model_kwargs={"order": (1, 1, 0)},
                    period=seasonal_period, robust=True,
                ).fit()
        elif method == "ets":
            fitted = ExponentialSmoothing(
                np.asarray(values, dtype=float),
                trend="add",
                damped_trend=True,
                seasonal="add" if seasonal_period is not None else None,
                seasonal_periods=seasonal_period,
                initialization_method="estimated",
            ).fit(optimized=True)
        else:
            raise ValueError("FORECAST_METHOD_UNSUPPORTED")
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise ValueError("FORECAST_NOT_CONVERGED")
    _check_cancelled(is_cancelled)
    model_result = getattr(fitted, "model_result", fitted)
    convergence = getattr(model_result, "mle_retvals", None)
    if method == "arima":
        if not convergence or convergence.get("converged") is not True:
            raise ValueError("FORECAST_NOT_CONVERGED")
        if start:
            prediction = fitted.get_prediction(start=start, end=start + steps - 1)
        else:
            prediction = fitted.get_prediction(start=len(values), end=len(values) + steps - 1)
        mean = np.asarray(prediction.predicted_mean, dtype=float)
        intervals = np.asarray(prediction.conf_int(alpha=0.05), dtype=float)
    else:
        if convergence is not None and convergence.get("success") is not True:
            raise ValueError("FORECAST_NOT_CONVERGED")
        mean = np.asarray(fitted.forecast(steps), dtype=float)
        # Holt-Winters exposes point forecasts but no calibrated prediction interval.
        intervals = None
    if (
        len(mean) != steps
        or not np.isfinite(mean).all()
        or intervals is not None and (intervals.shape != (steps, 2) or not np.isfinite(intervals).all())
    ):
        raise ValueError("FORECAST_NUMERICAL_RESULT_INVALID")
    return mean, intervals, fitted


def _metrics(actual: list[float], predicted: list[float]) -> dict:
    import numpy as np

    errors = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    actual_values = np.asarray(actual, dtype=float)
    mae = float(np.mean(np.abs(errors)))
    rmse = float(np.sqrt(np.mean(errors ** 2)))
    mape = float(np.mean(np.abs(errors / actual_values)) * 100) if np.all(actual_values != 0) else None
    wape_denominator = float(np.sum(np.abs(actual_values)))
    wape = float(np.sum(np.abs(errors)) / wape_denominator * 100) if wape_denominator else None
    return {"mae": mae, "rmse": rmse, "mape_percent": mape, "wape_percent": wape}


def _evaluate_series(rows: list[dict], *, seasonal_period: int | None,
                     forecast_horizon: int | None, holdout_periods: int | None,
                     event_labels: dict[str, dict], forecast_method: str = "arima", is_cancelled=None) -> dict:
    _check_cancelled(is_cancelled)
    import numpy as np
    import ruptures
    import statsmodels
    from statsmodels.tsa.seasonal import STL

    metric = rows[0]["metric"]
    frequency = rows[0]["period"]["frequency"]
    timezone_name = rows[0]["period"]["timezone"]
    group_data = {
        "entity": rows[0]["entity"], "metric": metric, "scope": rows[0]["scope"],
        "period_frequency": frequency, "timezone": timezone_name,
        "observations": [], "method_state": "INSUFFICIENT", "reason": None,
    }
    values = [row["metric"]["value"] for row in rows]
    period_order = all(_frequency_contiguous(left, right) for left, right in zip(rows, rows[1:]))
    actual_only = all(row["metric"]["basis"] == "actual" for row in rows)
    review_ok = all(row["review_status"] in {"supported", "not_supplied"} for row in rows)
    timezone_ok = bool(timezone_name)
    value_complete = all(value is not None for value in values)
    if not period_order:
        group_data["reason"] = "PERIODS_HAVE_GAPS_OR_INCONSISTENT_FREQUENCY"
    elif not actual_only:
        group_data["reason"] = "ESTIMATES_AND_FORECASTS_ARE_NOT_MIXED_WITH_ACTUALS"
    elif not review_ok:
        group_data["reason"] = "SOURCE_REVIEW_STATUS_BLOCKS_MODEL_FIT"
    elif not timezone_ok:
        group_data["reason"] = "PERIOD_TIMEZONE_UNKNOWN"
    elif not value_complete:
        group_data["reason"] = "MISSING_VALUES_ARE_NOT_IMPUTED"
    elif len(rows) < 3:
        group_data["reason"] = "TOO_FEW_OBSERVATIONS_FOR_TREND_AND_FORECAST"
    if group_data["reason"] is not None:
        group_data["observations"] = [_public_time_observation(row) for row in rows]
        return group_data

    numeric_values = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("time-series values must be finite")
    n = len(rows)
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, np.asarray(numeric_values), 1)
    fitted_trend = (intercept + slope * x).tolist()
    series_std = float(np.std(numeric_values, ddof=1)) if n > 1 else 0.0
    can_stl = seasonal_period is not None and seasonal_period >= 2 and n >= 2 * seasonal_period
    decomposition = None
    if can_stl:
        _check_cancelled(is_cancelled)
        decomposed = STL(np.asarray(numeric_values, dtype=float), period=seasonal_period, robust=True).fit()
        _check_cancelled(is_cancelled)
        decomposition = {
            "trend": decomposed.trend.tolist(),
            "seasonal": decomposed.seasonal.tolist(),
            "residual": decomposed.resid.tolist(),
            "period": seasonal_period,
            "state": "READY",
        }
    else:
        decomposition = {
            "trend": fitted_trend,
            "seasonal": None,
            "residual": None,
            "period": seasonal_period,
            "state": "NOT_AVAILABLE",
            "reason": "DECLARED_SEASONAL_CYCLE_REQUIRES_TWO_COMPLETE_CYCLES" if seasonal_period else "SEASONAL_CYCLE_NOT_DECLARED",
        }

    change_points = {"state": "INSUFFICIENT", "break_indices": [], "penalty": None, "method": "PELT l2, jump=1"}
    if n >= 6 and series_std > 0:
        normalized = ((np.asarray(numeric_values) - np.mean(numeric_values)) / series_std).reshape(-1, 1)
        penalty = 3.0 * math.log(n)
        _check_cancelled(is_cancelled)
        breakpoints = ruptures.Pelt(model="l2", min_size=3, jump=1).fit(normalized).predict(pen=penalty)
        _check_cancelled(is_cancelled)
        change_points = {
            "state": "READY", "break_indices": breakpoints[:-1], "penalty": penalty,
            "method": "PELT l2 on standardized observations, min segment=3, jump=1",
        }
    elif series_std == 0:
        change_points["reason"] = "SERIES_HAS_NO_VARIATION"
    else:
        change_points["reason"] = "NEED_AT_LEAST_SIX_OBSERVATIONS_FOR_TWO_SEGMENTS"
    if change_points["state"] == "READY":
        change_points["label_comparison"] = _change_point_label_metrics(
            rows, change_points["break_indices"], event_labels, is_cancelled=is_cancelled,
        )
    elif event_labels:
        change_points["label_comparison"] = {
            "state": "NOT_AVAILABLE",
            "reason": change_points.get("reason"),
            "true_positive": None,
            "false_positive": None,
            "false_negative": None,
            "true_negative": None,
            "precision": None,
            "recall": None,
            "false_alarm_rate": None,
        }
    else:
        change_points["label_comparison"] = _change_point_label_metrics(rows, [], event_labels, is_cancelled=is_cancelled)

    backtest = {"state": "NOT_REQUESTED", "holdout_periods": [], "metrics": None, "monitoring": None}
    future = {"state": "NOT_REQUESTED", "periods": [], "predictions": []}
    if holdout_periods is not None:
        if holdout_periods <= 0 or holdout_periods >= n:
            backtest = {"state": "INSUFFICIENT", "reason": "HOLDOUT_WINDOW_LEAVES_NO_TRAINING_DATA"}
        else:
            split = n - holdout_periods
            if seasonal_period is not None and seasonal_period >= 2 and split < 2 * seasonal_period:
                backtest = {"state": "INSUFFICIENT", "reason": "TRAINING_PREFIX_HAS_FEWER_THAN_TWO_SEASONAL_CYCLES"}
            elif split < 3:
                backtest = {"state": "INSUFFICIENT", "reason": "TRAINING_PREFIX_TOO_SHORT"}
            else:
                try:
                    predicted, intervals, train_model = _model_prediction(
                        numeric_values[:split], seasonal_period=seasonal_period if can_stl and split >= 2 * seasonal_period else None,
                        steps=holdout_periods, method=forecast_method, start=split, is_cancelled=is_cancelled,
                    )
                    actual = numeric_values[split:]
                    model_metrics = _metrics(actual, predicted.tolist())
                    naive = [numeric_values[split - 1]] * holdout_periods
                    naive_metrics = _metrics(actual, naive)
                    seasonal_naive = None
                    seasonal_naive_metrics = None
                    if seasonal_period is not None and seasonal_period >= 2 and split >= seasonal_period:
                        seasonal_naive = [
                            numeric_values[split - seasonal_period + (offset % seasonal_period)]
                            for offset in range(holdout_periods)
                        ]
                        seasonal_naive_metrics = _metrics(actual, seasonal_naive)
                    interval_coverage = None if intervals is None else float(np.mean(
                        (np.asarray(actual) >= intervals[:, 0]) & (np.asarray(actual) <= intervals[:, 1])
                    ))
                    if seasonal_period is not None and can_stl and split >= 2 * seasonal_period:
                        baseline_residual = STL(
                            np.asarray(numeric_values[:split], dtype=float),
                            period=seasonal_period, robust=True,
                        ).fit().resid
                    else:
                        train_result = getattr(train_model, "model_result", train_model)
                        baseline_residual = np.asarray(getattr(train_result, "resid", []), dtype=float)
                    scale = float(np.std(baseline_residual, ddof=1)) if split > 1 else 0.0
                    monitor_points = []
                    monitor_state = "INSUFFICIENT"
                    monitor_reason = "BASELINE_RESIDUAL_VARIANCE_ZERO"
                    if scale > 1e-10 * max(1.0, abs(float(np.mean(numeric_values[:split])))):
                        ewma, positive, negative = 0.0, 0.0, 0.0
                        lam = 0.2
                        for offset, error in enumerate(np.asarray(actual) - predicted):
                            z = float(error / scale)
                            ewma = lam * z + (1 - lam) * ewma
                            positive = max(0.0, positive + z - 0.5)
                            negative = min(0.0, negative + z + 0.5)
                            limit = 3 * math.sqrt(lam / (2 - lam) * (1 - (1 - lam) ** (2 * (offset + 1))))
                            monitor_points.append({
                                "observation_id": rows[split + offset]["id"],
                                "standardized_error": z,
                                "ewma": ewma,
                                "ewma_alarm": abs(ewma) > limit,
                                "cusum_positive": positive,
                                "cusum_negative": negative,
                                "cusum_alarm": positive > 5 or negative < -5,
                                "combined_alarm": abs(ewma) > limit or positive > 5 or negative < -5,
                            })
                        monitor_state = "READY"
                        monitor_reason = None
                    label_state = _label_metrics(monitor_points, event_labels)
                    backtest = {
                        "state": "READY",
                        "method": (
                            "STL+ARIMA(1,1,0)" if forecast_method == "arima" and can_stl and split >= 2 * seasonal_period
                            else "ARIMA(1,1,0)" if forecast_method == "arima"
                            else "ETS(additive,damped-trend,additive-seasonal)" if seasonal_period and split >= 2 * seasonal_period
                            else "ETS(additive,damped-trend)"
                        ),
                        "training_n": split,
                        "holdout_n": holdout_periods,
                        "metrics": model_metrics,
                        "naive_last_value_metrics": naive_metrics,
                        "better_mae_than_naive": model_metrics["mae"] < naive_metrics["mae"],
                        "seasonal_naive_metrics": seasonal_naive_metrics,
                        "better_mae_than_seasonal_naive": (
                            None if seasonal_naive_metrics is None
                            else model_metrics["mae"] < seasonal_naive_metrics["mae"]
                        ),
                        "baseline_definition": {
                            "last_value": "repeat the last training observation",
                            "seasonal_naive": (
                                f"repeat the last {seasonal_period} training observations"
                                if seasonal_naive is not None else "not available without a declared trained seasonal cycle"
                            ),
                        },
                        "nominal_interval_coverage": 0.95 if intervals is not None else None,
                        "observed_interval_coverage": interval_coverage,
                        "prediction_interval_state": "MODEL_INTERVAL_AVAILABLE" if intervals is not None else "NOT_ESTIMATED_BY_HOLT_WINTERS_METHOD",
                        "holdout": [
                            {"observation_id": rows[split + offset]["id"],
                             "period": rows[split + offset]["period"],
                             "actual": actual[offset], "prediction": float(predicted[offset]),
                             "lower_95": float(intervals[offset, 0]) if intervals is not None else None,
                             "upper_95": float(intervals[offset, 1]) if intervals is not None else None,
                             "last_value_baseline": naive[offset],
                             "seasonal_naive_baseline": seasonal_naive[offset] if seasonal_naive is not None else None,
                             "error": float(predicted[offset] - actual[offset])}
                            for offset in range(holdout_periods)
                        ],
                        "monitoring": {
                            "state": monitor_state, "reason": monitor_reason,
                            "points": monitor_points,
                            "parameters": {"ewma_lambda": 0.2, "ewma_sigma_limit": 3, "cusum_k": 0.5, "cusum_h": 5},
                            "label_evaluation": label_state,
                            "threshold_calibration": "standard chart values; not empirically calibrated without independent labels",
                        },
                    }
                except (ValueError, np.linalg.LinAlgError) as exc:
                    backtest = {"state": "INSUFFICIENT", "reason": str(exc)}
    if forecast_horizon is not None:
        if holdout_periods is None:
            future = {"state": "INSUFFICIENT", "reason": "OUT_OF_TIME_HOLDOUT_REQUIRED_BEFORE_FORECAST"}
        elif backtest.get("state") != "READY":
            future = {"state": "INSUFFICIENT", "reason": "OUT_OF_TIME_BACKTEST_NOT_AVAILABLE"}
        else:
            try:
                predictions, intervals, _ = _model_prediction(
                    numeric_values, seasonal_period=seasonal_period if can_stl else None,
                    steps=forecast_horizon, method=forecast_method, is_cancelled=is_cancelled,
                )
                periods = _future_periods(rows[-1], forecast_horizon)
                future = {
                    "state": "CONDITIONAL_FORECAST",
                    "method": (
                        "STL+ARIMA(1,1,0)" if forecast_method == "arima" and can_stl
                        else "ARIMA(1,1,0)" if forecast_method == "arima"
                        else "ETS(additive,damped-trend,additive-seasonal)" if can_stl
                        else "ETS(additive,damped-trend)"
                    ),
                    "prediction_interval_state": "MODEL_INTERVAL_AVAILABLE" if intervals is not None else "NOT_ESTIMATED_BY_HOLT_WINTERS_METHOD",
                    "periods": [
                        {"period": period, "value": float(predictions[index]),
                         "lower_95": float(intervals[index, 0]) if intervals is not None else None,
                         "upper_95": float(intervals[index, 1]) if intervals is not None else None}
                        for index, period in enumerate(periods)
                    ],
                    "predictions": predictions.tolist(),
                    "intervals_95": intervals.tolist() if intervals is not None else None,
                    "causal_effect": None,
                }
            except (ValueError, OverflowError) as exc:
                future = {"state": "INSUFFICIENT", "reason": str(exc)}
    group_data.update({
        "state": "AVAILABLE", "method_state": "AVAILABLE", "reason": None,
        "n": n, "trend": {"slope_per_period": float(slope), "first_value": numeric_values[0],
                            "last_value": numeric_values[-1], "first_to_last_delta": numeric_values[-1] - numeric_values[0]},
        "decomposition": decomposition,
        "change_points": change_points,
        "backtest": backtest,
        "forecast": future,
        "versions": {"statsmodels": statsmodels.__version__, "ruptures": ruptures.__version__},
        "observations": [
            {"id": row["id"], "entity": row["entity"], "metric": row["metric"],
             "period": row["period"], "value": _public_time_observation(row)["value"],
             "source": row["source"], "coverage": row["coverage"]}
            for row in rows
        ],
    })
    return group_data


def _public_time_observation(row: dict) -> dict:
    metric = row["metric"]
    return {
        "id": row["id"], "entity": row["entity"],
        "metric_key": metric["key"], "definition": metric["definition"],
        "value": _json_number_for_time(metric["value"]), "unit": metric["unit"],
        "currency": metric["currency"], "basis": metric["basis"],
        "period": row["period"], "coverage": row["coverage"],
        "review_status": row["review_status"], "source": row["source"],
    }


def _source_row_label(source: Any, ordinal: int) -> str:
    if isinstance(source, dict):
        row = source.get("row")
        sheet = source.get("sheet")
        if type(row) is int:
            sheet_label = f"{sheet} · " if isinstance(sheet, str) and sheet else ""
            return f"原件 {sheet_label}第{row}行"
    return f"观察记录 {ordinal}"


def _json_number_for_time(value):
    if value is None:
        return None
    if not value.is_finite():
        raise ValueError("time-series values must be finite")
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("time-series value exceeds the JSON numeric range")
    return number


def _label_metrics(points: list[dict], labels: dict[str, dict]) -> dict:
    if not labels:
        return {"state": "NOT_PROVIDED", "true_positive": None, "false_positive": None, "false_negative": None,
                "precision": None, "recall": None, "false_alarm_rate": None}
    alarms = {row["observation_id"]: row["combined_alarm"] for row in points}
    known = {
        identity: row["state"] for identity, row in labels.items()
        if identity in alarms and row["state"] in {"event", "no_event"}
    }
    if not known:
        return {"state": "NO_KNOWN_LABELS", "true_positive": None, "false_positive": None, "false_negative": None,
                "precision": None, "recall": None, "false_alarm_rate": None}
    true_positive = sum(identity in alarms and alarms[identity] and state == "event" for identity, state in known.items())
    false_positive = sum(identity in alarms and alarms[identity] and state == "no_event" for identity, state in known.items())
    false_negative = sum(identity in alarms and not alarms[identity] and state == "event" for identity, state in known.items())
    true_negative = sum(identity in alarms and not alarms[identity] and state == "no_event" for identity, state in known.items())
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    negative_denominator = false_positive + true_negative
    return {
        "state": "LABELED_HOLDOUT_ONLY",
        "labeled_holdout_count": len(known),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": true_positive / precision_denominator if precision_denominator else None,
        "recall": true_positive / recall_denominator if recall_denominator else None,
        "false_alarm_rate": false_positive / negative_denominator if negative_denominator else None,
    }


def _change_point_label_metrics(rows: list[dict], change_indices: list[int], labels: dict[str, dict], *, is_cancelled=None) -> dict:
    """Compare retrospective PELT boundaries with caller labels on the same series."""
    if not labels:
        return {"state": "NOT_PROVIDED", "true_positive": None, "false_positive": None,
                "false_negative": None, "true_negative": None, "precision": None,
                "recall": None, "false_alarm_rate": None}
    identities = {row["id"]: index for index, row in enumerate(rows)}
    known = {}
    for identity, value in labels.items():
        _check_cancelled(is_cancelled)
        if identity in identities and value.get("state") in {"event", "no_event"}:
            known[identities[identity]] = value["state"]
    if not known:
        return {"state": "NO_KNOWN_LABELS", "labeled_observation_count": 0,
                "true_positive": None, "false_positive": None, "false_negative": None,
                "true_negative": None, "precision": None, "recall": None, "false_alarm_rate": None}
    detected = set(change_indices)
    true_positive = sum(index in detected and state == "event" for index, state in known.items())
    false_negative = sum(index not in detected and state == "event" for index, state in known.items())
    false_positive = sum(index in detected and state == "no_event" for index, state in known.items())
    true_negative = sum(index not in detected and state == "no_event" for index, state in known.items())
    precision_denominator = true_positive + false_positive
    event_denominator = true_positive + false_negative
    no_event_denominator = false_positive + true_negative
    return {
        "state": "RETROSPECTIVE_LABEL_COMPARISON_ONLY",
        "evaluation_basis": "same-series exact-period overlap; not independent or prospective",
        "matching_rule": "a detected boundary matches a label only at the boundary observation index",
        "labeled_observation_count": len(known),
        "labeled_event_count": event_denominator,
        "labeled_no_event_count": no_event_denominator,
        "unlabeled_detected_break_indices": sorted(detected - set(known)),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": true_positive / precision_denominator if precision_denominator else None,
        "recall": true_positive / event_denominator if event_denominator else None,
        "false_alarm_rate": false_positive / no_event_denominator if no_event_denominator else None,
    }


def evaluate_time_series(inputs: dict, *, is_cancelled=None) -> dict:
    allowed = {"title", "question", "observations", "seasonal_cycle_periods", "forecast_horizon", "holdout_periods", "event_labels", "forecast_method"}
    if set(inputs) - allowed:
        raise ValueError("operating_time_series contains unsupported fields")
    observations = inputs.get("observations")
    if not isinstance(observations, list):
        raise ValueError("observations must be an array")
    normalized = []
    for index, row in enumerate(observations):
        _check_cancelled(is_cancelled)
        normalized.append(_operating_observation(row, index))
    if len({row["id"] for row in normalized}) != len(normalized):
        raise ValueError("observation IDs must be unique")
    season = inputs.get("seasonal_cycle_periods")
    if season is not None and (type(season) is not int or season < 2):
        raise ValueError("seasonal_cycle_periods must be a business cycle of at least two observations")
    horizon = inputs.get("forecast_horizon")
    if horizon is not None and (type(horizon) is not int or horizon <= 0):
        raise ValueError("forecast_horizon must be a positive number of business periods")
    holdout = inputs.get("holdout_periods", horizon)
    if holdout is not None and (type(holdout) is not int or holdout <= 0):
        raise ValueError("holdout_periods must be a positive number of historical evaluation periods")
    forecast_method = inputs.get("forecast_method", "arima")
    if forecast_method not in {"arima", "ets"}:
        raise ValueError("forecast_method must be arima or ets")
    labels = inputs.get("event_labels", [])
    if not isinstance(labels, list):
        raise ValueError("event_labels must be an array")
    by_id = {row["id"]: row for row in normalized}
    label_by_id = {}
    for index, raw in enumerate(labels):
        row = raw if isinstance(raw, dict) else None
        if row is None:
            raise ValueError(f"event_labels[{index}] must be an object")
        identity = row.get("observation_id")
        if not isinstance(identity, str) or identity not in by_id or identity in label_by_id:
            raise ValueError("event labels must reference unique supplied observation IDs")
        if row.get("state") not in {"event", "no_event", "unknown"}:
            raise ValueError("event label state must be event, no_event, or unknown")
        label_by_id[identity] = {"state": row["state"], "source_ref": row.get("source_ref")}
    if not normalized:
        groups = []
    else:
        grouped: dict[str, list[dict]] = {}
        for row in normalized:
            metric = row["metric"]
            key = json.dumps({
                "entity": row["entity"]["id"], "metric": metric["key"],
                "definition": metric["definition"], "unit": metric["unit"],
                "currency": metric["currency"], "basis": metric["basis"],
                "denominator": metric["denominator"],
                "comparison_dimensions": metric["comparison_dimensions"],
                "scope": row["scope"], "frequency": row["period"]["frequency"],
                "timezone": row["period"]["timezone"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            grouped.setdefault(key, []).append(row)
        groups = []
        for values in grouped.values():
            _check_cancelled(is_cancelled)
            values.sort(key=lambda row: (row["_period_start"], row["_period_end"], row["id"]))
            groups.append(_evaluate_series(values, seasonal_period=season, forecast_horizon=horizon,
                                           holdout_periods=holdout, event_labels=label_by_id,
                                           forecast_method=forecast_method,
                                           is_cancelled=is_cancelled))
    flat_observations = [
        [row["entity"]["name"], row["metric"]["key"], row["metric"]["definition"],
         f"{row['period']['start']}–{row['period']['end']} ({row['period']['timezone'] or '时区未知'})",
         _json_number_to_text(_json_number_for_time(row["metric"]["value"])), row["metric"]["unit"],
         row["metric"]["currency"], row["metric"]["basis"], _source_label(row["source"])]
        for row in normalized
    ]
    decomposition_rows, change_rows, change_label_rows, holdout_rows, forecast_rows, summary_rows, backtest_summary_rows = [], [], [], [], [], [], []
    for group in groups:
        source_by_id = {
            row["id"]: row.get("source") for row in group.get("observations", [])
        }
        ordinal_by_id = {
            row["id"]: index + 1 for index, row in enumerate(group.get("observations", []))
        }
        summary_rows.append([
            group["entity"]["name"], group["metric"]["key"], group.get("n", len(group.get("observations", []))), group["period_frequency"],
            group["timezone"], group.get("trend", {}).get("slope_per_period"),
            group.get("trend", {}).get("first_to_last_delta"), group["method_state"], group["reason"],
        ])
        points = group.get("decomposition")
        if points and points.get("state") == "READY":
            for index, row in enumerate(group["observations"]):
                decomposition_rows.append([
                    group["entity"]["name"], _source_row_label(row.get("source"), index + 1), row["period"]["start"], row["value"],
                    _json_number_to_text(points["trend"][index]), _json_number_to_text(points["seasonal"][index]),
                    _json_number_to_text(points["residual"][index]),
                ])
        for index in group.get("change_points", {}).get("break_indices", []):
            observations_for_group = group["observations"]
            change_rows.append([
                group["entity"]["name"], group["metric"]["key"],
                _source_row_label(
                    observations_for_group[index].get("source") if index < len(observations_for_group) else None,
                    index + 1,
                ),
                observations_for_group[index]["period"]["start"] if index < len(observations_for_group) else "系列结束",
                group["change_points"]["method"], group["change_points"]["penalty"],
                "回顾性变点，不代表因果事件",
            ])
        label_comparison = group.get("change_points", {}).get("label_comparison", {})
        if label_comparison.get("state") not in {None, "NOT_PROVIDED", "NO_KNOWN_LABELS"}:
            known_no_events = label_comparison.get("labeled_no_event_count")
            change_label_rows.append([
                group["entity"]["name"], group["metric"]["key"], label_comparison.get("state"),
                label_comparison.get("labeled_observation_count"), label_comparison.get("true_positive"),
                label_comparison.get("false_positive"), label_comparison.get("false_negative"),
                known_no_events, label_comparison.get("precision"), label_comparison.get("recall"),
                label_comparison.get("false_alarm_rate"),
            ])
        if group.get("backtest", {}).get("state") == "READY":
            backtest = group["backtest"]
            seasonal_metrics = backtest.get("seasonal_naive_metrics") or {}
            backtest_summary_rows.append([
                group["entity"]["name"], group["metric"]["key"], backtest.get("method"),
                backtest.get("metrics", {}).get("mae"), backtest.get("metrics", {}).get("rmse"),
                backtest.get("naive_last_value_metrics", {}).get("mae"), seasonal_metrics.get("mae"),
                backtest.get("better_mae_than_naive"), backtest.get("better_mae_than_seasonal_naive"),
                backtest.get("observed_interval_coverage"), backtest.get("prediction_interval_state"),
            ])
            for row in group["backtest"]["holdout"]:
                holdout_rows.append([
                    group["entity"]["name"],
                    _source_row_label(
                        source_by_id.get(row["observation_id"]),
                        ordinal_by_id.get(row["observation_id"], 1),
                    ),
                    row["period"]["start"],
                    row["actual"], row["prediction"], row["lower_95"], row["upper_95"],
                    row.get("last_value_baseline"), row.get("seasonal_naive_baseline"), row["error"],
                ])
        if group.get("forecast", {}).get("state") == "CONDITIONAL_FORECAST":
            for row in group["forecast"]["periods"]:
                forecast_rows.append([
                    group["entity"]["name"], group["metric"]["key"], row["period"]["start"],
                    row["value"], row["lower_95"], row["upper_95"], group["forecast"]["method"],
                ])
    sections = [
        _section(
            "time_series.observations", "按期经营观察",
            "保留每条原值与时区；缺值没有补零，估算/预测数据不会混作实绩。\n\n" +
            _md_table(["对象", "指标", "定义", "期间/时区", "实绩值", "单位", "币种", "状态", "来源"], flat_observations),
            {"observations": [_public_time_observation(row) for row in normalized]},
        ),
        _section(
            "time_series.trend_decomposition", "趋势与季节分解",
            "趋势斜率只描述本序列变化；季节分解只在明确周期且至少覆盖两个完整周期时运行。\n\n" +
            _md_table(["对象", "指标", "观察数", "频率", "时区", "每期趋势斜率", "首末差", "状态", "未运行原因"], summary_rows) +
            "\n\n分解值：\n\n" +
            _md_table(["对象", "源记录定位", "期间起", "实绩", "趋势", "季节项", "残差"], decomposition_rows),
            {"series": groups},
        ),
        _section(
            "time_series.change_points", "变点与监测",
            "变点检测按序列整体回顾计算；EWMA/CUSUM告警只在预留的时间外窗口中计算。无独立事件标注时，误报率和漏报率保持未知。\n\n" +
            _md_table(["对象", "指标", "源记录定位", "变点期间", "方法", "惩罚值", "解释"], change_rows) +
            "\n\n调用方标注对照（同一序列回顾性比对，不是独立留出）：\n\n" +
            _md_table(["对象", "指标", "状态", "已标观察", "命中事件", "标注无事件处检出", "漏检事件", "标注无事件数", "精确率", "召回率", "标注无事件误报率"], change_label_rows),
            {"series": [
                {"entity": row["entity"], "metric": row["metric"], "change_points": row.get("change_points"),
                 "monitoring": row.get("backtest", {}).get("monitoring")}
                for row in groups
            ]},
        ),
        _section(
            "time_series.backtest", "时间外回测",
            "按业务指定的历史期间留出训练；模型误差和名义区间覆盖仅说明本次时间外样本，不保证未来覆盖。ETS点预测不伪造区间。\n\n" +
            _md_table(["对象", "源记录定位", "留出期间", "实际值", "预测值", "95%预测下界", "95%预测上界", "末训练值基线", "季节朴素基线", "预测误差"], holdout_rows) +
            "\n\n模型与朴素基线比较：\n\n" + _md_table(
                ["对象", "指标", "模型", "模型MAE", "模型RMSE", "末训练值MAE", "季节朴素MAE", "胜过末训练值", "胜过季节朴素", "实测区间覆盖", "区间状态"],
                backtest_summary_rows,
            ),
            {"series": [{"entity": row["entity"], "metric": row["metric"], "backtest": row.get("backtest")} for row in groups]},
        ),
        _section(
            "time_series.forecast", "条件预测情景",
            "只在可用的时间外回测后生成；ARIMA区间条件于所选误差模型，Holt-Winters ETS仅提供点预测并保留区间未知；两者都不证明经营措施的因果效果。\n\n" +
            _md_table(["对象", "指标", "未来期间", "预测值", "95%下界", "95%上界", "方法"], forecast_rows),
            {"series": [{"entity": row["entity"], "metric": row["metric"], "forecast": row.get("forecast")} for row in groups]},
        ),
    ]
    question = _string(inputs.get("question"), "question", optional=True)
    if question:
        sections.insert(0, _section("time_series.question", "分析问题", question, {"question": question}))
    limitations = [
        "要求单一口径的真实连续观察；期间缺口、未知值、估计值/实绩混合、时区缺失或来源待核会阻止模型运行。",
        "季节分解需要明确的业务周期和至少两个完整周期；没有适用条件时只返回已成立的描述性趋势与缺口。",
        "ARIMA(1,1,0)预测区间依赖所选误差模型；Holt-Winters ETS未在此适配中提供预测区间。回测覆盖度不保证将来覆盖。",
        "PELT是回顾性变点检测，不作原因归因；EWMA/CUSUM默认阈值不是本业务样本校准结果。",
        "误报/漏报指标只使用调用方提供的独立事件标签；没有标签时保持未知。",
        "本操作不计算MMM、因果ROI或市场份额；这些由独立业务操作处理。",
    ]
    return _answer(_requested_title(inputs, "经营趋势、变点与预测"), sections, limitations)
