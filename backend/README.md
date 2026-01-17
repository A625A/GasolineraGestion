# Gasolinera Backend

## Gasoline analytics endpoint

The FastAPI service exposes `GET /api/gasoline/stats`, which powers the dashboard
widgets. The handler synthesises a daily consumption history for Regular, Super
and Diesel fuel types and returns:

- Aggregated tank capacity and current inventory for the site.
- Per-fuel KPIs (weekly litres sold, current inventory, 7-day average
  consumption, pricing and recent restock date).
- Predictive restocking guidance including projected depletion date, suggested
  restock window and a simple confidence score.
- Notification payloads when a fuel type is expected to run out within seven
  days.

### Prediction model

The predictive logic is intentionally lightweight so it can operate without a
full ML stack:

1. Slice the last seven days of consumption, convert litres to daily totals and
   compute a moving average.
2. Estimate days-to-depletion as `current_inventory / average_daily`.
3. Derive the projected depletion date and shift it by the configured delivery
   lead time to recommend a restock date. When the calculated restock date lands
   in the past, the service elevates the recommendation to today.
4. Flag a `warning` when the projected depletion window is seven days or less
   and `critical` when it is three days or less to drive in-app notifications.
5. Produce a confidence score between 0.3 and 0.9 based on the observed demand
   magnitude so consumers can weigh the recommendation alongside field
   knowledge.

All dates are returned in ISO-8601 format, making them easy to parse on both web
and mobile clients. The dataset can be swapped with a live data source by
replacing the `_FUEL_METADATA` dictionary inside
`app/api/gasoline.py`.
