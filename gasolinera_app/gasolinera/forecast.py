"""
Forecasting tools for monthly gasoline demand.

The module gives the project a single abstraction (`MonthlyUsageForecaster`)
that can run on top of Prophet or an ARIMA model from `statsmodels`.  Prophet
is preferred for its excellent handling of multiple seasonality components, but
it is an optional dependency.  When Prophet is not installed the code falls
back to ARIMA automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from .data import UsageSchema, validate_usage_dataframe

logger = logging.getLogger(__name__)


ForecastModel = Literal["prophet", "arima"]


def prepare_monthly_series(
    df: pd.DataFrame,
    gasoline_type: str,
    *,
    schema: UsageSchema | None = None,
    min_periods: int = 3,
) -> pd.DataFrame:
    """
    Transform the raw usage dataframe into a Prophet-compatible monthly series.

    Parameters
    ----------
    df:
        Validated usage dataframe (see :func:`validate_usage_dataframe`).
    gasoline_type:
        Case-insensitive fuel category to extract.
    schema:
        Optional schema descriptor when working with custom column names.
    min_periods:
        Minimum number of monthly observations required to proceed.  A
        ``ValueError`` is raised otherwise.
    """

    schema = schema or UsageSchema()
    validated = validate_usage_dataframe(df, schema)
    mask = validated[schema.type_column].str.lower() == gasoline_type.lower()
    subset = validated.loc[mask, [schema.date_column, schema.volume_column]].copy()
    if subset.empty:
        raise ValueError(f"No data found for gasoline type '{gasoline_type}'.")

    subset = (
        subset.set_index(schema.date_column)
        .resample("MS")[schema.volume_column]
        .sum()
        .reset_index()
        .rename(columns={schema.date_column: "ds", schema.volume_column: "y"})
    )

    if len(subset) < min_periods:
        raise ValueError(
            f"Insufficient data for {gasoline_type!r}. Expected at least {min_periods} months, got {len(subset)}."
        )
    return subset


@dataclass
class ForecastResult:
    """Container for forecast output."""

    fitted_values: pd.DataFrame
    forecast: pd.DataFrame
    model_type: ForecastModel


class MonthlyUsageForecaster:
    """
    Facade over Prophet/ARIMA forecasting models.

    Usage
    -----
    >>> forecaster = MonthlyUsageForecaster(model="prophet")
    >>> forecaster.fit(df, gasoline_type="Regular")
    >>> forecast_df = forecaster.predict(periods=6)
    """

    def __init__(self, model: ForecastModel = "prophet", *, schema: UsageSchema | None = None, **model_kwargs):
        self.model_type: ForecastModel = model
        self.schema = schema or UsageSchema()
        self.model_kwargs = model_kwargs
        self._model = None
        self._history: Optional[pd.DataFrame] = None
        self._fitted: Optional[pd.DataFrame] = None

    @property
    def is_fit(self) -> bool:
        return self._model is not None and self._history is not None

    def fit(self, df: pd.DataFrame, gasoline_type: str) -> ForecastResult:
        """Fit the underlying model and return in-sample metrics."""

        history = prepare_monthly_series(df, gasoline_type, schema=self.schema)
        self._history = history

        if self.model_type == "prophet":
            self._model = self._fit_prophet(history)
            fitted = self._model.predict(history)
            forecast_df = fitted[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
            return ForecastResult(fitted_values=fitted, forecast=forecast_df, model_type="prophet")

        self._model = self._fit_arima(history)
        fitted = self._model.get_prediction().summary_frame()
        fitted = fitted.rename(columns={"mean": "yhat", "mean_ci_lower": "yhat_lower", "mean_ci_upper": "yhat_upper"})
        fitted = fitted.reset_index().rename(columns={"index": "ds"})
        return ForecastResult(fitted_values=fitted, forecast=fitted, model_type="arima")

    def predict(self, periods: int = 6, freq: str = "MS") -> pd.DataFrame:
        """Generate future predictions."""

        if not self.is_fit:
            raise RuntimeError("Model must be fit before calling predict().")

        if self.model_type == "prophet":
            future = self._model.make_future_dataframe(periods=periods, freq=freq)
            forecast = self._model.predict(future)
            return forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].tail(periods)

        forecast_res = self._model.get_forecast(steps=periods)
        frame = forecast_res.summary_frame()
        frame = frame.rename(columns={"mean": "yhat", "mean_ci_lower": "yhat_lower", "mean_ci_upper": "yhat_upper"})
        frame = frame.reset_index().rename(columns={"index": "ds"})
        return frame

    def _fit_prophet(self, history: pd.DataFrame):
        try:
            from prophet import Prophet
        except ImportError as exc:  # pragma: no cover - fallback path
            logger.warning("Prophet not available, falling back to ARIMA: %s", exc)
            self.model_type = "arima"
            return self._fit_arima(history)

        model = Prophet(**self.model_kwargs)
        model.fit(history)
        return model

    def _fit_arima(self, history: pd.DataFrame):
        from statsmodels.tsa.arima.model import ARIMA

        # Use simple heuristics: difference once for stability, let statsmodels
        # pick the rest via AIC minimisation.
        order = self.model_kwargs.get("order", (1, 1, 1))
        y_series = history.set_index("ds")["y"].asfreq("MS")
        arima = ARIMA(y_series, order=order, freq="MS", **{k: v for k, v in self.model_kwargs.items() if k != "order"})
        return arima.fit()


def forecast_monthly_usage(
    df: pd.DataFrame,
    gasoline_type: str,
    *,
    model: ForecastModel = "prophet",
    periods: int = 6,
    freq: str = "MS",
    schema: UsageSchema | None = None,
    **model_kwargs,
) -> pd.DataFrame:
    """
    Convenience wrapper around :class:`MonthlyUsageForecaster`.

    Returns only the future forecast slice to simplify use in dashboards or API
    controllers.
    """

    forecaster = MonthlyUsageForecaster(model=model, schema=schema, **model_kwargs)
    forecaster.fit(df, gasoline_type)
    return forecaster.predict(periods=periods, freq=freq)


def plot_forecast(history: pd.DataFrame, forecast: pd.DataFrame, *, title: str = "Monthly usage forecast") -> None:
    """
    Quick visualisation helper that plots the historical series and forecast.

    The function uses matplotlib directly to keep dependencies light and makes
    no assumptions about the environment (Jupyter, CLI or Streamlit).
    """

    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(history["ds"], history["y"], label="Historical")
    ax.plot(forecast["ds"], forecast["yhat"], label="Forecast")
    ax.fill_between(forecast["ds"], forecast["yhat_lower"], forecast["yhat_upper"], color="C1", alpha=0.3)
    ax.set_title(title)
    ax.set_ylabel("Volume (litres)")
    ax.set_xlabel("Month")
    ax.legend()
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()
