"""
Geo-analytics helpers for recommending new station locations.

Clustering is used as a lightweight proxy for more sophisticated demand
modelling.  The module offers both scikit-learn and folium integrations, but
will degrade gracefully when those optional dependencies are absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class LocationRecommendation:
    """Represents a suggested new station site."""

    latitude: float
    longitude: float
    potential_demand: float


class LocationRecommender:
    """
    Fit a KMeans model on historical sales geodata to find underserved areas.

    Parameters
    ----------
    n_clusters:
        Controls the number of recommendations.  Start with a small number (3–5)
        and adjust based on geography and budget.
    """

    def __init__(
        self,
        n_clusters: int = 3,
        *,
        lat_column: str = "latitude",
        lon_column: str = "longitude",
        weight_column: str | None = "volume",
    ):
        self.n_clusters = n_clusters
        self.lat_column = lat_column
        self.lon_column = lon_column
        self.weight_column = weight_column
        self._model = None
        self._features: Optional[pd.DataFrame] = None
        self._training_data: Optional[pd.DataFrame] = None

    def fit(self, station_df: pd.DataFrame) -> None:
        """
        Fit the clustering model using existing station performance data.

        The dataframe is expected to contain latitude/longitude columns and a
        numeric ``volume`` column representing throughput or demand.
        """

        try:
            from sklearn.cluster import KMeans
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("scikit-learn is required for clustering features.") from exc

        features = station_df[[self.lat_column, self.lon_column]].copy()
        weights = (
            station_df[self.weight_column].astype(float).values
            if self.weight_column and self.weight_column in station_df.columns
            else None
        )

        self._model = KMeans(n_clusters=self.n_clusters, n_init="auto", random_state=42)
        self._model.fit(features, sample_weight=weights)
        self._features = features
        self._training_data = station_df[[self.lat_column, self.lon_column]].copy()
        if self.weight_column and self.weight_column in station_df.columns:
            self._training_data[self.weight_column] = station_df[self.weight_column].astype(float)

    def recommend(self) -> pd.DataFrame:
        """
        Return a dataframe with cluster centroids and estimated demand.

        The estimation simply averages the weight of the points that belong to
        each cluster.  More advanced approaches could incorporate demographic
        datasets or traffic intensity layers.
        """

        if self._model is None or self._features is None:
            raise RuntimeError("Call fit() before requesting recommendations.")

        labels = self._model.labels_
        features = self._features.copy()
        features["cluster"] = labels

        aggregated = features.groupby("cluster").agg({self.lat_column: "mean", self.lon_column: "mean"})
        if hasattr(self, "_training_data") and self.weight_column and self.weight_column in getattr(self, "_training_data").columns:
            training = self._training_data.copy()
            training["cluster"] = labels
            aggregated["potential_demand"] = training.groupby("cluster")[self.weight_column].mean()
        else:
            aggregated["potential_demand"] = 1.0
        recommendations = aggregated.reset_index(drop=True)
        return recommendations.rename(columns={self.lat_column: "latitude", self.lon_column: "longitude"})


def build_folium_map(existing: pd.DataFrame, recommendations: pd.DataFrame, *, tiles: str = "CartoDB positron"):
    """
    Visualise current and suggested locations using Folium.

    Returns the map object so callers can save it as HTML or display it in a
    notebook.  The function silently skips map generation if Folium is not
    installed to keep dependencies optional.
    """

    try:
        import folium
    except ImportError:  # pragma: no cover - optional dependency
        print("Folium not installed; skipping map rendering.")
        return None

    center_lat = existing["latitude"].mean()
    center_lon = existing["longitude"].mean()
    fmap = folium.Map(location=[center_lat, center_lon], zoom_start=6, tiles=tiles)

    for _, row in existing.iterrows():
        folium.CircleMarker(
            location=[row["latitude"], row["longitude"]],
            radius=5,
            popup=f"Existing Station (ID: {row.get('station_id', 'N/A')})",
            color="blue",
            fill=True,
            fill_opacity=0.7,
        ).add_to(fmap)

    for _, row in recommendations.iterrows():
        folium.Marker(
            location=[row["latitude"], row["longitude"]],
            popup=f"Suggested Site – Demand Score: {row.get('potential_demand', 1.0):.2f}",
            icon=folium.Icon(color="green", icon="flag"),
        ).add_to(fmap)

    return fmap


def sample_geo_dataframe() -> pd.DataFrame:
    """
    Provide a small deterministic dataset with station coordinates and volume.

    The coordinates approximate cities in Mexico for realistic mapping demos.
    """

    data = [
        {"station_id": "CDMX-01", "latitude": 19.4326, "longitude": -99.1332, "volume": 120000},
        {"station_id": "GDL-02", "latitude": 20.6597, "longitude": -103.3496, "volume": 95000},
        {"station_id": "MTY-03", "latitude": 25.6866, "longitude": -100.3161, "volume": 88000},
        {"station_id": "PUE-04", "latitude": 19.0414, "longitude": -98.2063, "volume": 62000},
        {"station_id": "QRO-05", "latitude": 20.5888, "longitude": -100.3899, "volume": 54000},
    ]
    return pd.DataFrame(data)
