from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, HTTPException, status
import threading
import time

from app.services import sales_data, fuel_types
from app.services.state import update_state

router = APIRouter(prefix="/api/import", tags=["import"])


EXCEL_PATH = sales_data._DEFAULT_EXCEL_PATH  # pylint: disable=protected-access
GALLON_TO_LITER = 3.78541
HAVE_PANDAS = True
try:
  import pandas as pd  # type: ignore
except Exception:
  HAVE_PANDAS = False


def _parse_excel() -> Dict[str, Dict[str, float]]:
  if not HAVE_PANDAS:
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pandas no está disponible en el backend.")
  data: Dict[str, Dict[str, float]] = {"prices": {}, "sales_liters": {}}
  discovered: Dict[str, str] = {}

  if not EXCEL_PATH.exists():
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fuente de datos no encontrada.")

  if EXCEL_PATH.is_dir():
    sources = sorted(
      path
      for path in EXCEL_PATH.iterdir()
      if path.is_file() and path.suffix.lower() in {".xlsx", ".xls"}
    )
  else:
    sources = [EXCEL_PATH]

  if not sources:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se encontraron archivos Excel para importar.")

  for source in sources:
    xl = pd.ExcelFile(source)
    if "Precios" in xl.sheet_names:
      prices_df = xl.parse("Precios")
      for _, row in prices_df.iterrows():
        raw_fuel = row.get("Tipo")
        fuel = fuel_types.normalize_fuel_key(raw_fuel)
        if not fuel:
          continue
        label = fuel_types.normalize_fuel_label(raw_fuel, fallback_key=fuel)
        try:
          price_gal = float(row.get("PG (Precio/galón)", 0))
        except (TypeError, ValueError):
          continue
        data["prices"][fuel] = price_gal
        if label:
          discovered.setdefault(fuel, label)

    if "Ventas" in xl.sheet_names:
      ventas_df = xl.parse("Ventas")
      grouped = ventas_df.groupby("Tipo")["Galones"].sum()
      for fuel_raw, galones in grouped.items():
        fuel_id = fuel_types.normalize_fuel_key(fuel_raw)
        if not fuel_id:
          continue
        data["sales_liters"][fuel_id] = data["sales_liters"].get(fuel_id, 0.0) + float(galones) * GALLON_TO_LITER
        label = fuel_types.normalize_fuel_label(fuel_raw, fallback_key=fuel_id)
        if label:
          discovered.setdefault(fuel_id, label)

  if discovered:
    fuel_types.record_discovered_fuels([(key, label) for key, label in discovered.items()], source="import")

  return data


@router.post("/excel", status_code=status.HTTP_200_OK)
def import_from_excel() -> Dict[str, Dict[str, float]]:
  parsed = _parse_excel()

  def _mutator(state: Dict) -> None:
    finance = state.setdefault("finance", {})
    earnings = finance.setdefault("earnings", {})
    fuel = earnings.setdefault("fuel", {})
    overrides = state.setdefault("gasolineOverrides", {})

    for fuel_id, price_per_gallon in parsed["prices"].items():
      entry = fuel.setdefault(fuel_id, {"pricePerGallon": 0.0, "litersSold": 0.0, "mode": "manual"})
      entry["pricePerGallon"] = price_per_gallon
      entry["mode"] = "manual"
      overrides.setdefault(fuel_id, {})["pricePerGallon"] = price_per_gallon

    for fuel_id, liters_sold in parsed["sales_liters"].items():
      entry = fuel.setdefault(fuel_id, {"pricePerGallon": 0.0, "litersSold": 0.0, "mode": "manual"})
      entry["litersSold"] = liters_sold
      entry["mode"] = "manual"

    finance["earnings"] = earnings
    state["gasolineOverrides"] = overrides

  update_state(_mutator)
  return parsed


_refresh_thread_started = False


def _refresh_loop(interval_seconds: int = 300) -> None:
  while True:
    try:
      import_from_excel()
    except Exception as exc:  # pragma: no cover - background safety
      print(f"[IMPORT_EXCEL_ERROR] {exc}")
    time.sleep(interval_seconds)


def start_excel_refresh(interval_seconds: int = 300) -> None:
  global _refresh_thread_started
  if _refresh_thread_started or not HAVE_PANDAS:
    return
  _refresh_thread_started = True
  thread = threading.Thread(target=_refresh_loop, args=(interval_seconds,), daemon=True)
  thread.start()
