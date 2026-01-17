"""
Flask-based REST API exposing the gasolinera analytics toolkit.

The API keeps state in-memory so it can be demonstrated without a backing data
store; swap the global registries for database repositories when integrating in
production.  Endpoints return JSON payloads designed for dashboards or mobile
apps and include logo URLs to ease navigation in UI clients.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory, url_for

from .analytics import PeakHourAnalyzer, sample_sales_log
from .contacts import ContactBook, Contact, sample_contact_book
from .data import UsageSchema, sample_usage_dataframe, validate_usage_dataframe
from .forecast import MonthlyUsageForecaster
from .inventory import InventoryManager
from .mapping import LocationRecommender, sample_geo_dataframe
from .notifications import ConsoleNotifier, NotificationMessage, NotificationService
from .scheduling import DeliveryScheduler, GoogleCalendarClient, suggest_delivery_window


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
LOGO_TYPES = ("Regular", "Premium", "Diesel")


def _serialize_dataframe(df: pd.DataFrame) -> List[Dict]:
    """
    Convert a pandas dataframe into a list of JSON-ready dicts.

    Datetime columns are converted to ISO-8601 strings to keep the API contract
    frontend-friendly.
    """

    records = df.to_dict(orient="records")
    for record in records:
        for key, value in list(record.items()):
            if isinstance(value, (pd.Timestamp, datetime)):
                record[key] = value.isoformat()
    return records


def _serialize_contact(contact: Contact) -> Dict:
    return {
        "contact_id": contact.contact_id,
        "name": contact.name,
        "email": contact.email,
        "phone": contact.phone,
        "company": contact.company,
        "notes": contact.notes,
        "categories": contact.categories,
    }


def _logo_url_for(app: Flask, fuel_type: str) -> Optional[str]:
    candidate = fuel_type.lower()
    filename = f"logos/{candidate}.svg"
    resource_path = STATIC_DIR / filename
    if resource_path.exists():
        with app.app_context():
            return url_for("static", filename=filename, _external=False)
    return None


def create_app() -> Flask:
    """
    Application factory.

    Returns a configured Flask app with routes that orchestrate the analytics,
    forecasting, and scheduling modules.
    """

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["LOGO_FOLDER"] = STATIC_DIR / "logos"

    data_store: Dict[str, pd.DataFrame] = {
        "usage": sample_usage_dataframe(),
        "sales": sample_sales_log(),
        "geo": sample_geo_dataframe(),
    }

    notification_service = NotificationService([ConsoleNotifier()])
    inventory_manager = InventoryManager()
    calendar_client = GoogleCalendarClient()
    scheduler = DeliveryScheduler(calendar_client)
    contact_book: ContactBook = sample_contact_book()

    # seed inventory snapshot
    inventory_manager.record_delivery("CDMX-01", "Regular", volume=30000, capacity=50000)
    inventory_manager.record_delivery("CDMX-01", "Diesel", volume=18000, capacity=40000)

    def _get_usage_df() -> pd.DataFrame:
        return data_store["usage"]

    def _get_sales_df() -> pd.DataFrame:
        return data_store["sales"]

    def _get_geo_df() -> pd.DataFrame:
        return data_store["geo"]

    @app.route("/api/health", methods=["GET"])
    def health():
        now = datetime.utcnow()
        return jsonify({"status": "ok", "timestamp": now.isoformat()})

    @app.route("/api/logos", methods=["GET"])
    def list_logos():
        logos = {}
        for fuel_type in LOGO_TYPES:
            url = _logo_url_for(app, fuel_type)
            if url:
                logos[fuel_type.lower()] = url
        return jsonify({"logos": logos})

    @app.route("/api/logos/<fuel_type>", methods=["GET"])
    def fetch_logo(fuel_type: str):
        filename = f"{fuel_type.lower()}.svg"
        path = app.config["LOGO_FOLDER"] / filename
        if not path.exists():
            return jsonify({"error": f"Logo for '{fuel_type}' not found."}), 404
        return send_from_directory(app.config["LOGO_FOLDER"], filename)

    @app.route("/api/usage/sample", methods=["GET"])
    def sample_usage():
        df = _get_usage_df()
        schema = UsageSchema()
        types = sorted(df[schema.type_column].unique())
        return jsonify({"gasoline_types": types, "records": _serialize_dataframe(df.head(24))})

    @app.route("/api/usage/upload", methods=["POST"])
    def upload_usage():
        if "file" in request.files:
            csv_file = request.files["file"]
            df = pd.read_csv(csv_file)
        else:
            payload = request.get_json(silent=True)
            if not payload:
                return jsonify({"error": "Provide a CSV file or JSON payload."}), 400
            df = pd.DataFrame(payload)

        try:
            validated = validate_usage_dataframe(df)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        data_store["usage"] = validated
        return jsonify({"message": "Usage data uploaded successfully.", "rows": len(validated)})

    @app.route("/api/forecast/<fuel_type>", methods=["GET"])
    def forecast_endpoint(fuel_type: str):
        periods = int(request.args.get("periods", 6))
        model_type = request.args.get("model", "prophet")
        forecaster = MonthlyUsageForecaster(model=model_type)
        usage_df = _get_usage_df()
        try:
            forecaster.fit(usage_df, gasoline_type=fuel_type)
            forecast = forecaster.predict(periods=periods)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # pragma: no cover - runtime safeguard
            return jsonify({"error": f"Failed to generate forecast: {exc}"}), 500

        return jsonify(
            {
                "fuel_type": fuel_type.title(),
                "model": forecaster.model_type,
                "forecast": _serialize_dataframe(forecast),
                "logo": _logo_url_for(app, fuel_type),
            }
        )

    @app.route("/api/analytics/peaks", methods=["GET"])
    def analytics_peaks():
        station_id = request.args.get("station_id", "CDMX-01")
        analyzer = PeakHourAnalyzer()
        analyzer.fit(_get_sales_df())
        summary = analyzer.get_station_summary(station_id) or analyzer.get_station_summary("all")
        if summary is None:
            return jsonify({"error": "No peak data available."}), 404

        top_hours = summary.top_hours.head(5).to_dict()
        by_weekday = summary.by_weekday.to_dict()
        heatmap = summary.heatmap.fillna(0.0).to_dict()
        return jsonify(
            {
                "station_id": station_id,
                "top_hours": top_hours,
                "by_weekday": by_weekday,
                "heatmap": heatmap,
            }
        )

    @app.route("/api/analytics/spikes", methods=["GET"])
    def analytics_spikes():
        threshold = float(request.args.get("threshold", 2.5))
        analyzer = PeakHourAnalyzer()
        spikes_df = analyzer.detect_spikes(_get_sales_df(), zscore_threshold=threshold)
        spike_records = spikes_df[spikes_df["is_spike"]]
        return jsonify({"threshold": threshold, "spikes": _serialize_dataframe(spike_records)})

    @app.route("/api/mapping/recommendations", methods=["GET"])
    def mapping_recommendations():
        clusters = int(request.args.get("clusters", 3))
        recommender = LocationRecommender(n_clusters=clusters)
        try:
            recommender.fit(_get_geo_df())
            recommendations = recommender.recommend()
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "requires": "scikit-learn and folium"}), 503
        return jsonify(
            {
                "clusters": clusters,
                "recommendations": _serialize_dataframe(recommendations),
            }
        )

    @app.route("/api/contacts", methods=["GET"])
    def list_contacts():
        category = request.args.get("category")
        contacts = contact_book.list_contacts(category=category)
        return jsonify({"contacts": [_serialize_contact(contact) for contact in contacts], "category": category})

    @app.route("/api/contacts", methods=["POST"])
    def create_contact():
        payload = request.get_json(silent=True) or {}
        name = payload.get("name")
        if not name:
            return jsonify({"error": "Contact name is required."}), 400
        categories = payload.get("categories", [])
        contact = contact_book.create_contact(
            name=name,
            email=payload.get("email"),
            phone=payload.get("phone"),
            company=payload.get("company"),
            notes=payload.get("notes"),
            categories=categories,
        )
        return jsonify({"contact": _serialize_contact(contact)}), 201

    @app.route("/api/contacts/<contact_id>", methods=["DELETE"])
    def delete_contact(contact_id: str):
        if not contact_book.get_contact(contact_id):
            return jsonify({"error": "Contact not found."}), 404
        contact_book.remove_contact(contact_id)
        return jsonify({"message": "Contact deleted."})

    @app.route("/api/contacts/categories", methods=["GET"])
    def list_categories():
        return jsonify({"categories": contact_book.categories})

    @app.route("/api/contacts/categories", methods=["POST"])
    def create_category():
        payload = request.get_json(silent=True) or {}
        category = payload.get("category")
        if not category:
            return jsonify({"error": "Category name is required."}), 400
        contact_book.add_category(category)
        return jsonify({"categories": contact_book.categories}), 201

    @app.route("/api/inventory", methods=["GET"])
    def inventory_snapshot():
        snapshot = []
        for record in inventory_manager.get_snapshot().values():
            snapshot.append(
                {
                    "station_id": record.station_id,
                    "gasoline_type": record.gasoline_type,
                    "current_level": record.current_level,
                    "capacity": record.capacity,
                    "updated_at": record.updated_at.isoformat(),
                    "reorder_needed": inventory_manager.reorder_needed(record.station_id, record.gasoline_type),
                    "logo": _logo_url_for(app, record.gasoline_type),
                }
            )
        return jsonify({"inventory": snapshot})

    @app.route("/api/inventory/delivery", methods=["POST"])
    def inventory_delivery():
        payload = request.get_json(silent=True) or {}
        try:
            station_id = payload["station_id"]
            gasoline_type = payload["gasoline_type"]
            volume = float(payload["volume"])
            capacity = float(payload.get("capacity", volume))
        except KeyError as exc:
            return jsonify({"error": f"Missing field: {exc.args[0]}"}), 400

        record = inventory_manager.record_delivery(station_id, gasoline_type, volume=volume, capacity=capacity)
        notification_service.send(
            NotificationMessage(
                subject=f"Delivery recorded - {record.station_id}",
                body=f"{volume:.0f}L of {record.gasoline_type} added. Current level {record.current_level:.0f}L.",
                category="inventory",
            )
        )
        return jsonify({"message": "Delivery recorded.", "record": record.__dict__})

    @app.route("/api/schedule/deliveries", methods=["POST"])
    def schedule_delivery():
        payload = request.get_json(silent=True) or {}
        try:
            station_id = payload["station_id"]
            summary = payload.get("summary", f"Delivery - Station {station_id}")
            start_time = datetime.fromisoformat(payload["start"])
            duration_minutes = int(payload.get("duration_minutes", 120))
        except KeyError as exc:
            return jsonify({"error": f"Missing field: {exc.args[0]}"}), 400
        except ValueError as exc:
            return jsonify({"error": f"Invalid date: {exc}"}), 400

        event = scheduler.schedule_fuel_delivery(
            summary,
            start=start_time,
            duration=timedelta(minutes=duration_minutes),
            station_id=station_id,
            notes=payload.get("notes"),
        )
        eta = suggest_delivery_window(start_time, usage_rate_per_day=float(payload.get("usage_per_day", 4000)), reorder_threshold=float(payload.get("reorder_threshold", 12000)))
        notification_service.send(
            NotificationMessage(
                subject=f"Delivery scheduled for station {station_id}",
                body=f"{summary} on {start_time:%Y-%m-%d %H:%M}. Next reorder expected around {eta:%Y-%m-%d %H:%M}.",
                category="delivery",
            )
        )
        return jsonify({"event": event.__dict__, "reorder_eta": eta.isoformat()})

    @app.route("/api/schedule/deliveries", methods=["GET"])
    def list_scheduled_deliveries():
        start = datetime.utcnow()
        end = start + timedelta(days=30)
        events = calendar_client.list_events(start, end)
        payload = []
        for event in events:
            payload.append(
                {
                    "summary": event.summary,
                    "start": event.start.isoformat(),
                    "end": event.end.isoformat(),
                    "description": event.description,
                    "metadata": event.metadata,
                }
            )
        return jsonify({"events": payload})

    @app.route("/api/dashboard/summary", methods=["GET"])
    def dashboard_summary():
        usage_df = _get_usage_df()
        schema = UsageSchema()
        totals = usage_df.groupby(schema.type_column)[schema.volume_column].sum().to_dict()
        latest_month = usage_df[schema.date_column].max()

        response = {
            "totals": totals,
            "latest_month": latest_month.isoformat() if isinstance(latest_month, pd.Timestamp) else str(latest_month),
            "logos": {fuel.lower(): _logo_url_for(app, fuel) for fuel in LOGO_TYPES},
            "contacts": {
                "total": len(contact_book.list_contacts()),
                "categories": contact_book.categories,
            },
        }
        return jsonify(response)

    return app


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    app = create_app()
    app.run(debug=True, port=8000)
