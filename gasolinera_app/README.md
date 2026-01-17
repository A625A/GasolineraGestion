Gasolinera App
==============

Modular Python toolkit for planning and operating a gasoline distribution network.

Key modules
-----------
- `gasolinera.data`: CSV upload helpers, validation, and rich sample data.
- `gasolinera.forecast`: Prophet/ARIMA forecaster for monthly consumption per fuel type.
- `gasolinera.analytics`: Peak-hour detection, anomaly spotting, and heatmap utilities.
- `gasolinera.mapping`: KMeans-based site recommendations plus Folium visualisations.
- `gasolinera.scheduling`: Calendar façade with Google Calendar placeholder client.
- `gasolinera.notifications`: Notification dispatcher with console/email stubs.
- `gasolinera.inventory`: In-memory stock management helpers.
- `gasolinera.security`: Minimal user/role service ready for integration in a web UI.
- `gasolinera.contacts`: Owner-defined contact book with category management and export helpers.

Optional integrations
---------------------
- **Calendar**: Replace `GoogleCalendarClient` with a real Google API client (see `google-api-python-client` docs).
- **Notifications**: Plug in email/SMS providers by implementing the `NotificationBackend` protocol.
- **Dashboards**: The package can power Streamlit or Flask apps (not included, but requirements list optional packages).
- **Reports**: Use `reportlab`/`openpyxl` to export PDFs or spreadsheets via the provided dataframes.
REST API
--------
Launch a navigation-friendly REST API (logos served alongside data) using Flask:

```bash
export PYTHONPATH=$(pwd)/gasolinera_app:$PYTHONPATH  # ensure the package is importable
export FLASK_APP=gasolinera.gasolinera.api:create_app
flask run --port 8000
# or directly:
PYTHONPATH=$(pwd)/gasolinera_app python -m gasolinera.gasolinera.api
```

Key endpoints (all JSON):
- `GET /api/health` – service heartbeat.
- `GET /api/logos` – fuel-type logos (SVG) for UI clients.
- `POST /api/usage/upload` – ingest historical CSVs (file or JSON).
- `GET /api/forecast/<fuel_type>` – monthly usage predictions (with logo URL).
- `GET /api/analytics/peaks` – daily/weekly peak-hour breakdown.
- `GET /api/mapping/recommendations` – suggested station coordinates (requires scikit-learn).
- `GET /api/inventory`, `POST /api/inventory/delivery` – track tank levels and deliveries.
- `GET/POST /api/schedule/deliveries` – manage calendar events and reorder ETAs.
- `GET /api/contacts`, `POST /api/contacts`, `DELETE /api/contacts/<id>` – maintain supplier/fleet contact information.
- `GET /api/contacts/categories`, `POST /api/contacts/categories` – manage owner-defined contact categories.
- `GET /api/dashboard/summary` – aggregate stats for dashboards.

Static assets (SVG logos) live in `gasolinera_app/static/logos`; replace them with brand assets as needed.

Streamlit UI
------------
For an interactive web app, run:

```bash
PYTHONPATH=$(pwd)/gasolinera_app:$PYTHONPATH streamlit run streamlit_app.py
```

The UI includes navigation for forecasting, peak analytics, contact management (with category filters and exports), location recommendations (Folium map), and inventory snapshots. Upload a CSV in the Forecast section to work with real usage data, or rely on the bundled samples for demos.

Test account
------------
Set credentials in `env/backend.env` (or your local env file) before testing authentication.

Before testing the end-to-end flow, ensure the Streamlit UI is running:

```bash
PYTHONPATH=$(pwd)/gasolinera_app:$PYTHONPATH streamlit run streamlit_app.py
```

Setup
-----
1. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   Prophet and Folium are optional; remove them if you do not need forecasting/maps.
   When pip installs scripts into `~/Library/Python/3.9/bin`, run:

   ```bash
   source scripts/add_local_python_bin.sh
   ```

   to add the directory to your `PATH` for commands like `flask`, `pytest`, and `streamlit`.

2. Launch the interactive CLI console (navigable menu with ASCII UI):

   ```bash
   python demo.py
   ```

   The console covers forecasting, peak analysis, scheduling, mapping, inventory, contacts, and API references. Mapping features require `scikit-learn` and `folium`; if absent that menu item is skipped gracefully.

3. Execute the unit tests (requires `pytest`):

   ```bash
   pytest
   ```

Data requirements
-----------------
- Historical usage CSVs must contain: `date`, `gasoline_type`, `volume`, `station_id`.
- Sales logs for peak analysis expect: `timestamp`, `volume`, and optionally `station_id`.
- Geo datasets for location planning require latitude/longitude columns (default names `latitude`/`longitude`).

Next steps
----------
- Swap demo data with production sources or data warehouse queries.
- Wire the package into a Streamlit or Flask admin dashboard with authentication via `gasolinera.security`.
- Persist inventory events to a relational database for full auditing.

Containerized deployment
------------------------
The repository root now ships Docker assets to orchestrate the frontend, backend, and supporting infrastructure:

1. Update the environment samples under `env/` (`frontend.env`, `backend.env`) with real secrets, database credentials, and Mapbox/API keys.
2. (Optional) Drop TLS certificates into `infra/certs/` and adjust `infra/nginx.conf` with the correct domain.
3. Build and start the stack:

   ```bash
   docker compose build
   docker compose up -d
   ```

   Services started:
   - `frontend` – Next.js dashboard (exposed via Nginx on ports 80/443).
   - `backend` – FastAPI API container.
   - `postgres` – PostGIS-enabled PostgreSQL instance.
   - `redis` – Password-protected Redis suitable for Celery/socket usage.
   - `nginx` – Reverse proxy handling TLS and routing to frontend/backend.

   Stop everything with `docker compose down` when finished.

The backend skeleton is located in `backend/app/`; expand it with the production routes/services. The dashboard Dockerfile lives in `dashboard_app/Dockerfile` and mirrors the architecture guidance provided earlier.
