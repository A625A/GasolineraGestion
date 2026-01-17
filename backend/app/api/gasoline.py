from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from datetime import date, datetime, time, timedelta, timezone
from math import ceil, isfinite
from typing import Any, Dict, Literal, cast

from fastapi import APIRouter, Body, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.services import sales_data, fuel_types
from app.services.state import load_state, sync_predictive_events, update_state
from app.services.email_client import EmailDeliveryError, credentials_available, send_email
from app.services.notification_preferences import get_email_notification_config


class ConsumptionPoint(BaseModel):
  date: date
  liters: float


class DailyConsumptionTotals(BaseModel):
  date: date
  totals: Dict[str, float]
  total: float


class RestockRecommendation(BaseModel):
  projectedDepletionDate: date
  recommendedRestockDate: date
  confidenceScore: float = Field(ge=0.0, le=1.0)
  daysUntilDepletion: float
  leadTimeDays: int
  status: Literal["ok", "warning", "critical"]
  message: str


class DepletionRange(BaseModel):
  minDate: date
  maxDate: date
  confidence: float = Field(ge=0.0, le=1.0)


class FuelTypeSnapshot(BaseModel):
  id: str
  fuelKey: str
  label: str
  displayName: str
  color: str
  tankCapacityLiters: float
  currentInventoryLiters: float
  utilizationPercent: float
  lastRestockDate: date | None = None
  pricePerGallon: float
  pricePerGallonSelf: float | None = None
  pricePerGallonFull: float | None = None
  averageDailyConsumptionLiters: float
  soldTodayLiters: float
  soldLast7DaysLiters: float
  soldLast30DaysLiters: float
  weeklySalesLiters: float
  sharePercent: float
  consumptionTrend: list[ConsumptionPoint]
  estimatedDepletionRange: DepletionRange | None = None
  estimatedDepletionDate: date | None = None
  nextRestockDate: date | None = None
  restock: RestockRecommendation | None = None
  needsConfiguration: bool = False
  configurationWarnings: list[str] = []
  salesStats: Dict[str, float] = Field(default_factory=dict)
  depletion: Dict[str, Any] | None = None


class InventorySummary(BaseModel):
  totalCapacityLiters: float
  currentInventoryLiters: float
  utilizationPercent: float


class RestockNotification(BaseModel):
  fuelTypeId: str
  fuelTypeLabel: str
  severity: Literal["info", "warning", "critical"]
  message: str
  recommendedRestockDate: date


class GasolineStatsResponse(BaseModel):
  generatedAt: datetime
  summary: InventorySummary
  fuelTypes: list[FuelTypeSnapshot]
  notifications: list[RestockNotification]
  consumptionTotals30Days: list[DailyConsumptionTotals]
  hasHourData: bool = False


class RestockPlanUpdate(BaseModel):
  lastRestockDate: date | None = None
  nextRestockDate: date | None = None
  tankCapacityLiters: float | None = Field(default=None, gt=0)
  currentInventoryLiters: float | None = Field(default=None, ge=0)


class FuelTypeCreate(BaseModel):
  id: str = Field(min_length=2, max_length=30, pattern="^[a-zA-Z0-9_-]+$")
  label: str = Field(min_length=2, max_length=60)
  color: str = Field(default="#60A5FA", min_length=4, max_length=16)
  tankCapacityLiters: float = Field(gt=0)
  currentInventoryLiters: float = Field(ge=0)
  pricePerGallon: float = Field(ge=0)
  leadTimeDays: int = Field(default=2, ge=0, le=30)


router = APIRouter(prefix="/api/gasoline", tags=["gasoline"])

_SENT_EMAIL_KEYS: set[tuple[str, str, str]] = set()
_GAS_STATS_CACHE: Dict[str, Any] = {"key": None, "response": None, "payloads": None}


def _iso(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _event_window(day: date) -> tuple[str, str]:
  start_dt = datetime.combine(day, time(hour=14, minute=0), tzinfo=timezone.utc)
  end_dt = start_dt + timedelta(hours=1)
  return _iso(start_dt), _iso(end_dt)


def _build_fuel_catalog(state: Dict[str, Any], sales_keys: set[str]) -> Dict[str, Dict[str, Any]]:
  presets = fuel_types.get_fuel_presets()
  catalog = {entry["id"]: dict(entry) for entry in fuel_types.get_fuel_types(state)}
  missing = []

  for fuel_id in sales_keys:
    if fuel_id in catalog:
      continue
    label = fuel_types.normalize_fuel_label(fuel_id, fallback_key=fuel_id)
    catalog[fuel_id] = {"id": fuel_id, "label": label, "source": "sales"}
    missing.append((fuel_id, label))

  if missing:
    fuel_types.record_discovered_fuels(missing, source="sales")

  for fuel_id, preset in presets.items():
    if fuel_id not in catalog:
      continue
    catalog[fuel_id] = {**preset, **catalog[fuel_id], "id": fuel_id}

  return catalog


def _percentile(values: list[float], pct: float) -> float | None:
  if not values:
    return None
  ordered = sorted(values)
  k = (len(ordered) - 1) * pct
  floor_index = int(k)
  ceil_index = min(len(ordered) - 1, floor_index + 1)
  if floor_index == ceil_index:
    return ordered[floor_index]
  return ordered[floor_index] * (ceil_index - k) + ordered[ceil_index] * (k - floor_index)


def _build_trend_from_daily(start_day: date, days: int, daily_map: Dict[date, float]) -> list[ConsumptionPoint]:
  points: list[ConsumptionPoint] = []
  for offset in range(days):
    day = start_day + timedelta(days=offset)
    liters = round(daily_map.get(day, 0.0), 4)
    points.append(ConsumptionPoint(date=day, liters=liters))
  return points


def _consumption_confidence(daily_values: list[float]) -> float:
  if not daily_values:
    return 0.2
  avg = statistics.mean(daily_values)
  if avg <= 0:
    return 0.2
  deviation = statistics.pstdev(daily_values) if len(daily_values) > 1 else 0.0
  ratio = deviation / avg
  return max(0.15, min(1.0, 1.0 - min(ratio, 1.5) / 1.5))


def _build_consumption_trend(values: list[float], today: date) -> list[ConsumptionPoint]:
  points: list[ConsumptionPoint] = []
  for offset, liters in enumerate(reversed(values)):
    day = today - timedelta(days=offset)
    points.append(ConsumptionPoint(date=day, liters=liters))
  return list(reversed(points))


def _compute_restock(
  current_inventory: float,
  average_daily: float,
  lead_time: int,
  tank_capacity: float,
  today: date,
) -> RestockRecommendation:
  if average_daily <= 0:
    projected_days = float("inf")
    depletion_date = today + timedelta(days=90)
  else:
    projected_days = current_inventory / average_daily
    depletion_date = today + timedelta(days=ceil(projected_days))

  if projected_days == float("inf"):
    recommended_date = today + timedelta(days=max(lead_time, 1))
  else:
    recommended_date = depletion_date - timedelta(days=5)
    if recommended_date < today:
      recommended_date = today

  status: Literal["ok", "warning", "critical"]
  message: str
  confidence = max(0.1, min(1.0, consumption_confidence_scale(average_daily)))

  if projected_days == float("inf"):
    status = "ok"
    message = "No recent consumption detected. Monitoring inventory."
  elif projected_days <= 3:
    status = "critical"
    message = "Projected to run out within 3 days. Expedite restock."
  elif projected_days <= 7:
    status = "warning"
    message = "Inventory trending low. Schedule restock within the week."
  else:
    status = "ok"
    message = "Inventory levels stable."

  return RestockRecommendation(
    projectedDepletionDate=depletion_date,
    recommendedRestockDate=recommended_date,
    confidenceScore=confidence,
    daysUntilDepletion=projected_days,
    leadTimeDays=lead_time,
    status=status,
    message=message,
  )


def consumption_confidence_scale(average_daily: float) -> float:
  if average_daily <= 0:
    return 0.1
  base = min(average_daily / 5000, 1.0)
  return 0.3 + 0.6 * base


def _dispatch_restock_notifications(state: Dict[str, Any], notifications: list[RestockNotification]) -> None:
  if not credentials_available():
    return
  config = get_email_notification_config(state)
  if not config:
    return

  recipient, lead_days = config
  today = date.today()

  for notification in notifications:
    days_until = (notification.recommendedRestockDate - today).days
    if days_until < 0:
      continue
    if lead_days > 0 and days_until == lead_days:
      _send_email_notification(notification, recipient, days_until, "lead")
    if days_until == 0:
      _send_email_notification(notification, recipient, days_until, "day0")


def _coerce_float(value: Any) -> float | None:
  try:
    parsed = float(value)
  except (TypeError, ValueError):
    return None
  return parsed if isfinite(parsed) else None


def _build_cache_key(state: Dict[str, Any], fingerprint: str | None) -> str:
  payload = {
    "overrides": state.get("gasolineOverrides", {}),
    "custom": state.get("customFuelTypes", []),
    "discovered": state.get("discoveredFuelTypes", []),
  }
  raw = json.dumps(payload, sort_keys=True, default=str)
  digest = hashlib.sha256(raw.encode()).hexdigest()
  return f"{fingerprint or 'none'}:{digest}"


def _compute_gasoline_stats(
  state: Dict[str, Any],
  sales_state: Dict[str, Any] | None = None,
) -> tuple[GasolineStatsResponse, list[dict[str, object]], str | None]:
  overrides_raw: Dict[str, Any] = state.get("gasolineOverrides", {})
  overrides: Dict[str, Dict[str, Any]] = {}
  for key, value in overrides_raw.items():
    normalized = fuel_types.normalize_fuel_key(key)
    if not normalized:
      continue
    overrides[normalized] = value if isinstance(value, dict) else {}

  fuel_snapshots: list[FuelTypeSnapshot] = []
  total_capacity = 0.0
  total_inventory = 0.0
  weekly_sales_map: dict[str, float] = {}
  predictive_payloads: list[dict[str, object]] = []

  sales_state = sales_state or sales_data.load_sales_state()
  fingerprint = sales_state.get("sourceFingerprint")
  transactions = sales_state.get("transactions", [])
  today = sales_data.effective_today(transactions)
  sales_snapshot = sales_data.build_recent_sales(
    days=60,
    today=today,
    transactions=transactions,
    fingerprint=fingerprint,
  )
  full_history_avg = sales_data.full_history_daily_average(transactions, fingerprint=fingerprint)
  sales_by_fuel = sales_snapshot.by_fuel
  catalog = _build_fuel_catalog(state, set(sales_by_fuel.keys()))
  known_fuels = sorted(set(catalog.keys()) | set(sales_by_fuel.keys()))
  color_fallbacks = ["#60A5FA", "#34D399", "#F59E0B", "#8B5CF6", "#EC4899", "#0EA5E9"]

  monthly_key = today.strftime("%Y-%m")
  monthly_stats = sales_data.monthly_earnings(monthly_key)
  revenue_by_fuel = monthly_stats.get("revenue_by_fuel_type", {})
  gallons_by_fuel = monthly_stats.get("gallons_by_fuel_type", {})
  avg_price_by_fuel = monthly_stats.get("average_price_per_gallon_by_type", {})

  day_totals: Dict[date, Dict[str, float]] = {}
  start_window = today - timedelta(days=29)

  for idx, fuel_id in enumerate(known_fuels):
    meta = catalog.get(fuel_id, {"id": fuel_id})
    fuel_label = meta.get("label") or fuel_id.replace("-", " ").title()
    fuel_color = meta.get("color") or color_fallbacks[idx % len(color_fallbacks)]
    fuel_capacity = _coerce_float(meta.get("tank_capacity"))
    current_inventory = _coerce_float(meta.get("current_inventory"))
    lead_time = int(_coerce_float(meta.get("lead_time_days")) or 2)
    price_per_gallon = _coerce_float(meta.get("price_per_gallon")) or 0.0
    override_entry = overrides.get(fuel_id, {})

    override_capacity = _coerce_float(override_entry.get("tankCapacityLiters"))
    if override_capacity is not None and override_capacity > 0:
      fuel_capacity = override_capacity

    override_inventory = _coerce_float(override_entry.get("currentInventoryLiters"))
    if override_inventory is not None and override_inventory >= 0:
      current_inventory = override_inventory

    fuel_sales = sales_by_fuel.get(fuel_id, {"daily": {}, "prices": [], "amount": 0.0, "gallons": 0.0, "liters": 0.0, "transactions": 0})
    avg_price_gallon = statistics.mean(fuel_sales.get("prices", [])) if fuel_sales.get("prices") else 0.0
    if avg_price_gallon > 0:
      price_per_gallon = avg_price_gallon

    override_price = _coerce_float(override_entry.get("pricePerGallon"))
    if override_price is not None and override_price >= 0:
      if avg_price_gallon > 0 and override_price < avg_price_gallon:
        price_per_gallon = avg_price_gallon
      else:
        price_per_gallon = override_price

    last_restock_date = None
    override_last = override_entry.get("lastRestockDate")
    if isinstance(override_last, str):
      try:
        last_restock_date = date.fromisoformat(override_last)
      except ValueError:
        last_restock_date = None
    if last_restock_date is None and meta.get("last_restock_days_ago") is not None:
      try:
        last_restock_days = int(meta.get("last_restock_days_ago", 5))
      except (TypeError, ValueError):
        last_restock_days = 5
      last_restock_date = today - timedelta(days=last_restock_days)

    baseline_liters = _coerce_float(override_entry.get("inventoryBaselineLiters"))
    if baseline_liters is None:
      baseline_liters = _coerce_float(override_entry.get("currentInventoryLiters"))
    if baseline_liters is None:
      baseline_liters = current_inventory

    baseline_at = override_entry.get("inventoryBaselineAt")
    if not baseline_at and last_restock_date is not None:
      baseline_at = last_restock_date.isoformat()
    if baseline_at and baseline_liters is not None:
      consumed_since = sales_data.sum_liters_since(fuel_id, baseline_at, transactions=transactions)
      if consumed_since > 0:
        current_inventory = max(baseline_liters - consumed_since, 0.0)

    daily_map: Dict[date, float] = fuel_sales.get("daily", {})
    trend_points = _build_trend_from_daily(start_window, 30, daily_map)
    daily_values = [point.liters for point in trend_points]

    if not any(daily_values):
      history = meta.get("consumption_history", [])
      if history:
        trend_points = _build_consumption_trend(history[-30:], today)
        daily_values = [point.liters for point in trend_points]

    sold_today = daily_values[-1] if daily_values else 0.0
    sold_last_7 = sum(daily_values[-7:]) if daily_values else 0.0
    sold_last_30 = sum(daily_values) if daily_values else 0.0
    avg_30 = sold_last_30 / 30 if sold_last_30 > 0 else 0.0
    avg_full = full_history_avg.get(fuel_id, 0.0)
    if avg_30 > 0 and avg_full > 0:
      average_daily = (avg_30 * 0.7) + (avg_full * 0.3)
    elif avg_30 > 0:
      average_daily = avg_30
    else:
      average_daily = avg_full
    weekly_sales = sold_last_7
    weekly_sales_map[fuel_id] = weekly_sales

    missing_fields: list[str] = []
    if fuel_capacity is None or fuel_capacity <= 0:
      missing_fields.append("tankCapacityLiters")
    if current_inventory is None or current_inventory < 0:
      missing_fields.append("currentInventoryLiters")
    needs_configuration = bool(missing_fields)
    configuration_warnings: list[str] = []
    if "tankCapacityLiters" in missing_fields:
      configuration_warnings.append("Configura la capacidad del tanque para habilitar la predicción de agotamiento.")
    if "currentInventoryLiters" in missing_fields:
      configuration_warnings.append("Ingresa el inventario actual para calcular reabastecimiento.")

    safe_capacity = fuel_capacity or 0.0
    safe_inventory = current_inventory if current_inventory is not None else 0.0

    confidence = _consumption_confidence(daily_values)
    restock: RestockRecommendation | None = None
    estimated_range = None
    estimated_depletion_date = None
    next_restock_date = None
    depletion_payload = None
    default_next_restock = None

    if not needs_configuration:
      restock = _compute_restock(
        safe_inventory,
        average_daily,
        lead_time,
        safe_capacity,
        today,
      )
      restock.confidenceScore = max(restock.confidenceScore, confidence)

      range_min = _percentile(daily_values, 0.75) or average_daily
      range_max = _percentile(daily_values, 0.25) or average_daily
      min_days = safe_inventory / range_min if range_min > 0 else float("inf")
      max_days = safe_inventory / range_max if range_max > 0 else float("inf")

      depletion_days = safe_inventory / average_daily if average_daily > 0 else float("inf")
      estimated_depletion_date = today + timedelta(days=ceil(depletion_days)) if depletion_days != float("inf") else today + timedelta(days=90)
      if min_days != float("inf") and max_days != float("inf"):
        estimated_range = DepletionRange(
          minDate=today + timedelta(days=ceil(min_days)),
          maxDate=today + timedelta(days=ceil(max_days)),
          confidence=confidence,
        )

      if estimated_depletion_date is not None:
        reminder_date = estimated_depletion_date - timedelta(days=3)
        default_next_restock = reminder_date if reminder_date >= today else today
      else:
        default_next_restock = restock.recommendedRestockDate if restock.recommendedRestockDate >= today else today
      next_restock_date = default_next_restock
      override_next = override_entry.get("nextRestockDate")
      if isinstance(override_next, str):
        try:
          candidate = date.fromisoformat(override_next)
          next_restock_date = candidate
        except ValueError:
          next_restock_date = default_next_restock

      if estimated_depletion_date is not None:
        safety_buffer = max(lead_time, 3)
        reorder_by = estimated_depletion_date - timedelta(days=safety_buffer)
        if reorder_by < today:
          reorder_by = today
        depletion_payload = {
          "avg_daily_30d": average_daily,
          "empty_low": (estimated_range.minDate if estimated_range else estimated_depletion_date),
          "empty_high": (estimated_range.maxDate if estimated_range else estimated_depletion_date),
          "confidence_score": confidence,
          "reorder_by_date": reorder_by,
        }

    utilization_percent = 0.0 if safe_capacity == 0 else round(safe_inventory / safe_capacity * 100, 2)
    total_capacity += safe_capacity
    total_inventory += safe_inventory

    for point in trend_points:
      totals = day_totals.setdefault(point.date, {})
      totals[fuel_id] = point.liters

    sales_stats = {
      "gallons_today": round(sold_today / sales_data.GALLON_TO_LITER, 4),
      "gallons_last_7d": round(sold_last_7 / sales_data.GALLON_TO_LITER, 4),
      "gallons_last_30d": round(sold_last_30 / sales_data.GALLON_TO_LITER, 4),
      "revenue_mtd": round(float(revenue_by_fuel.get(fuel_id, 0.0)), 4),
      "avg_pg_mtd": round(float(avg_price_by_fuel.get(fuel_id, price_per_gallon)), 4),
    }

    snapshot = FuelTypeSnapshot(
      id=fuel_id,
      fuelKey=fuel_id,
      label=fuel_label,
      displayName=fuel_label,
      color=fuel_color,
      tankCapacityLiters=safe_capacity,
      currentInventoryLiters=safe_inventory,
      utilizationPercent=utilization_percent,
      lastRestockDate=last_restock_date,
      pricePerGallon=price_per_gallon,
      pricePerGallonSelf=price_per_gallon,
      pricePerGallonFull=price_per_gallon,
      averageDailyConsumptionLiters=average_daily,
      soldTodayLiters=sold_today,
      soldLast7DaysLiters=sold_last_7,
      soldLast30DaysLiters=sold_last_30,
      weeklySalesLiters=weekly_sales,
      sharePercent=0.0,
      consumptionTrend=trend_points,
      estimatedDepletionRange=estimated_range,
      estimatedDepletionDate=estimated_depletion_date,
      nextRestockDate=next_restock_date,
      restock=restock,
      needsConfiguration=needs_configuration,
      configurationWarnings=configuration_warnings,
      salesStats=sales_stats,
      depletion=depletion_payload,
    )
    fuel_snapshots.append(snapshot)

    if restock and next_restock_date and default_next_restock:
      event_start, event_end = _event_window(next_restock_date)
      lead_days_before = max((restock.projectedDepletionDate - next_restock_date).days, 0)
      predictive_payloads.append(
        {
          "fuelType": fuel_id,
          "title": f"Restock {fuel_label}",
          "description": restock.message,
          "start": event_start,
          "end": event_end,
          "allDay": False,
          "metadata": {
            "confidence": restock.confidenceScore,
            "projectedDepletionDate": restock.projectedDepletionDate.isoformat(),
            "recommendedVolume": round(max(safe_capacity - safe_inventory, 0.0), 2),
            "defaultNextRestock": default_next_restock.isoformat(),
            "leadDaysBefore": lead_days_before,
            "tankCapacityLiters": safe_capacity,
            "fuelLabel": fuel_label,
          },
        }
      )

  total_weekly_sales = sum(weekly_sales_map.values()) or 1.0
  for snapshot in fuel_snapshots:
    snapshot.sharePercent = round(weekly_sales_map.get(snapshot.id, 0.0) / total_weekly_sales * 100, 2)

  utilization = 0.0 if total_capacity == 0 else round(total_inventory / total_capacity * 100, 2)
  summary = InventorySummary(
    totalCapacityLiters=total_capacity,
    currentInventoryLiters=total_inventory,
    utilizationPercent=utilization,
  )

  notifications: list[RestockNotification] = []
  for snapshot in fuel_snapshots:
    if snapshot.restock is None:
      continue
    severity_map: dict[
      Literal["ok", "warning", "critical"],
      Literal["info", "warning", "critical"]
    ] = {
      "ok": "info",
      "warning": "warning",
      "critical": "critical",
    }
    severity = severity_map[snapshot.restock.status]
    if severity in {"warning", "critical"}:
      notifications.append(
        RestockNotification(
          fuelTypeId=snapshot.id,
          fuelTypeLabel=snapshot.label,
          severity=severity,
          message=snapshot.restock.message,
          recommendedRestockDate=snapshot.restock.recommendedRestockDate,
        )
      )

  fuel_list = sorted(known_fuels)
  daily_consumption: list[DailyConsumptionTotals] = []
  for offset in range(30):
    day = start_window + timedelta(days=offset)
    totals_for_day = {fuel: day_totals.get(day, {}).get(fuel, 0.0) for fuel in fuel_list}
    daily_consumption.append(DailyConsumptionTotals(
      date=day,
      totals=totals_for_day,
      total=sum(totals_for_day.values()),
    ))

  response = GasolineStatsResponse(
    generatedAt=datetime.now(timezone.utc),
    summary=summary,
    fuelTypes=fuel_snapshots,
    notifications=notifications,
    consumptionTotals30Days=daily_consumption,
    hasHourData=sales_snapshot.has_hour_data,
  )
  return response, predictive_payloads, fingerprint


def build_gasoline_stats(*, with_side_effects: bool = True) -> GasolineStatsResponse:
  state = load_state()
  sales_state = sales_data.load_sales_state()
  fingerprint = sales_state.get("sourceFingerprint")
  cache_key = _build_cache_key(state, fingerprint)
  cached = _GAS_STATS_CACHE.get("response") if _GAS_STATS_CACHE.get("key") == cache_key else None
  if cached is not None:
    predictive_payloads = _GAS_STATS_CACHE.get("payloads") or []
    if with_side_effects:
      sync_predictive_events(predictive_payloads)
      _dispatch_restock_notifications(state, cached.notifications)
    return cached

  response, predictive_payloads, _ = _compute_gasoline_stats(state, sales_state)
  _GAS_STATS_CACHE.update({
    "key": cache_key,
    "response": response,
    "payloads": predictive_payloads,
  })
  if with_side_effects:
    sync_predictive_events(predictive_payloads)
    _dispatch_restock_notifications(state, response.notifications)
  return response


def set_fuel_price_override(fuel_id: str, price: float | None) -> None:
  def _mutator(state: Dict[str, Any]) -> None:
    normalized = fuel_types.normalize_fuel_key(fuel_id) or fuel_id
    overrides = state.setdefault("gasolineOverrides", {})
    entry = dict(overrides.get(normalized, {}))
    if price is None:
      entry.pop("pricePerGallon", None)
    else:
      entry["pricePerGallon"] = float(price)
    if entry:
      overrides[normalized] = entry
    else:
      overrides.pop(normalized, None)
    state["gasolineOverrides"] = overrides

  update_state(_mutator)


@router.get("/stats", response_model=GasolineStatsResponse)
def gasoline_stats() -> GasolineStatsResponse:
  return build_gasoline_stats(with_side_effects=True)


@router.patch("/restock/{fuel_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_restock_plan(fuel_id: str, payload: RestockPlanUpdate) -> Response:
  if (
    payload.lastRestockDate is None
    and payload.nextRestockDate is None
    and payload.tankCapacityLiters is None
    and payload.currentInventoryLiters is None
  ):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide at least one field to update.")
  if payload.tankCapacityLiters is not None and payload.tankCapacityLiters <= 0:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tank capacity must be greater than zero.")
  if payload.currentInventoryLiters is not None and payload.currentInventoryLiters < 0:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current inventory must be zero or greater.")
  try:
    normalized_id = fuel_types.validate_fuel_id(fuel_id)
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

  def _mutator(state: Dict[str, Any]) -> None:
    overrides = state.setdefault("gasolineOverrides", {})
    entry = dict(overrides.get(normalized_id, {}))
    previous_inventory = _coerce_float(entry.get("currentInventoryLiters"))
    if previous_inventory is None:
      previous_inventory = _coerce_float(entry.get("inventoryBaselineLiters"))
    if payload.lastRestockDate is not None:
      entry["lastRestockDate"] = payload.lastRestockDate.isoformat()
    if payload.nextRestockDate is not None:
      entry["nextRestockDate"] = payload.nextRestockDate.isoformat()
    if payload.tankCapacityLiters is not None:
      entry["tankCapacityLiters"] = float(payload.tankCapacityLiters)
    if payload.currentInventoryLiters is not None:
      entry["currentInventoryLiters"] = float(payload.currentInventoryLiters)
      entry["inventoryBaselineLiters"] = float(payload.currentInventoryLiters)
      entry["inventoryBaselineAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    overrides[normalized_id] = entry
    state["gasolineOverrides"] = overrides

    if payload.currentInventoryLiters is not None and previous_inventory is not None:
      delta_liters = float(payload.currentInventoryLiters) - previous_inventory
      if delta_liters >= sales_data.GALLON_TO_LITER:
        finance = state.setdefault("finance", {})
        costs = finance.setdefault("costs", {}).setdefault("entries", [])
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        fuel_label = fuel_types.normalize_fuel_label(normalized_id, fallback_key=normalized_id)
        costs.append({
          "id": f"refill-{normalized_id}-{uuid.uuid4().hex}",
          "name": f"Refill {fuel_label}",
          "amount": 0.0,
          "type": "one_time",
          "dueDate": date.today().isoformat(),
          "notes": "Auto-added from inventory refill.",
          "reminder": {"enabled": False, "daysBefore": 0, "syncToCalendar": False, "calendarEventId": None},
          "createdAt": now_iso,
          "updatedAt": now_iso,
          "source": "refill",
          "metadata": {
            "fuelType": normalized_id,
            "quantityLiters": round(delta_liters, 3),
            "quantityGallons": round(delta_liters / sales_data.GALLON_TO_LITER, 3),
          }
        })
        finance["updatedAt"] = now_iso

  update_state(_mutator)
  return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/fuel-types", response_model=GasolineStatsResponse, status_code=status.HTTP_201_CREATED)
def add_fuel_type(payload: FuelTypeCreate) -> GasolineStatsResponse:
  try:
    fuel_types.ensure_custom_fuel_type(payload.model_dump())
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  return build_gasoline_stats(with_side_effects=False)


def _send_email_notification(notification: RestockNotification, recipient: str, days_until: int, tag: str) -> None:
  if not credentials_available():
    return

  key = (notification.fuelTypeId, notification.recommendedRestockDate.isoformat(), tag)
  if key in _SENT_EMAIL_KEYS:
    return

  if days_until <= 0:
    intro = f"Recordatorio que hoy debes renovar el producto {notification.fuelTypeLabel}."
  elif days_until == 1:
    intro = f"Recordatorio que en 1 día tienes que renovar el producto {notification.fuelTypeLabel}."
  else:
    intro = f"Recordatorio que en {days_until} días tienes que renovar el producto {notification.fuelTypeLabel}."

  subject = f"Recordatorio de reabastecimiento - {notification.fuelTypeLabel}"
  body_lines = [
    intro,
    f"Fecha recomendada de reabastecimiento: {notification.recommendedRestockDate:%d/%m/%Y}.",
    "",
    notification.message,
    "",
    "Por favor no contestar a este correo automático."
  ]
  try:
    send_email(
      recipients=recipient,
      subject=subject,
      body="\n".join(body_lines),
    )
  except EmailDeliveryError as exc:  # pragma: no cover - logging side effects
    print(f"[EMAIL_ERROR] Failed to send notification: {exc}")
  else:
    _SENT_EMAIL_KEYS.add(key)
