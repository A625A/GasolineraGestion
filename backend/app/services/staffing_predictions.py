from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple

from app.services import sales_data
from app.services.state import load_state


_WEEKDAY_INDEX = {
  "monday": 0,
  "tuesday": 1,
  "wednesday": 2,
  "thursday": 3,
  "friday": 4,
  "saturday": 5,
  "sunday": 6,
}


@dataclass
class DemandProfile:
  hourly_liters: Dict[Tuple[int, int], float]
  has_hour_data: bool


def _parse_time(value: str, *, default: time) -> time:
  try:
    return datetime.strptime(value, "%H:%M").time()
  except Exception:
    return default


def _build_demand_profile() -> DemandProfile:
  state = sales_data.load_sales_state()
  hourly: Dict[Tuple[int, int], float] = {}
  has_hour = False
  for tx in state.get("transactions", []):
    hour = tx.get("hour")
    if hour is None:
      continue
    has_hour = True
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    weekday = tx_date.weekday()
    key = (weekday, int(hour))
    hourly[key] = hourly.get(key, 0.0) + float(tx.get("liters", 0.0))
  return DemandProfile(hourly_liters=hourly, has_hour_data=has_hour)


def _percentiles(values: List[float]) -> tuple[float, float]:
  if not values:
    return 0.0, 0.0
  ordered = sorted(values)
  p50 = ordered[int(0.5 * (len(ordered) - 1))]
  p75 = ordered[int(0.75 * (len(ordered) - 1))]
  return p50, p75


def _resolve_worker_schedule(worker: Dict[str, Any], day_name: str) -> tuple[time, time] | None:
  schedule = worker.get("workSchedule") or {}
  days = schedule.get("daysOfWeek") or []
  if day_name not in days:
    return None
  day_overrides = schedule.get("dayOverrides") or {}
  override = day_overrides.get(day_name) if isinstance(day_overrides, dict) else None
  start_value = override.get("startTime") if isinstance(override, dict) else schedule.get("startTime", "08:00")
  end_value = override.get("endTime") if isinstance(override, dict) else schedule.get("endTime", "16:00")
  start_time = _parse_time(str(start_value), default=time(hour=8))
  end_time = _parse_time(str(end_value), default=time(hour=16))
  if end_time <= start_time:
    return None
  return start_time, end_time


def build_staffing_recommendations(days: int = 7, *, start: date | None = None) -> Dict[str, Any]:
  state = load_state()
  workers: List[Dict[str, Any]] = state.get("staffing", {}).get("workers", [])
  today = start or date.today()
  demand = _build_demand_profile()

  demand_values = list(demand.hourly_liters.values())
  p50, p75 = _percentiles(demand_values)

  schedule: List[Dict[str, Any]] = []
  warnings: List[str] = []
  total_required = 0
  total_assigned = 0

  for offset in range(days):
    day = today + timedelta(days=offset)
    weekday = day.weekday()
    day_name = next((name for name, index in _WEEKDAY_INDEX.items() if index == weekday), "monday")

    day_workers: List[Dict[str, Any]] = []
    earliest = None
    latest = None
    for worker in workers:
      schedule_window = _resolve_worker_schedule(worker, day_name)
      if not schedule_window:
        continue
      start_time, end_time = schedule_window
      day_workers.append({"worker": worker, "start": start_time, "end": end_time})
      earliest = start_time if earliest is None or start_time < earliest else earliest
      latest = end_time if latest is None or end_time > latest else latest

    if earliest is None or latest is None:
      schedule.append({
        "date": day.isoformat(),
        "weekday": day_name,
        "blocks": [],
      })
      warnings.append(f"{day.isoformat()}: Sin personal disponible para asignar.")
      continue

    start_hour = earliest.hour
    end_hour = latest.hour
    blocks: List[Dict[str, Any]] = []

    for hour in range(start_hour, end_hour):
      block_start = time(hour=hour)
      block_end = time(hour=hour + 1)
      demand_key = (weekday, hour)
      liters = demand.hourly_liters.get(demand_key, 0.0)

      if not demand.has_hour_data:
        required = 1
        demand_level = "low"
      elif liters >= p75:
        required = 3
        demand_level = "high"
      elif liters >= p50:
        required = 2
        demand_level = "medium"
      else:
        required = 1
        demand_level = "low"

      available_workers = [
        item for item in day_workers
        if item["start"] <= block_start and item["end"] >= block_end
      ]
      available_workers.sort(key=lambda item: item["worker"].get("fullName", ""))
      assigned_workers = available_workers[:required]

      assignments = [
        {
          "workerId": item["worker"].get("id", ""),
          "fullName": item["worker"].get("fullName", ""),
          "role": item["worker"].get("role") or item["worker"].get("jobType", ""),
          "start": block_start.strftime("%H:%M"),
          "end": block_end.strftime("%H:%M"),
        }
        for item in assigned_workers
      ]

      assigned_count = len(assignments)
      total_required += required
      total_assigned += assigned_count
      if assigned_count < required:
        warnings.append(
          f"{day.isoformat()} {block_start.strftime('%H:%M')}: Falta cobertura ({assigned_count}/{required})."
        )

      blocks.append({
        "start": block_start.strftime("%H:%M"),
        "end": block_end.strftime("%H:%M"),
        "demandLevel": demand_level,
        "required": required,
        "assigned": assigned_count,
        "assignments": assignments,
      })

    schedule.append({
      "date": day.isoformat(),
      "weekday": day_name,
      "blocks": blocks,
    })

  coverage_score = (total_assigned / total_required) if total_required else 0.0

  return {
    "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    "window": {
      "from": today.isoformat(),
      "to": (today + timedelta(days=days - 1)).isoformat(),
    },
    "coverageScore": round(coverage_score, 3),
    "warnings": warnings[:10],
    "schedule": schedule,
    "hasHourData": demand.has_hour_data,
  }
