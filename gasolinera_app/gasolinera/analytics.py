"""
Analytics utilities for understanding consumption patterns.

The core feature is peak-hour prediction which aggregates sales logs to
identify the busiest periods per day and per week.  The output can be reused by
the scheduling and notification modules to prepare staffing and stock levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class PeakHourSummary:
    """Aggregated statistics for a given station."""

    top_hours: pd.Series
    by_weekday: pd.Series
    heatmap: pd.DataFrame


class PeakHourAnalyzer:
    """
    Analyse sales logs to extract peak service hours.

    Expected DataFrame columns:
        - ``timestamp``: datetime of the transaction.
        - ``volume``: litres sold in the transaction.
        - ``station_id`` (optional): allows per-station breakdown.
    """

    def __init__(self, timestamp_column: str = "timestamp", volume_column: str = "volume", station_column: str = "station_id"):
        self.timestamp_column = timestamp_column
        self.volume_column = volume_column
        self.station_column = station_column
        self._summary_by_station: Dict[str, PeakHourSummary] = {}

    def fit(self, sales_df: pd.DataFrame) -> None:
        df = sales_df.copy()
        df[self.timestamp_column] = pd.to_datetime(df[self.timestamp_column], errors="coerce")
        df = df.dropna(subset=[self.timestamp_column])
        df[self.volume_column] = pd.to_numeric(df[self.volume_column], errors="coerce").fillna(0.0)
        if self.station_column in df.columns:
            grouped = df.groupby(df[self.station_column].astype(str))
        else:
            grouped = [( "all", df)]

        self._summary_by_station = {}
        for station_id, group in grouped:
            summary = self._summarise_station(group)
            self._summary_by_station[str(station_id)] = summary

    def _summarise_station(self, group: pd.DataFrame) -> PeakHourSummary:
        group = group.set_index(self.timestamp_column)
        group["hour"] = group.index.hour
        group["weekday"] = group.index.day_name()

        top_hours = group.groupby("hour")[self.volume_column].sum().sort_values(ascending=False)
        weekday_profile = group.groupby("weekday")[self.volume_column].sum()
        heatmap = (
            group.pivot_table(index="weekday", columns="hour", values=self.volume_column, aggfunc="sum")
            .reindex(index=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
            .fillna(0.0)
        )
        return PeakHourSummary(top_hours=top_hours, by_weekday=weekday_profile, heatmap=heatmap)

    def get_station_summary(self, station_id: str | int = "all") -> Optional[PeakHourSummary]:
        return self._summary_by_station.get(str(station_id))

    def detect_spikes(self, sales_df: pd.DataFrame, *, zscore_threshold: float = 2.5) -> pd.DataFrame:
        """
        Flag transactions whose z-score exceeds the given threshold.

        Useful for raising anomaly notifications such as suspicious demand
        surges that could indicate a leak or large fleet order.
        """

        df = sales_df.copy()
        df[self.timestamp_column] = pd.to_datetime(df[self.timestamp_column], errors="coerce")
        df = df.dropna(subset=[self.timestamp_column])
        df[self.volume_column] = pd.to_numeric(df[self.volume_column], errors="coerce").fillna(0.0)

        grouped = df.groupby(df[self.timestamp_column].dt.floor("H"))[self.volume_column].sum()
        mean = grouped.mean()
        std = grouped.std(ddof=0)
        if std == 0:
            df["is_spike"] = False
            return df

        zscores = (grouped - mean) / std
        spike_hours = zscores[zscores > zscore_threshold].index
        df["is_spike"] = df[self.timestamp_column].dt.floor("H").isin(spike_hours)
        return df


def plot_peak_heatmap(summary: PeakHourSummary, *, figsize=(10, 4)) -> None:
    """Render a weekday/hour heatmap using seaborn if available."""

    import matplotlib.pyplot as plt

    try:
        import seaborn as sns
    except ImportError:  # pragma: no cover - fallback path
        plt.imshow(summary.heatmap.values, aspect="auto")
        plt.xticks(range(summary.heatmap.shape[1]), summary.heatmap.columns)
        plt.yticks(range(summary.heatmap.shape[0]), summary.heatmap.index)
        plt.title("Peak hour heatmap")
        plt.colorbar(label="Volume")
        plt.tight_layout()
        plt.show()
        return

    plt.figure(figsize=figsize)
    sns.heatmap(summary.heatmap, cmap="viridis")
    plt.title("Peak hour heatmap")
    plt.xlabel("Hour of day")
    plt.ylabel("Weekday")
    plt.tight_layout()
    plt.show()


def sample_sales_log(periods: int = 500, station_id: str = "CDMX-01") -> pd.DataFrame:
    """
    Generate a synthetic sales log for demos or manual testing.

    The output conforms to the expected schema for :class:`PeakHourAnalyzer`.
    """

    timestamps = pd.date_range("2024-01-01", periods=periods, freq="H")
    rng = pd.Series(range(periods))
    # Build a demand curve that peaks during morning and evening rush hours.
    hour = timestamps.hour
    peak_morning = (hour >= 6) & (hour <= 9)
    peak_evening = (hour >= 17) & (hour <= 20)
    base = 200 + 50 * (peak_morning | peak_evening)
    weekend_boost = np.where(timestamps.weekday >= 5, 30, 0)
    volume = base + weekend_boost + (rng % 20)

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "volume": volume,
            "station_id": station_id,
            "transaction_id": range(periods),
        }
    )
