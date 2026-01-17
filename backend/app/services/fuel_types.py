from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.state import load_state, update_state


FuelTypeRecord = Dict[str, Any]


_DEFAULT_FUEL_PRESETS: Dict[str, Dict[str, Any]] = {
  "regular": {
    "label": "Regular",
    "color": "#FACC15",
    "tank_capacity": 42000,
    "current_inventory": 24800,
    "last_restock_days_ago": 5,
    "price_per_gallon": 5.26,
    "lead_time_days": 2,
    "consumption_history": [
      3050, 3120, 2980, 3210, 3340, 3275, 3185, 3100, 2995, 2940,
      2880, 3010, 3075, 3150, 3205, 3300, 3240, 3180, 3090, 3015,
      2970, 3055, 3125, 3190, 3255, 3310, 3385, 3295, 3220, 3160
    ],
  },
  "super": {
    "label": "Super",
    "color": "#F87171",
    "tank_capacity": 36000,
    "current_inventory": 16200,
    "last_restock_days_ago": 7,
    "price_per_gallon": 5.89,
    "lead_time_days": 3,
    "consumption_history": [
      2120, 2250, 2195, 2080, 2160, 2235, 2290, 2350, 2280, 2200,
      2140, 2095, 2025, 1980, 2050, 2105, 2165, 2210, 2275, 2320,
      2250, 2190, 2135, 2085, 2040, 1995, 2060, 2125, 2180, 2230
    ],
  },
  "diesel": {
    "label": "Diesel",
    "color": "#34D399",
    "tank_capacity": 48000,
    "current_inventory": 36500,
    "last_restock_days_ago": 3,
    "price_per_gallon": 4.87,
    "lead_time_days": 4,
    "consumption_history": [
      4180, 4300, 4400, 4250, 4380, 4525, 4475, 4390, 4335, 4285,
      4205, 4150, 4050, 3985, 4075, 4155, 4220, 4310, 4395, 4465,
      4380, 4290, 4215, 4140, 4085, 4025, 3950, 3880, 3975, 4055
    ],
  },
}


def _utc_now_iso() -> str:
  return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_fuel_key(raw: Any) -> Optional[str]:
  if raw is None:
    return None
  text = str(raw).strip()
  if not text:
    return None
  normalized = unicodedata.normalize("NFKD", text)
  normalized = "".join(char for char in normalized if not unicodedata.combining(char))
  normalized = normalized.lower()
  slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
  return slug or None


def normalize_fuel_label(raw: Any, *, fallback_key: Optional[str] = None) -> Optional[str]:
  if raw is None:
    if fallback_key:
      return fallback_key.replace("-", " ").title()
    return None
  text = str(raw).strip()
  if not text and fallback_key:
    return fallback_key.replace("-", " ").title()
  return re.sub(r"\s+", " ", text).title()


def record_discovered_fuels(fuels: Iterable[Tuple[str, str | None]], *, source: str = "sales") -> None:
  discoveries = [(fuel_id, label) for fuel_id, label in fuels if fuel_id]
  if not discoveries:
    return

  def _mutator(state: Dict[str, Any]) -> None:
    discovered: List[FuelTypeRecord] = state.setdefault("discoveredFuelTypes", [])
    existing_map = {entry.get("id"): entry for entry in discovered if isinstance(entry, dict)}
    now_iso = _utc_now_iso()

    for fuel_id, label in discoveries:
      entry = existing_map.get(fuel_id)
      if entry is None:
        entry = {
          "id": fuel_id,
          "label": label or fuel_id.replace("-", " ").title(),
          "sources": [source],
          "discoveredAt": now_iso,
          "lastSeenAt": now_iso,
        }
        discovered.append(entry)
        existing_map[fuel_id] = entry
      else:
        entry["lastSeenAt"] = now_iso
        sources = entry.get("sources") if isinstance(entry.get("sources"), list) else []
        if source not in sources:
          sources.append(source)
        entry["sources"] = sources
        if label and label != entry.get("label"):
          entry["label"] = label

    state["discoveredFuelTypes"] = discovered

  update_state(_mutator)


def get_fuel_presets() -> Dict[str, Dict[str, Any]]:
  return {key: dict(value) for key, value in _DEFAULT_FUEL_PRESETS.items()}


def get_fuel_types(state: Dict[str, Any] | None = None) -> List[FuelTypeRecord]:
  state = state or load_state()
  discovered = state.get("discoveredFuelTypes") or []
  custom = state.get("customFuelTypes") or []
  merged: Dict[str, FuelTypeRecord] = {}

  for entry in discovered:
    if not isinstance(entry, dict):
      continue
    fuel_id = normalize_fuel_key(entry.get("id"))
    if not fuel_id:
      continue
    merged[fuel_id] = {
      "id": fuel_id,
      "label": normalize_fuel_label(entry.get("label"), fallback_key=fuel_id),
      "source": "discovered",
    }

  for entry in custom:
    if not isinstance(entry, dict):
      continue
    fuel_id = normalize_fuel_key(entry.get("id"))
    if not fuel_id:
      continue
    merged[fuel_id] = {
      "id": fuel_id,
      "label": normalize_fuel_label(entry.get("label"), fallback_key=fuel_id),
      "source": "custom",
      "color": entry.get("color"),
      "tank_capacity": entry.get("tank_capacity", entry.get("tankCapacityLiters")),
      "current_inventory": entry.get("current_inventory", entry.get("currentInventoryLiters")),
      "lead_time_days": entry.get("lead_time_days", entry.get("leadTimeDays")),
      "price_per_gallon": entry.get("price_per_gallon", entry.get("pricePerGallon")),
      "consumption_history": entry.get("consumption_history"),
      "last_restock_days_ago": entry.get("last_restock_days_ago", entry.get("lastRestockDaysAgo")),
    }

  presets = get_fuel_presets()
  for fuel_id, preset in presets.items():
    if fuel_id not in merged:
      continue
    merged[fuel_id] = {**preset, **merged[fuel_id], "id": fuel_id}

  return sorted(merged.values(), key=lambda item: item.get("id", ""))


def ensure_custom_fuel_type(payload: Dict[str, Any]) -> None:
  fuel_id = normalize_fuel_key(payload.get("id"))
  if not fuel_id:
    raise ValueError("Fuel type id is required.")
  label = normalize_fuel_label(payload.get("label"), fallback_key=fuel_id)
  now_iso = _utc_now_iso()

  def _mutator(state: Dict[str, Any]) -> None:
    custom = state.setdefault("customFuelTypes", [])
    for entry in custom:
      if str(entry.get("id", "")).lower() == fuel_id:
        raise ValueError("Fuel type already exists.")
    custom.append({
      "id": fuel_id,
      "label": label or fuel_id.replace("-", " ").title(),
      "color": payload.get("color", "#60A5FA"),
      "tank_capacity": payload.get("tank_capacity", payload.get("tankCapacityLiters", 30000)),
      "current_inventory": payload.get("current_inventory", payload.get("currentInventoryLiters", 0)),
      "price_per_gallon": payload.get("price_per_gallon", payload.get("pricePerGallon", 0)),
      "lead_time_days": payload.get("lead_time_days", payload.get("leadTimeDays", 2)),
      "last_restock_days_ago": payload.get("last_restock_days_ago", 3),
      "consumption_history": payload.get("consumption_history") or [0] * 30,
      "createdAt": now_iso,
      "updatedAt": now_iso,
    })
    state["customFuelTypes"] = custom

  update_state(_mutator)


def validate_fuel_id(fuel_id: str, state: Dict[str, Any] | None = None) -> str:
  normalized = normalize_fuel_key(fuel_id)
  if not normalized:
    raise ValueError("Fuel type is required.")
  state = state or load_state()
  ids = {entry["id"] for entry in get_fuel_types(state)}
  if normalized not in ids:
    raise ValueError("Fuel type not recognized.")
  return normalized
