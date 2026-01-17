from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.gasoline import build_gasoline_stats
from app.services import sales_data
from app.services.state import load_state

router = APIRouter(prefix="/api/predictions", tags=["predictions"])

_HEATMAP_CACHE: Dict[str, Any] = {
  "fingerprint": None,
  "heatmap": [],
  "samples": 0,
  "total": 0,
}
_PREDICTIONS_CACHE: Dict[str, Any] = {"key": None, "response": None}


def _build_predictions_cache_key(fingerprint: str | None, state: Dict[str, Any]) -> str:
  payload = {
    "fingerprint": fingerprint,
    "overrides": state.get("gasolineOverrides", {}),
    "custom": state.get("customFuelTypes", []),
    "discovered": state.get("discoveredFuelTypes", []),
    "financeUpdatedAt": state.get("finance", {}).get("updatedAt"),
  }
  raw = json.dumps(payload, sort_keys=True, default=str)
  return hashlib.sha256(raw.encode()).hexdigest()


class MonthlyForecast(BaseModel):
  month: str
  predictedRevenue: float
  predictedRevenueLow: float
  predictedRevenueHigh: float
  momDeltaPct: float | None = None
  predictedByFuelType: Dict[str, float]
  confidence: Literal["low", "medium", "high"]
  basisMonths: list["BasisMonth"]
  drivers: list["ForecastDriver"]


class BasisMonth(BaseModel):
  month: str
  revenue: float


class ForecastDriver(BaseModel):
  label: str
  value: str


class PeakHourPrediction(BaseModel):
  hour: int
  label: str
  transactions: int
  gallons: float
  liters: float


class PeakHours(BaseModel):
  available: bool
  note: str | None = None
  topHours: list[PeakHourPrediction]
  peakDayOfWeek: str | None = None
  tomorrowHours: list[PeakHourPrediction] | None = None
  heatmap: list["PeakHeatmapCell"] = []
  sampleSize: int = 0
  confidenceNote: str | None = None


class PeakHeatmapCell(BaseModel):
  weekday: int
  weekdayLabel: str
  hour: int
  transactions: int
  liters: float


class StockoutPrediction(BaseModel):
  fuelType: str
  label: str
  recommendedReorderDate: date
  reorderByDate: date
  estimatedEmptyDate: date
  estimatedEmptyRange: Dict[str, date] | None = None
  risk: Literal["green", "yellow", "red"]
  daysRemaining: int
  confidence: float


class YearlyEarnings(BaseModel):
  year: int
  actualGross: float
  actualNet: float
  actualCosts: float
  predictedGross: float
  predictedNet: float
  predictedCosts: float
  monthsCovered: int
  monthsRemaining: int
  method: str


class PredictionsResponse(BaseModel):
  generatedAt: datetime
  monthlyForecast: MonthlyForecast
  peakHours: PeakHours
  stockout: list[StockoutPrediction]
  yearlyEarnings: YearlyEarnings


def _format_hour_label(hour: int) -> str:
  start = f"{hour:02d}:00"
  end_hour = (hour + 1) % 24
  end = f"{end_hour:02d}:00"
  return f"{start} - {end}"


def _compute_peak_day(transactions: list[dict]) -> str | None:
  day_totals: Dict[int, float] = {}
  for tx in transactions:
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    weekday = tx_date.weekday()
    day_totals[weekday] = day_totals.get(weekday, 0.0) + float(tx.get("liters", 0.0))
  if not day_totals:
    return None
  peak_day = max(day_totals.items(), key=lambda item: item[1])[0]
  return calendar.day_name[peak_day]


def _compute_heatmap(transactions: list[dict]) -> tuple[list[PeakHeatmapCell], int, int]:
  buckets: Dict[tuple[int, int], PeakHeatmapCell] = {}
  total = len(transactions)
  samples = 0
  for tx in transactions:
    hour = tx.get("hour")
    if hour is None:
      continue
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    weekday = tx_date.weekday()
    key = (weekday, int(hour))
    cell = buckets.get(key)
    if cell is None:
      cell = PeakHeatmapCell(
        weekday=weekday,
        weekdayLabel=calendar.day_name[weekday],
        hour=int(hour),
        transactions=0,
        liters=0.0,
      )
      buckets[key] = cell
    cell.transactions += 1
    cell.liters += float(tx.get("liters", 0.0))
    samples += 1
  return list(buckets.values()), samples, total


def _cached_heatmap(transactions: list[dict], fingerprint: str | None) -> tuple[list[PeakHeatmapCell], int, int]:
  global _HEATMAP_CACHE
  if fingerprint and _HEATMAP_CACHE.get("fingerprint") == fingerprint:
    return (
      _HEATMAP_CACHE.get("heatmap", []),
      _HEATMAP_CACHE.get("samples", 0),
      _HEATMAP_CACHE.get("total", 0),
    )
  heatmap, samples, total = _compute_heatmap(transactions)
  if fingerprint:
    _HEATMAP_CACHE = {
      "fingerprint": fingerprint,
      "heatmap": heatmap,
      "samples": samples,
      "total": total,
    }
  return heatmap, samples, total


def _build_forecast_drivers(forecast_payload: dict) -> list[ForecastDriver]:
  drivers: list[ForecastDriver] = []
  trend_value = float(forecast_payload.get("trend_value", 0.0))
  drivers.append(ForecastDriver(
    label="Tendencia últimos meses",
    value=f"{trend_value:+.2f}",
  ))

  basis_months = forecast_payload.get("basis_months", [])
  if len(basis_months) >= 2:
    last_month = basis_months[-1]["month"]
    prev_month = basis_months[-2]["month"]
    last = sales_data.monthly_earnings(last_month)
    prev = sales_data.monthly_earnings(prev_month)
    last_total = float(last.get("total_revenue", 0.0))
    prev_total = float(prev.get("total_revenue", 0.0))
    mix_shift = 0.0
    if last_total > 0 and prev_total > 0:
      fuel_keys = set(last.get("revenue_by_fuel_type", {}).keys()) | set(prev.get("revenue_by_fuel_type", {}).keys())
      last_rev = last.get("revenue_by_fuel_type", {})
      prev_rev = prev.get("revenue_by_fuel_type", {})
      for fuel in fuel_keys:
        last_share = float(last_rev.get(fuel, 0.0)) / last_total
        prev_share = float(prev_rev.get(fuel, 0.0)) / prev_total
        mix_shift += abs(last_share - prev_share)
      mix_shift = (mix_shift / 2) * 100
    drivers.append(ForecastDriver(
      label="Cambio de mezcla de combustibles",
      value=f"{mix_shift:.1f} pp",
    ))

    last_gallons = float(last.get("total_gallons_sold", 0.0))
    prev_gallons = float(prev.get("total_gallons_sold", 0.0))
    last_avg_pg = (last_total / last_gallons) if last_gallons > 0 else 0.0
    prev_avg_pg = (prev_total / prev_gallons) if prev_gallons > 0 else 0.0
    delta_pg = last_avg_pg - prev_avg_pg
    drivers.append(ForecastDriver(
      label="Cambio en precio promedio/galón",
      value=f"{delta_pg:+.2f}",
    ))

  return drivers


@router.get("", response_model=PredictionsResponse)
def get_predictions() -> PredictionsResponse:
  state = load_state()
  sales_state = sales_data.load_sales_state()
  fingerprint = sales_state.get("sourceFingerprint")
  cache_key = _build_predictions_cache_key(fingerprint, state)
  cached = _PREDICTIONS_CACHE.get("response") if _PREDICTIONS_CACHE.get("key") == cache_key else None
  if cached is not None:
    return cached

  forecast_payload = sales_data.forecast_next_month()
  drivers = _build_forecast_drivers(forecast_payload)
  forecast = MonthlyForecast(
    month=forecast_payload["month"],
    predictedRevenue=forecast_payload["predicted_revenue"],
    predictedRevenueLow=forecast_payload.get("predicted_revenue_low", forecast_payload["predicted_revenue"]),
    predictedRevenueHigh=forecast_payload.get("predicted_revenue_high", forecast_payload["predicted_revenue"]),
    momDeltaPct=forecast_payload.get("mom_delta_pct"),
    predictedByFuelType=forecast_payload.get("predicted_by_fuel_type", {}),
    confidence=forecast_payload.get("confidence", "low"),  # type: ignore[arg-type]
    basisMonths=forecast_payload.get("basis_months", []),
    drivers=drivers,
  )

  sales_summary = sales_data.compute_sales_summary()
  has_hour_data = bool(sales_summary.get("has_hour_data"))
  peak_note = None
  top_hours: list[PeakHourPrediction] = []
  peak_day = None

  transactions = sales_state.get("transactions", [])
  heatmap_cells, hour_samples, total_samples = _cached_heatmap(transactions, fingerprint)
  coverage = (hour_samples / total_samples) if total_samples else 0.0
  confidence_note = None

  if has_hour_data:
    for entry in sales_data.peak_hours():
      top_hours.append(PeakHourPrediction(
        hour=entry["hour"],
        label=_format_hour_label(int(entry["hour"])),
        transactions=entry.get("transactions", 0),
        gallons=entry.get("gallons", 0.0),
        liters=entry.get("liters", 0.0),
      ))
    peak_day = _compute_peak_day(transactions)
    if hour_samples < 100:
      confidence_note = "Muestra limitada para horas pico; interpreta con cautela."
    elif coverage < 0.7:
      confidence_note = "Cobertura parcial de horas; algunas ventas no incluyen Hora."
  else:
    peak_note = "La predicción requiere la columna Hora en el Excel."

  peak_hours = PeakHours(
    available=has_hour_data,
    note=peak_note,
    topHours=top_hours,
    peakDayOfWeek=peak_day,
    tomorrowHours=None,
    heatmap=heatmap_cells,
    sampleSize=hour_samples,
    confidenceNote=confidence_note,
  )

  stats = build_gasoline_stats(with_side_effects=False)
  today = sales_data.effective_today(transactions)
  if has_hour_data:
    tomorrow = today + timedelta(days=1)
    tomorrow_ranked = sales_data.peak_hours_for_weekday(tomorrow.weekday())
    if tomorrow_ranked:
      peak_hours.tomorrowHours = [
        PeakHourPrediction(
          hour=entry["hour"],
          label=_format_hour_label(int(entry["hour"])),
          transactions=entry.get("transactions", 0),
          gallons=entry.get("gallons", 0.0),
          liters=entry.get("liters", 0.0),
        )
        for entry in tomorrow_ranked
      ]
    else:
      # Fallback to general peak hours if we lack weekday-specific history.
      peak_hours.tomorrowHours = top_hours

  stockout: list[StockoutPrediction] = []
  for fuel in stats.fuelTypes:
    if fuel.needsConfiguration:
      continue
    estimated_range = fuel.estimatedDepletionRange
    projected_date = estimated_range.minDate if estimated_range else fuel.estimatedDepletionDate
    if projected_date is None:
      continue
    reorder_buffer = timedelta(days=2)
    recommended_date = projected_date - reorder_buffer
    if recommended_date < today:
      recommended_date = today
    days_remaining = max((projected_date - today).days, 0)
    if days_remaining <= 2:
      risk = "red"
    elif days_remaining <= 5:
      risk = "yellow"
    else:
      risk = "green"

    range_payload = None
    if estimated_range:
      range_payload = {"minDate": estimated_range.minDate, "maxDate": estimated_range.maxDate}

    reorder_by_date = recommended_date
    if isinstance(fuel.depletion, dict):
      candidate = fuel.depletion.get("reorder_by_date")
      if isinstance(candidate, date):
        reorder_by_date = candidate
    stockout.append(StockoutPrediction(
      fuelType=fuel.id,
      label=fuel.label,
      recommendedReorderDate=reorder_by_date,
      reorderByDate=reorder_by_date,
      estimatedEmptyDate=projected_date,
      estimatedEmptyRange=range_payload,
      risk=risk,
      daysRemaining=days_remaining,
      confidence=fuel.restock.confidenceScore if fuel.restock else 0.0,
    ))

  year = today.year
  month_key = f"{year}-{today.month:02d}"
  year_start = f"{year}-01"
  year_months = sales_data.monthly_earnings_range(year_start, month_key)

  def _monthly_revenue(entry: dict) -> float:
    raw_value = entry.get("total_revenue_raw")
    if isinstance(raw_value, (int, float)):
      return float(raw_value)
    return float(entry.get("total_revenue", 0.0))

  actual_gross = sum(_monthly_revenue(item) for item in year_months)
  recent_months = year_months[-3:] if len(year_months) >= 3 else year_months
  avg_recent = (sum(_monthly_revenue(item) for item in recent_months) / len(recent_months)) if recent_months else 0.0
  months_remaining = max(12 - today.month, 0)
  predicted_gross = actual_gross + (avg_recent * months_remaining)

  costs = state.get("finance", {}).get("costs", {}).get("entries", [])
  actual_costs = 0.0
  for entry in costs:
    due_raw = entry.get("dueDate")
    if not due_raw:
      continue
    try:
      due_date = date.fromisoformat(str(due_raw))
    except Exception:
      continue
    if due_date.year != year or due_date > today:
      continue
    try:
      amount = float(entry.get("amount", 0.0))
    except (TypeError, ValueError):
      amount = 0.0
    if amount:
      actual_costs += amount

  months_covered = max(today.month, 1)
  avg_cost = actual_costs / months_covered if months_covered > 0 else 0.0
  predicted_costs = avg_cost * 12
  actual_net = actual_gross - actual_costs
  predicted_net = predicted_gross - predicted_costs
  yearly_earnings = YearlyEarnings(
    year=year,
    actualGross=round(actual_gross, 4),
    actualNet=round(actual_net, 4),
    actualCosts=round(actual_costs, 4),
    predictedGross=round(predicted_gross, 4),
    predictedNet=round(predicted_net, 4),
    predictedCosts=round(predicted_costs, 4),
    monthsCovered=months_covered,
    monthsRemaining=months_remaining,
    method="avg_last_3_months",
  )

  response = PredictionsResponse(
    generatedAt=datetime.now(timezone.utc),
    monthlyForecast=forecast,
    peakHours=peak_hours,
    stockout=stockout,
    yearlyEarnings=yearly_earnings,
  )
  _PREDICTIONS_CACHE.update({"key": cache_key, "response": response})
  return response
