"""
Data handling utilities for the gasoline analytics application.

The functions in this module keep pandas-specific logic contained in a single
place so that other modules can focus on domain behaviour (forecasting,
reporting, etc.).  In production the CSV loader would be replaced with a
database adapter or cloud storage connector – the design keeps those concerns
isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

import pandas as pd


USAGE_COLUMNS: Sequence[str] = ("date", "gasoline_type", "volume", "station_id")


@dataclass(frozen=True)
class UsageSchema:
    """Simple schema descriptor for usage data frames."""

    date_column: str = "date"
    type_column: str = "gasoline_type"
    volume_column: str = "volume"
    station_column: str = "station_id"

    @property
    def required_columns(self) -> List[str]:
        return [self.date_column, self.type_column, self.volume_column, self.station_column]


def validate_usage_dataframe(df: pd.DataFrame, schema: UsageSchema | None = None) -> pd.DataFrame:
    """
    Validate and normalise a usage dataframe.

    Parameters
    ----------
    df:
        The dataframe to validate.
    schema:
        Optional schema descriptor.  The default schema expects columns defined
        in :data:`USAGE_COLUMNS`.

    Returns
    -------
    pandas.DataFrame
        A copy of the validated dataframe with the date column coerced to
        pandas datetime.

    Raises
    ------
    ValueError
        If required columns are missing.
    """

    schema = schema or UsageSchema()
    missing = [col for col in schema.required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Usage dataframe missing required columns: {missing}")

    validated = df.copy()
    validated[schema.date_column] = pd.to_datetime(validated[schema.date_column], errors="raise")
    validated[schema.volume_column] = pd.to_numeric(validated[schema.volume_column], errors="coerce").fillna(0.0)
    validated[schema.station_column] = validated[schema.station_column].astype(str)
    validated[schema.type_column] = validated[schema.type_column].str.title()
    return validated


def load_usage_csv(
    path: str | Path,
    *,
    schema: UsageSchema | None = None,
    parse_dates: bool = True,
    encoding: str = "utf-8",
    **read_csv_kwargs,
) -> pd.DataFrame:
    """
    Load historical usage data from a CSV file.

    The loader normalises column names to lowercase with underscores so that
    incoming spreadsheets with headers such as ``Gasoline Type`` still work.
    Additional pandas ``read_csv`` keyword arguments are forwarded.
    """

    file_path = Path(path)
    df = pd.read_csv(file_path, encoding=encoding, **read_csv_kwargs)
    df.columns = [col.strip().lower().replace(" ", "_") for col in df.columns]
    if parse_dates:
        schema = schema or UsageSchema()
        df[schema.date_column] = pd.to_datetime(df[schema.date_column], errors="coerce")
    return validate_usage_dataframe(df, schema)


def sample_usage_dataframe(year: int = 2023, station_ids: Iterable[int] | None = None) -> pd.DataFrame:
    """
    Build a deterministic sample dataframe for demos and tests.

    The generated dataset covers a full year with three gasoline categories and
    gently varying consumption to make charts look realistic.
    """

    station_ids = list(station_ids or (101, 102))
    records = []
    gasoline_types = ("Regular", "Premium", "Diesel")

    for month in range(1, 13):
        for station_index, station_id in enumerate(station_ids):
            for fuel_index, fuel_type in enumerate(gasoline_types):
                base_volume = 25000 + (month * 800) + (fuel_index * 1500) - (station_index * 1200)
                seasonal_adjustment = 1.15 if month in (6, 7, 8) else 0.9 if month in (1, 2) else 1.0
                volume = round(base_volume * seasonal_adjustment, 2)
                records.append(
                    {
                        "date": pd.Timestamp(year=year, month=month, day=1),
                        "gasoline_type": fuel_type,
                        "volume": volume,
                        "station_id": station_id,
                    }
                )

    df = pd.DataFrame(records)
    return validate_usage_dataframe(df)


def pivot_usage_by_type(df: pd.DataFrame, schema: UsageSchema | None = None) -> pd.DataFrame:
    """Pivot the dataset into a wide format with one column per gasoline type."""

    schema = schema or UsageSchema()
    validated = validate_usage_dataframe(df, schema)
    pivot = (
        validated.pivot_table(
            index=schema.date_column,
            columns=schema.type_column,
            values=schema.volume_column,
            aggfunc="sum",
        )
        .sort_index()
        .fillna(0.0)
    )
    pivot.columns.name = None
    return pivot


def filter_by_station(df: pd.DataFrame, station_ids: Iterable[str | int]) -> pd.DataFrame:
    """Return rows matching the requested station identifiers."""

    station_ids = {str(station_id) for station_id in station_ids}
    return df[df["station_id"].astype(str).isin(station_ids)].copy()


def export_usage_to_csv(df: pd.DataFrame, destination: str | Path, *, index: bool = False) -> Path:
    """Export a dataframe to CSV, returning the resolved path."""

    destination_path = Path(destination)
    df.to_csv(destination_path, index=index)
    return destination_path.resolve()
