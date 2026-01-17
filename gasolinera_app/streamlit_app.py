"""Streamlit UI for the gasolinera analytics toolkit."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import streamlit as st

from gasolinera.analytics import PeakHourAnalyzer, sample_sales_log
from gasolinera.contacts import ContactBook, sample_contact_book
from gasolinera.data import UsageSchema, sample_usage_dataframe, validate_usage_dataframe
from gasolinera.forecast import MonthlyUsageForecaster, prepare_monthly_series
from gasolinera.inventory import InventoryManager
from gasolinera.mapping import LocationRecommender, build_folium_map, sample_geo_dataframe

# Optional imports (wrapped in try/except later)
try:
    import folium  # noqa: F401
    from streamlit.components.v1 import html as st_html
except ImportError:  # pragma: no cover - streamlit fallback
    folium = None  # type: ignore
    st_html = None  # type: ignore


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def init_session_state() -> None:
    if "usage_df" not in st.session_state:
        st.session_state.usage_df = sample_usage_dataframe()
    if "sales_df" not in st.session_state:
        st.session_state.sales_df = sample_sales_log()
    if "geo_df" not in st.session_state:
        st.session_state.geo_df = sample_geo_dataframe()
    if "contact_book" not in st.session_state:
        st.session_state.contact_book = sample_contact_book()
    if "inventory" not in st.session_state:
        inventory = InventoryManager()
        inventory.record_delivery("CDMX-01", "Regular", volume=30000, capacity=50000)
        inventory.record_delivery("CDMX-01", "Diesel", volume=18000, capacity=40000)
        st.session_state.inventory = inventory


def get_logo_path(fuel: str) -> Optional[Path]:
    path = STATIC_DIR / "logos" / f"{fuel.lower()}.svg"
    return path if path.exists() else None


def show_logo_gallery() -> None:
    cols = st.columns(3)
    for col, fuel in zip(cols, ["Regular", "Premium", "Diesel"]):
        path = get_logo_path(fuel)
        if path:
            col.image(str(path), caption=fuel, use_column_width=True)
        else:
            col.write(f"{fuel} logo missing.")


def dashboard_tab() -> None:
    usage_df: pd.DataFrame = st.session_state.usage_df
    schema = UsageSchema()
    st.subheader("Operational Snapshot")

    totals = usage_df.groupby(schema.type_column)[schema.volume_column].sum().sort_values(ascending=False)
    latest_month = usage_df[schema.date_column].max()

    st.metric("Months of history", len(usage_df[schema.date_column].dt.to_period("M").unique()))
    st.metric("Latest month", latest_month.strftime("%B %Y"))

    st.bar_chart(totals, height=300)
    st.caption("Total consumption per gasoline type (litres).")

    st.markdown("### Logos")
    show_logo_gallery()


def forecast_tab() -> None:
    usage_df: pd.DataFrame = st.session_state.usage_df
    schema = UsageSchema()
    st.subheader("Monthly Usage Prediction")

    uploaded_file = st.file_uploader("Upload usage CSV (optional)", type=["csv"], help="Use columns date, gasoline_type, volume, station_id.")
    if uploaded_file:
        try:
            df = pd.read_csv(uploaded_file)
            usage_df = validate_usage_dataframe(df, schema)
            st.session_state.usage_df = usage_df
            st.success("Usage data updated from CSV.")
        except Exception as exc:  # pragma: no cover - user input
            st.error(f"Failed to load CSV: {exc}")

    fuel_types = sorted(usage_df[schema.type_column].unique())
    fuel_type = st.selectbox("Gasoline type", options=fuel_types)
    months = st.slider("Months to predict", min_value=3, max_value=24, value=6)
    model = st.selectbox("Model", options=["prophet", "arima"], index=0)

    forecaster = MonthlyUsageForecaster(model=model)
    try:
        forecaster.fit(usage_df, gasoline_type=fuel_type)
        forecast_df = forecaster.predict(periods=months)
        history_df = prepare_monthly_series(usage_df, fuel_type)
    except Exception as exc:  # pragma: no cover - runtime fallback
        st.error(f"Unable to build forecast: {exc}")
        return

    combined = pd.concat(
        [
            history_df.assign(series="Historical").rename(columns={"y": "volume"}),
            forecast_df.assign(series="Forecast").rename(columns={"yhat": "volume"})[["ds", "volume", "series"]],
        ]
    )
    chart_data = combined.pivot(index="ds", columns="series", values="volume")
    st.line_chart(chart_data, height=320)
    st.dataframe(forecast_df, use_container_width=True)


def peak_tab() -> None:
    st.subheader("Peak Hour Analysis")
    sales_df: pd.DataFrame = st.session_state.sales_df
    analyzer = PeakHourAnalyzer()
    analyzer.fit(sales_df)
    summary = analyzer.get_station_summary("CDMX-01")
    if not summary:
        st.info("No sales data available.")
        return

    top_hours = summary.top_hours.head(7).reset_index()
    top_hours.columns = ["Hour", "Litres"]
    st.table(top_hours)

    weekday_df = summary.by_weekday.reset_index()
    weekday_df.columns = ["Weekday", "Litres"]
    st.bar_chart(weekday_df.set_index("Weekday"))

    heatmap_df = summary.heatmap.copy()
    st.dataframe(heatmap_df.style.background_gradient(cmap="YlGnBu"), use_container_width=True)


def mapping_tab() -> None:
    st.subheader("Location Recommendations")
    geo_df: pd.DataFrame = st.session_state.geo_df
    clusters = st.slider("Number of suggested sites", min_value=2, max_value=8, value=3)
    recommender = LocationRecommender(n_clusters=clusters)
    try:
        recommender.fit(geo_df)
        recommendations = recommender.recommend()
    except RuntimeError as exc:
        st.warning(f"Install scikit-learn to enable clustering. Details: {exc}")
        return

    st.dataframe(recommendations, use_container_width=True)

    if folium and st_html:
        fmap = build_folium_map(geo_df, recommendations)
        if fmap:
            st_html(fmap._repr_html_(), height=420)
    else:
        st.info("Install `folium` and `streamlit` extras to view interactive maps.")


def contacts_tab() -> None:
    st.subheader("Contact Directory")
    contact_book: ContactBook = st.session_state.contact_book

    with st.expander("Add new contact"):
        name = st.text_input("Name")
        company = st.text_input("Company")
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        notes = st.text_area("Notes")
        categories = st.multiselect("Categories", options=contact_book.categories)
        new_category = st.text_input("Add category", key="new_category")
        if st.button("Save contact", use_container_width=True):
            if new_category:
                contact_book.add_category(new_category)
                categories.append(new_category)
            if not name:
                st.error("Name is required.")
            else:
                contact_book.create_contact(
                    name=name,
                    company=company or None,
                    phone=phone or None,
                    email=email or None,
                    notes=notes or None,
                    categories=categories,
                )
                st.success(f"Contact {name} saved.")
                st.rerun()

    selected_category = st.selectbox("Filter by category", options=["All"] + contact_book.categories)
    if selected_category == "All":
        contacts = contact_book.list_contacts()
    else:
        contacts = contact_book.list_contacts(category=selected_category)

    records: Dict[str, list] = {
        "Name": [],
        "Company": [],
        "Phone": [],
        "Email": [],
        "Categories": [],
    }
    for contact in contacts:
        records["Name"].append(contact.name)
        records["Company"].append(contact.company or "")
        records["Phone"].append(contact.phone or "")
        records["Email"].append(contact.email or "")
        records["Categories"].append(", ".join(contact.categories))

    st.dataframe(pd.DataFrame(records), use_container_width=True)

    export_choice = st.selectbox("Export format", options=["None", "CSV", "JSON"])
    if export_choice == "CSV":
        csv_buffer = io.BytesIO()
        contact_book.to_dataframe().to_csv(csv_buffer, index=False)
        st.download_button("Download contacts CSV", data=csv_buffer.getvalue(), file_name="contacts.csv", mime="text/csv")
    elif export_choice == "JSON":
        payload = [contact.__dict__ for contact in contact_book.list_contacts()]
        st.download_button(
            "Download contacts JSON",
            data=json.dumps(payload, indent=2),
            file_name="contacts.json",
            mime="application/json",
        )


def inventory_tab() -> None:
    st.subheader("Inventory Snapshot")
    inventory: InventoryManager = st.session_state.inventory
    records = []
    for record in inventory.get_snapshot().values():
        records.append(
            {
                "Station": record.station_id,
                "Gasoline": record.gasoline_type,
                "Current": record.current_level,
                "Capacity": record.capacity,
                "Last updated": record.updated_at,
            }
        )
    st.dataframe(pd.DataFrame(records), use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Gasolinera Control Center", layout="wide")
    st.title("⛽ Gasolinera Control Center")
    st.caption("Unified dashboard for forecasting, analytics, contacts, and expansion planning.")

    init_session_state()

    menu = st.sidebar.radio(
        "Navigate",
        options=[
            "Dashboard",
            "Forecast",
            "Peak analysis",
            "Mapping",
            "Contacts",
            "Inventory",
        ],
    )

    if menu == "Dashboard":
        dashboard_tab()
    elif menu == "Forecast":
        forecast_tab()
    elif menu == "Peak analysis":
        peak_tab()
    elif menu == "Mapping":
        mapping_tab()
    elif menu == "Contacts":
        contacts_tab()
    elif menu == "Inventory":
        inventory_tab()


if __name__ == "__main__":
    main()
