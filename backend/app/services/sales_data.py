from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict

from app.services import fuel_types

HAVE_PANDAS = True
try:
  import pandas as pd  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
  HAVE_PANDAS = False

GALLON_TO_LITER = 3.78541

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _BASE_DIR / "data"
_STATE_PATH = _DATA_DIR / "sales.json"


def _resolve_excel_path(raw_path: str) -> Path:
  candidate = Path(raw_path)
  if candidate.is_absolute():
    return candidate
  search_roots = [Path.cwd(), _BASE_DIR, _BASE_DIR.parent]
  for root in search_roots:
    resolved = (root / candidate).resolve()
    if resolved.exists():
      return resolved
  return candidate


_RAW_EXCEL_PATH = os.getenv("SALES_EXCEL_PATH")
_DEFAULT_EXCEL_PATH = (
  _resolve_excel_path(_RAW_EXCEL_PATH)
  if _RAW_EXCEL_PATH
  else _DATA_DIR / "gas_sales_3000_jan2025_to_today.xlsx"
)

_LOCK = threading.RLock()
_REFRESH_IN_PROGRESS = False
_CACHE_LOCK = threading.RLock()
_SALES_CACHE: Dict[str, Any] = {
  "fingerprint": None,
  "recent": {},
  "summary": {},
  "monthly": {},
  "months": None,
  "forecast": None,
  "peak": None,
  "weekday": {},
  "full_history_avg": None,
}
_STATE_CACHE: Dict[str, Any] = {"mtime": None, "state": None}


def _get_cache(fingerprint: str | None) -> Dict[str, Any]:
  global _SALES_CACHE
  with _CACHE_LOCK:
    if fingerprint and _SALES_CACHE.get("fingerprint") != fingerprint:
      _SALES_CACHE = {
        "fingerprint": fingerprint,
        "recent": {},
        "summary": {},
        "monthly": {},
        "months": None,
        "forecast": None,
        "peak": None,
        "weekday": {},
        "full_history_avg": None,
      }
    return _SALES_CACHE


class SalesTransaction(TypedDict, total=False):
  id: str
  date: str
  month_key: str
  fuel_type: str
  gallons: float
  liters: float
  price_per_gallon: float
  total_amount: float
  raw_total_amount: float | None
  hour: int | None
  created_at: str
  loaded_at: str
  source_file: str
  source_row: int
  needs_review: bool


class SalesState(TypedDict, total=False):
  ingestedAt: str | None
  source: str | None
  transactions: List[SalesTransaction]
  sourceFingerprint: str | None


class SalesValidationExample(TypedDict, total=False):
  row: int
  reason: str
  payload: Dict[str, Any]


class SalesIngestionReport(TypedDict, total=False):
  rows_read: int
  rows_inserted: int
  rows_skipped: int
  adjusted_rows: int
  needs_review: int
  validation_errors: Dict[str, Any]
  ingested_at: str
  source: str


class PeakHour(TypedDict, total=False):
  hour: int
  transactions: int
  gallons: float
  liters: float


class MonthlyEarnings(TypedDict, total=False):
  month: str
  total_gallons_sold: float
  total_liters_sold: float
  total_revenue: float
  total_revenue_raw: float
  revenue_by_fuel_type: Dict[str, float]
  raw_revenue_by_fuel_type: Dict[str, float]
  gallons_by_fuel_type: Dict[str, float]
  average_price_per_gallon_by_type: Dict[str, float]
  price_per_gallon_range_by_type: Dict[str, Dict[str, float]]
  transactions: int


@dataclass
class DailyFuelTotals:
  totals: Dict[date, Dict[str, float]]
  by_fuel: Dict[str, Dict[str, Any]]
  has_hour_data: bool


def _utc_now_iso() -> str:
  return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_store() -> None:
  _DATA_DIR.mkdir(parents=True, exist_ok=True)
  if not _STATE_PATH.exists():
    with _STATE_PATH.open("w", encoding="utf-8") as fh:
      json.dump({"ingestedAt": None, "source": None, "transactions": []}, fh, ensure_ascii=False, indent=2)


def _load_state_unlocked() -> SalesState:
  try:
    mtime = _STATE_PATH.stat().st_mtime
  except FileNotFoundError:
    return {"ingestedAt": None, "source": None, "transactions": []}
  cached_state = _STATE_CACHE.get("state")
  if cached_state is not None and _STATE_CACHE.get("mtime") == mtime:
    return cached_state
  with _STATE_PATH.open("r", encoding="utf-8") as fh:
    state = json.load(fh)
  _STATE_CACHE["mtime"] = mtime
  _STATE_CACHE["state"] = state
  return state


def _write_state_unlocked(state: SalesState) -> None:
  with _STATE_PATH.open("w", encoding="utf-8") as fh:
    json.dump(state, fh, ensure_ascii=False, indent=2)
  try:
    _STATE_CACHE["mtime"] = _STATE_PATH.stat().st_mtime
  except FileNotFoundError:
    _STATE_CACHE["mtime"] = None
  _STATE_CACHE["state"] = state


def _refresh_from_sources(sources: List[Path], fingerprint: str | None) -> None:
  global _REFRESH_IN_PROGRESS
  try:
    ingest_sales_from_excels(file_paths=sources, source=str(_DEFAULT_EXCEL_PATH), fingerprint=fingerprint)
  finally:
    _REFRESH_IN_PROGRESS = False


def _auto_refresh_from_default() -> None:
  """
  Always rebuild sales state from the default Excel when available.
  This keeps analytics aligned with the Excel source of truth and ignores stale JSON state.
  """
  sources = _resolve_excel_sources()
  if not sources:
    return
  _ensure_store()
  current_state = _load_state_unlocked()
  fingerprint = _compute_sources_fingerprint(sources)
  if fingerprint and current_state.get("sourceFingerprint") == fingerprint and current_state.get("transactions"):
    return
  global _REFRESH_IN_PROGRESS
  if _REFRESH_IN_PROGRESS:
    return
  _REFRESH_IN_PROGRESS = True
  thread = threading.Thread(
    target=_refresh_from_sources,
    args=(sources, fingerprint),
    daemon=True,
  )
  thread.start()


def load_sales_state() -> SalesState:
  with _LOCK:
    _ensure_store()
    current_state = _load_state_unlocked()
    try:
      _auto_refresh_from_default()
    except Exception:
      # If Excel cannot be read, fall back to last good JSON.
      pass
    updated_state = _load_state_unlocked()
    return updated_state if updated_state else current_state


def _save_sales_state(state: SalesState) -> None:
  with _LOCK:
    _ensure_store()
    _write_state_unlocked(state)


def _normalize_fuel_type(raw: Any) -> str | None:
  return fuel_types.normalize_fuel_key(raw)


def _parse_hour(raw: Any) -> int | None:
  if raw is None:
    return None
  try:
    if isinstance(raw, datetime):
      value = int(raw.hour)
      return value if 0 <= value <= 23 else None
    if isinstance(raw, time):
      value = int(raw.hour)
      return value if 0 <= value <= 23 else None
    if isinstance(raw, float):
      if 0 <= raw < 1:
        value = int(raw * 24)
        return value if 0 <= value <= 23 else None
      value = int(raw)
      return value if 0 <= value <= 23 else None
    if isinstance(raw, int):
      value = int(raw)
      return value if 0 <= value <= 23 else None
    text = str(raw).strip()
    if not text:
      return None
    lowered = text.lower().replace(".", "")
    lowered = lowered.replace(" a m", " am").replace(" p m", " pm")
    if ("/" in lowered or "-" in lowered) and ":" in lowered and " " in lowered:
      tokens = lowered.split()
      if tokens:
        if tokens[-1] in {"am", "pm"} and len(tokens) >= 2:
          lowered = f"{tokens[-2]} {tokens[-1]}"
        else:
          lowered = tokens[-1]
    if "am" in lowered or "pm" in lowered:
      for fmt in ("%I:%M %p", "%I:%M:%S %p"):
        try:
          parsed = datetime.strptime(lowered, fmt)
          return parsed.hour
        except Exception:
          continue
    if ":" in lowered:
      head = lowered.split(":", 1)[0]
      value = int(head)
    else:
      value = int(lowered)
    return value if 0 <= value <= 23 else None
  except (TypeError, ValueError):
    return None


def _parse_date(raw: Any) -> date | None:
  if raw is None:
    return None
  if isinstance(raw, datetime):
    return raw.date()
  if isinstance(raw, date):
    return raw
  text = str(raw).strip()
  if not text:
    return None
  if "T" in text:
    text = text.split("T", 1)[0].strip()
  if " " in text:
    text = text.split(" ", 1)[0].strip()
  try:
    return date.fromisoformat(text.replace("/", "-"))
  except ValueError:
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d"):
      try:
        return datetime.strptime(text, fmt).date()
      except Exception:
        continue
    return None


def _parse_datetime(raw: Any) -> datetime | None:
  if raw is None:
    return None
  if isinstance(raw, datetime):
    value = raw
  elif isinstance(raw, date):
    value = datetime.combine(raw, time.min)
  else:
    text = str(raw).strip()
    if not text:
      return None
    try:
      value = datetime.fromisoformat(text)
    except ValueError:
      parsed_date = _parse_date(text)
      if not parsed_date:
        return None
      value = datetime.combine(parsed_date, time.min)
  if value.tzinfo is not None:
    value = value.astimezone(timezone.utc).replace(tzinfo=None)
  return value


def _transaction_datetime(tx: SalesTransaction) -> datetime | None:
  try:
    tx_date = date.fromisoformat(tx.get("date", ""))
  except Exception:
    return None
  hour = tx.get("hour")
  if isinstance(hour, (int, float)) and 0 <= int(hour) <= 23:
    return datetime.combine(tx_date, time(hour=int(hour)))
  return datetime.combine(tx_date, time.min)


def _max_transaction_date(transactions: Iterable[SalesTransaction]) -> date | None:
  latest: date | None = None
  for tx in transactions:
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    if latest is None or tx_date > latest:
      latest = tx_date
  return latest


def effective_today(transactions: Optional[Iterable[SalesTransaction]] = None) -> date:
  if transactions is None:
    state = load_sales_state()
    transactions = state.get("transactions", [])
  latest = _max_transaction_date(transactions)
  return latest or date.today()


def full_history_daily_average(
  transactions: Optional[Iterable[SalesTransaction]] = None,
  *,
  fingerprint: str | None = None,
) -> Dict[str, float]:
  used_cache = False
  if transactions is None:
    state = load_sales_state()
    fingerprint = fingerprint or state.get("sourceFingerprint")
    cache = _get_cache(fingerprint)
    cached = cache.get("full_history_avg")
    if isinstance(cached, dict):
      return cached
    transactions = state.get("transactions", [])
    used_cache = True
  elif fingerprint:
    cache = _get_cache(fingerprint)
    cached = cache.get("full_history_avg")
    if isinstance(cached, dict):
      return cached

  totals: Dict[str, float] = {}
  min_dates: Dict[str, date] = {}
  max_dates: Dict[str, date] = {}

  for tx in transactions:
    fuel = tx.get("fuel_type")
    if not fuel:
      continue
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    liters = float(tx.get("liters", 0.0))
    if not math.isfinite(liters):
      liters = 0.0
    totals[fuel] = totals.get(fuel, 0.0) + liters
    if fuel not in min_dates or tx_date < min_dates[fuel]:
      min_dates[fuel] = tx_date
    if fuel not in max_dates or tx_date > max_dates[fuel]:
      max_dates[fuel] = tx_date

  averages: Dict[str, float] = {}
  for fuel, total in totals.items():
    start = min_dates.get(fuel)
    end = max_dates.get(fuel)
    if not start or not end:
      averages[fuel] = 0.0
      continue
    span_days = (end - start).days + 1
    averages[fuel] = (total / span_days) if span_days > 0 else 0.0

  if used_cache or fingerprint:
    try:
      if not fingerprint:
        state = load_sales_state()
        fingerprint = state.get("sourceFingerprint")
      cache = _get_cache(fingerprint)
      cache["full_history_avg"] = averages
    except Exception:
      pass

  return averages


def _percentile(values: List[float], percentile: float) -> float | None:
  if not values:
    return None
  ordered = sorted(values)
  k = (len(ordered) - 1) * percentile
  f = int(k)
  c = min(len(ordered) - 1, f + 1)
  if f == c:
    return ordered[f]
  return ordered[f] * (c - k) + ordered[c] * (k - f)


def _read_workbook(file_bytes: Optional[bytes], file_path: Optional[Path]) -> "pd.ExcelFile":
  if not HAVE_PANDAS:
    raise RuntimeError("pandas is required to parse Excel files.")
  if file_bytes is not None:
    buffer = BytesIO(file_bytes)
    return pd.ExcelFile(buffer)
  if file_path is None:
    raise FileNotFoundError("No Excel file provided.")
  if not file_path.exists():
    raise FileNotFoundError(str(file_path))
  return pd.ExcelFile(file_path)


def _list_excel_files(directory: Path) -> List[Path]:
  return sorted(
    path
    for path in directory.iterdir()
    if path.is_file() and path.suffix.lower() in {".xlsx", ".xls"}
  )


def _compute_sources_fingerprint(paths: List[Path]) -> str:
  parts: List[str] = []
  for path in sorted(paths, key=lambda item: item.name):
    try:
      stat = path.stat()
    except FileNotFoundError:
      continue
    parts.append(f"{path.name}:{int(stat.st_mtime)}:{stat.st_size}")
  digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
  return digest


def _resolve_excel_sources() -> List[Path]:
  if _DEFAULT_EXCEL_PATH.exists():
    if _DEFAULT_EXCEL_PATH.is_dir():
      return _list_excel_files(_DEFAULT_EXCEL_PATH)
    return [_DEFAULT_EXCEL_PATH]
  return []


def _load_price_map(workbook: "pd.ExcelFile") -> Dict[str, float]:
  price_map: Dict[str, float] = {}
  if "Precios" not in workbook.sheet_names:
    return price_map

  try:
    prices_df = workbook.parse("Precios")
  except Exception:
    return price_map

  for _, row in prices_df.iterrows():
    fuel = _normalize_fuel_type(row.get("Tipo"))
    if not fuel:
      continue
    raw_price = row.get("PG (Precio/galón)") if "PG (Precio/galón)" in row else row.get("PG")
    try:
      price_val = float(raw_price)
    except (TypeError, ValueError):
      continue
    if price_val > 0 and math.isfinite(price_val):
      price_map[fuel] = price_val
  return price_map


def _normalize_column_key(value: Any) -> str:
  return str(value).strip().lower()


def _build_column_map(columns: Iterable[Any]) -> Dict[str, Any]:
  return {_normalize_column_key(column): column for column in columns}


def _column_value(row: Any, column_map: Dict[str, Any], *candidates: str) -> Any:
  for candidate in candidates:
    key = column_map.get(_normalize_column_key(candidate))
    if key is not None:
      return row.get(key)
  return None


def _ingest_sales_workbook(
  workbook: "pd.ExcelFile",
  *,
  source_file: str,
  now_iso: str,
) -> Tuple[List[SalesTransaction], List[SalesValidationExample], int, int, int, Dict[str, str]]:
  price_map = _load_price_map(workbook)

  ventas_sheet = "Ventas" if "Ventas" in workbook.sheet_names else workbook.sheet_names[0]
  dataframe = workbook.parse(ventas_sheet)
  column_map = _build_column_map(dataframe.columns)
  rows_read = len(dataframe.index)
  inserted: List[SalesTransaction] = []
  errors: List[SalesValidationExample] = []
  adjusted_rows = 0
  needs_review = 0
  discovered: Dict[str, str] = {}

  for idx, row in dataframe.iterrows():
    raw_fuel = _column_value(row, column_map, "Tipo")
    fuel_type = _normalize_fuel_type(raw_fuel)
    fuel_label = fuel_types.normalize_fuel_label(raw_fuel, fallback_key=fuel_type)
    gallons = _column_value(row, column_map, "Galones", "Galón", "Galon", "Galones vendidos")
    price_per_gallon = _column_value(row, column_map, "PG (Precio/galón)", "PG", "Precio/galón", "Precio galon", "Precio por galon")
    total_amount = _column_value(row, column_map, "Gasto total", "Gasto_total", "Total", "Total venta", "Importe")
    day_value = _column_value(row, column_map, "Dia", "Día", "Fecha", "fecha", "Fecha venta", "Fecha_venta")
    hour = _parse_hour(_column_value(row, column_map, "Hora", "Hora venta", "Hora de venta"))
    if hour is None:
      hour = _parse_hour(day_value)
    source_row = int(idx) + 2

    parsed_date = _parse_date(day_value)
    if parsed_date is None:
      errors.append({"row": source_row, "reason": "Fecha inválida", "payload": {"Dia": day_value, "file": source_file}})
      continue

    if fuel_type is None:
      errors.append({"row": source_row, "reason": "Tipo de combustible inválido", "payload": {"Tipo": raw_fuel, "file": source_file}})
      continue

    try:
      gallons_value = float(gallons)
    except (TypeError, ValueError):
      gallons_value = -1.0

    if gallons_value <= 0 or not math.isfinite(gallons_value):
      errors.append({"row": source_row, "reason": "Galones deben ser > 0", "payload": {"Galones": gallons, "file": source_file}})
      continue

    try:
      price_value = float(price_per_gallon)
    except (TypeError, ValueError):
      price_value = -1.0

    if price_value <= 0 or not math.isfinite(price_value):
      # Try to derive from prices sheet first
      fallback_price = price_map.get(fuel_type)
      if fallback_price and fallback_price > 0:
        price_value = fallback_price
        adjusted_rows += 1
      else:
        # Try to derive from total_amount / gallons
        try:
          derived_total = float(total_amount)
        except Exception:
          derived_total = None
        if derived_total is not None and math.isfinite(derived_total) and gallons_value > 0:
          price_value = derived_total / gallons_value
          adjusted_rows += 1
        else:
          errors.append({"row": source_row, "reason": "Precio por galón inválido", "payload": {"PG": price_per_gallon, "file": source_file}})
          continue

    liters_value = gallons_value * GALLON_TO_LITER
    expected_total = gallons_value * price_value
    raw_total_amount: float | None
    needs_review_row = False

    try:
      if total_amount is None or (HAVE_PANDAS and isinstance(total_amount, float) and pd.isna(total_amount)):  # type: ignore[attr-defined]
        raw_total_amount = None
        total_value = expected_total
        adjusted_rows += 1
      else:
        total_value = float(total_amount)
        raw_total_amount = total_value
        tolerance = max(expected_total * 0.02, 0.05)
        if abs(total_value - expected_total) <= tolerance:
          total_value = expected_total
          adjusted_rows += 1
        elif abs(total_value - expected_total) > tolerance * 4:
          needs_review_row = True
          needs_review += 1
    except (TypeError, ValueError):
      raw_total_amount = None
      total_value = expected_total
      adjusted_rows += 1

    if not math.isfinite(total_value):
      total_value = expected_total

    transaction: SalesTransaction = {
      "id": str(uuid.uuid4()),
      "date": parsed_date.isoformat(),
      "month_key": parsed_date.strftime("%Y-%m"),
      "fuel_type": fuel_type,
      "gallons": round(gallons_value, 4),
      "liters": round(liters_value, 4),
      "price_per_gallon": round(price_value, 4),
      "total_amount": round(total_value, 4),
      "raw_total_amount": raw_total_amount,
      "hour": hour,
      "created_at": now_iso,
      "loaded_at": now_iso,
      "source_file": source_file,
      "source_row": source_row,
      "needs_review": needs_review_row,
    }
    inserted.append(transaction)
    if fuel_label:
      discovered.setdefault(fuel_type, fuel_label)

  return inserted, errors, rows_read, adjusted_rows, needs_review, discovered


def ingest_sales_from_excels(
  *,
  file_paths: List[Path],
  source: str | None = None,
  fingerprint: str | None = None,
) -> SalesIngestionReport:
  if not file_paths:
    raise FileNotFoundError("No Excel files found.")

  now_iso = _utc_now_iso()
  inserted: List[SalesTransaction] = []
  errors: List[SalesValidationExample] = []
  rows_read = 0
  adjusted_rows = 0
  needs_review = 0
  discovered: Dict[str, str] = {}

  for path in file_paths:
    workbook = _read_workbook(file_bytes=None, file_path=path)
    file_label = path.name
    file_inserted, file_errors, file_rows, file_adjusted, file_review, file_discovered = _ingest_sales_workbook(
      workbook,
      source_file=file_label,
      now_iso=now_iso,
    )
    inserted.extend(file_inserted)
    errors.extend(file_errors)
    rows_read += file_rows
    adjusted_rows += file_adjusted
    needs_review += file_review
    discovered.update(file_discovered)

  seen: set[tuple[str, int]] = set()
  unique_inserted: List[SalesTransaction] = []
  for tx in inserted:
    key = (tx.get("source_file", ""), int(tx.get("source_row", 0)))
    if key in seen:
      continue
    seen.add(key)
    unique_inserted.append(tx)
  inserted = unique_inserted
  if discovered:
    fuel_types.record_discovered_fuels([(key, label) for key, label in discovered.items()], source="sales")

  inserted.sort(key=lambda t: (t["date"], t.get("hour") or -1))
  state: SalesState = {
    "ingestedAt": now_iso,
    "source": source or str(_DEFAULT_EXCEL_PATH),
    "transactions": inserted,
    "sourceFingerprint": fingerprint,
  }
  _save_sales_state(state)

  report: SalesIngestionReport = {
    "rows_read": rows_read,
    "rows_inserted": len(inserted),
    "rows_skipped": rows_read - len(inserted),
    "adjusted_rows": adjusted_rows,
    "needs_review": needs_review,
    "validation_errors": {"count": len(errors), "examples": errors[:5]},
    "ingested_at": now_iso,
    "source": state["source"] or "",
  }
  return report


def ingest_sales_from_excel(
  *,
  file_bytes: Optional[bytes] = None,
  file_path: Optional[Path] = None,
  source: str | None = None,
  fingerprint: str | None = None,
) -> SalesIngestionReport:
  if file_path is not None and file_path.exists() and file_path.is_dir():
    return ingest_sales_from_excels(
      file_paths=_list_excel_files(file_path),
      source=source or str(file_path),
      fingerprint=fingerprint,
    )

  now_iso = _utc_now_iso()
  workbook = _read_workbook(file_bytes, file_path)
  source_file = source or (file_path.name if file_path else "uploaded.xlsx")
  inserted, errors, rows_read, adjusted_rows, needs_review, discovered = _ingest_sales_workbook(
    workbook,
    source_file=source_file,
    now_iso=now_iso,
  )

  seen: set[tuple[str, int]] = set()
  unique_inserted: List[SalesTransaction] = []
  for tx in inserted:
    key = (tx.get("source_file", ""), int(tx.get("source_row", 0)))
    if key in seen:
      continue
    seen.add(key)
    unique_inserted.append(tx)
  inserted = unique_inserted
  if discovered:
    fuel_types.record_discovered_fuels([(key, label) for key, label in discovered.items()], source="sales")

  inserted.sort(key=lambda t: (t["date"], t.get("hour") or -1))
  state: SalesState = {
    "ingestedAt": now_iso,
    "source": source or str(file_path or _DEFAULT_EXCEL_PATH),
    "transactions": inserted,
    "sourceFingerprint": fingerprint,
  }
  _save_sales_state(state)

  report: SalesIngestionReport = {
    "rows_read": rows_read,
    "rows_inserted": len(inserted),
    "rows_skipped": rows_read - len(inserted),
    "adjusted_rows": adjusted_rows,
    "needs_review": needs_review,
    "validation_errors": {"count": len(errors), "examples": errors[:5]},
    "ingested_at": now_iso,
    "source": state["source"] or "",
  }
  return report


def _filter_by_date(transactions: Iterable[SalesTransaction], start: Optional[date], end: Optional[date]) -> List[SalesTransaction]:
  results: List[SalesTransaction] = []
  for tx in transactions:
    try:
      tx_date = date.fromisoformat(tx["date"])
    except Exception:
      continue
    if start and tx_date < start:
      continue
    if end and tx_date > end:
      continue
    results.append(tx)
  return results


def sum_liters_since(
  fuel_type: str,
  since: str | date | datetime | None,
  *,
  transactions: Optional[Iterable[SalesTransaction]] = None,
) -> float:
  if not fuel_type or since is None:
    return 0.0
  baseline = _parse_datetime(since)
  if baseline is None:
    return 0.0
  if transactions is None:
    state = load_sales_state()
    transactions = state.get("transactions", [])
  total = 0.0
  for tx in transactions:
    if tx.get("fuel_type") != fuel_type:
      continue
    tx_dt = _transaction_datetime(tx)
    if tx_dt is None or tx_dt < baseline:
      continue
    try:
      liters = float(tx.get("liters", 0.0))
    except (TypeError, ValueError):
      liters = 0.0
    if math.isfinite(liters):
      total += liters
  return total


def _daily_totals(transactions: Iterable[SalesTransaction]) -> DailyFuelTotals:
  totals: Dict[date, Dict[str, float]] = {}
  by_fuel: Dict[str, Dict[str, Any]] = {}
  has_hour = False

  for tx in transactions:
    try:
      tx_date = date.fromisoformat(tx["date"])
    except Exception:
      continue
    fuel = tx.get("fuel_type", "").lower()
    liters = float(tx.get("liters", 0.0))
    hour = tx.get("hour")
    if hour is not None:
      has_hour = True

    day_totals = totals.setdefault(tx_date, {})
    day_totals[fuel] = day_totals.get(fuel, 0.0) + liters

    fuel_bucket = by_fuel.setdefault(fuel, {"daily": {}, "prices": [], "amount": 0.0, "gallons": 0.0, "liters": 0.0, "transactions": 0})
    fuel_bucket["daily"][tx_date] = fuel_bucket["daily"].get(tx_date, 0.0) + liters
    fuel_bucket["prices"].append(float(tx.get("price_per_gallon", 0.0)))
    amount = float(tx.get("total_amount", 0.0))
    if math.isfinite(amount):
      fuel_bucket["amount"] += amount
    fuel_bucket["gallons"] += float(tx.get("gallons", 0.0))
    fuel_bucket["liters"] += liters
    fuel_bucket["transactions"] += 1

  return DailyFuelTotals(totals=totals, by_fuel=by_fuel, has_hour_data=has_hour)


def build_recent_sales(
  days: int = 30,
  *,
  today: Optional[date] = None,
  transactions: Optional[Iterable[SalesTransaction]] = None,
  fingerprint: str | None = None,
) -> DailyFuelTotals:
  if transactions is None:
    state = load_sales_state()
    fingerprint = fingerprint or state.get("sourceFingerprint")
    cache = _get_cache(fingerprint)
    transactions = state.get("transactions", [])
    if today is None:
      today = effective_today(transactions)
    start = today - timedelta(days=days - 1)
    cache_key = f"{today.isoformat()}:{days}"
    cached = cache["recent"].get(cache_key)
    if cached:
      return cached
    filtered = _filter_by_date(transactions, start, today)
    totals = _daily_totals(filtered)
    cache["recent"][cache_key] = totals
    return totals

  today = today or effective_today(transactions)
  start = today - timedelta(days=days - 1)
  if fingerprint:
    cache = _get_cache(fingerprint)
    cache_key = f"{today.isoformat()}:{days}"
    cached = cache["recent"].get(cache_key)
    if cached:
      return cached
  filtered = _filter_by_date(transactions, start, today)
  totals = _daily_totals(filtered)
  if fingerprint:
    cache = _get_cache(fingerprint)
    cache["recent"][cache_key] = totals
  return totals


def compute_sales_summary(start: Optional[date] = None, end: Optional[date] = None) -> Dict[str, Any]:
  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cache_key = f"{start.isoformat() if start else 'none'}:{end.isoformat() if end else 'none'}"
  cached = cache["summary"].get(cache_key)
  if cached:
    return cached
  filtered = _filter_by_date(state.get("transactions", []), start, end)
  totals = _daily_totals(filtered)
  response: Dict[str, Any] = {
    "range": {
      "from": start.isoformat() if start else None,
      "to": end.isoformat() if end else None,
    },
    "ingested_at": state.get("ingestedAt"),
    "source": state.get("source"),
    "totals": {
      "transactions": len(filtered),
      "gallons": sum(tx.get("gallons", 0.0) for tx in filtered),
      "liters": sum(tx.get("liters", 0.0) for tx in filtered),
      "revenue": sum(
        value if (isinstance(value, (int, float)) and math.isfinite(value)) else 0.0
        for value in (tx.get("total_amount", 0.0) for tx in filtered)
      ),
    },
    "by_fuel_type": {},
    "has_hour_data": totals.has_hour_data,
  }
  for fuel, bucket in totals.by_fuel.items():
    revenue = float(bucket.get("amount", 0.0))
    if not math.isfinite(revenue):
      revenue = 0.0
    gallons = float(bucket.get("gallons", 0.0))
    avg_price = statistics.mean(bucket["prices"]) if bucket["prices"] else 0.0
    response["by_fuel_type"][fuel] = {
      "transactions": bucket.get("transactions", 0),
      "gallons": gallons,
      "liters": float(bucket.get("liters", 0.0)),
      "revenue": revenue,
      "average_price_per_gallon": avg_price,
    }
  cache["summary"][cache_key] = response
  return response


def peak_hours(top_n: int = 3) -> List[PeakHour]:
  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cached = cache.get("peak")
  if cached:
    return cached[:top_n]
  buckets: Dict[int, PeakHour] = {}
  for tx in state.get("transactions", []):
    hour = tx.get("hour")
    if hour is None:
      continue
    bucket = buckets.setdefault(hour, {"hour": hour, "transactions": 0, "gallons": 0.0, "liters": 0.0})
    bucket["transactions"] += 1
    bucket["gallons"] += float(tx.get("gallons", 0.0))
    bucket["liters"] += float(tx.get("liters", 0.0))
  ranked = sorted(buckets.values(), key=lambda item: (item["gallons"], item["transactions"]), reverse=True)
  cache["peak"] = ranked
  return ranked[:top_n]


def peak_hours_for_weekday(weekday: int, top_n: int = 3) -> List[PeakHour]:
  """Return peak hours filtered by a target weekday (0=Monday)."""
  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cached = cache["weekday"].get(weekday)
  if cached:
    return cached[:top_n]
  buckets: Dict[int, PeakHour] = {}
  for tx in state.get("transactions", []):
    hour = tx.get("hour")
    if hour is None:
      continue
    try:
      tx_date = date.fromisoformat(tx.get("date", ""))
    except Exception:
      continue
    if tx_date.weekday() != weekday:
      continue
    bucket = buckets.setdefault(hour, {"hour": hour, "transactions": 0, "gallons": 0.0, "liters": 0.0})
    bucket["transactions"] += 1
    bucket["gallons"] += float(tx.get("gallons", 0.0))
    bucket["liters"] += float(tx.get("liters", 0.0))
  ranked = sorted(buckets.values(), key=lambda item: (item["gallons"], item["transactions"]), reverse=True)
  cache["weekday"][weekday] = ranked
  return ranked[:top_n]


def monthly_earnings(month: str) -> MonthlyEarnings:
  def _r(val: float) -> float:
    return round(float(val), 4)

  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cached = cache["monthly"].get(month)
  if cached:
    return cached
  totals: MonthlyEarnings = {
    "month": month,
    "total_gallons_sold": 0.0,
    "total_liters_sold": 0.0,
    "total_revenue": 0.0,
    "total_revenue_raw": 0.0,
    "revenue_by_fuel_type": {},
    "raw_revenue_by_fuel_type": {},
    "gallons_by_fuel_type": {},
    "average_price_per_gallon_by_type": {},
    "price_per_gallon_range_by_type": {},
    "transactions": 0,
  }
  prices: Dict[str, List[float]] = {}

  for tx in state.get("transactions", []):
    if tx.get("month_key") != month:
      continue
    fuel = tx.get("fuel_type", "")
    totals["transactions"] += 1
    gallons = float(tx.get("gallons", 0.0))
    if not math.isfinite(gallons):
      gallons = 0.0
    liters = float(tx.get("liters", gallons * GALLON_TO_LITER))
    if not math.isfinite(liters):
      liters = gallons * GALLON_TO_LITER
    revenue = float(tx.get("total_amount", gallons * float(tx.get("price_per_gallon", 0.0))))
    if not math.isfinite(revenue):
      revenue = gallons * float(tx.get("price_per_gallon", 0.0))
    raw_amount = tx.get("raw_total_amount")
    raw_revenue = float(raw_amount) if raw_amount is not None else revenue
    if not math.isfinite(raw_revenue):
      raw_revenue = revenue

    totals["total_gallons_sold"] += gallons
    totals["total_liters_sold"] += liters
    totals["total_revenue"] += revenue
    totals["total_revenue_raw"] += raw_revenue

    totals["revenue_by_fuel_type"][fuel] = totals["revenue_by_fuel_type"].get(fuel, 0.0) + revenue
    totals["raw_revenue_by_fuel_type"][fuel] = totals["raw_revenue_by_fuel_type"].get(fuel, 0.0) + raw_revenue
    totals["gallons_by_fuel_type"][fuel] = totals["gallons_by_fuel_type"].get(fuel, 0.0) + gallons
    price_value = float(tx.get("price_per_gallon", 0.0))
    if math.isfinite(price_value) and price_value > 0:
      prices.setdefault(fuel, []).append(price_value)

  for fuel, gallons in totals["gallons_by_fuel_type"].items():
    revenue = totals["revenue_by_fuel_type"].get(fuel, 0.0)
    avg_price_gal = revenue / gallons if gallons > 0 else 0.0
    totals["average_price_per_gallon_by_type"][fuel] = _r(avg_price_gal)
    values = prices.get(fuel, [])
    if values:
      min_price = min(values)
      max_price = max(values)
    else:
      min_price = 0.0
      max_price = 0.0
    totals["price_per_gallon_range_by_type"][fuel] = {
      "min": _r(min_price),
      "max": _r(max_price),
    }

  totals["total_gallons_sold"] = _r(totals["total_gallons_sold"])
  totals["total_liters_sold"] = _r(totals["total_liters_sold"])
  totals["total_revenue"] = _r(totals["total_revenue"])
  totals["total_revenue_raw"] = _r(totals["total_revenue_raw"])
  totals["revenue_by_fuel_type"] = {fuel: _r(val) for fuel, val in totals["revenue_by_fuel_type"].items()}
  totals["raw_revenue_by_fuel_type"] = {fuel: _r(val) for fuel, val in totals["raw_revenue_by_fuel_type"].items()}
  totals["gallons_by_fuel_type"] = {fuel: _r(val) for fuel, val in totals["gallons_by_fuel_type"].items()}
  totals["price_per_gallon_range_by_type"] = {
    fuel: {"min": _r(entry.get("min", 0.0)), "max": _r(entry.get("max", 0.0))}
    for fuel, entry in totals["price_per_gallon_range_by_type"].items()
  }

  cache["monthly"][month] = totals
  return totals


def monthly_earnings_range(from_month: str, to_month: str) -> List[MonthlyEarnings]:
  def _month_key_to_tuple(key: str) -> Tuple[int, int]:
    year_str, month_str = key.split("-")
    return int(year_str), int(month_str)

  from_tuple = _month_key_to_tuple(from_month)
  to_tuple = _month_key_to_tuple(to_month)

  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cached_months = cache.get("months")
  if cached_months is None:
    months: set[str] = set()
    for tx in state.get("transactions", []):
      month_key = tx.get("month_key")
      if not month_key:
        continue
      months.add(month_key)
    cached_months = sorted(months)
    cache["months"] = cached_months

  months_in_range = [
    month_key
    for month_key in cached_months
    if _month_key_to_tuple(month_key) >= from_tuple and _month_key_to_tuple(month_key) <= to_tuple
  ]

  return [monthly_earnings(month) for month in months_in_range]


def forecast_next_month() -> Dict[str, Any]:
  state = load_sales_state()
  fingerprint = state.get("sourceFingerprint")
  cache = _get_cache(fingerprint)
  cached = cache.get("forecast")
  if cached:
    return cached
  revenue_by_month: Dict[str, float] = {}
  revenue_by_fuel_and_month: Dict[Tuple[str, str], float] = {}
  for tx in state.get("transactions", []):
    month_key = tx.get("month_key")
    if not month_key:
      continue
    revenue_raw = float(tx.get("total_amount", 0.0))
    revenue = revenue_raw if math.isfinite(revenue_raw) else float(tx.get("gallons", 0.0)) * float(tx.get("price_per_gallon", 0.0))
    fuel = tx.get("fuel_type", "")
    revenue_by_month[month_key] = revenue_by_month.get(month_key, 0.0) + revenue
    revenue_by_fuel_and_month[(fuel, month_key)] = revenue_by_fuel_and_month.get((fuel, month_key), 0.0) + revenue

  if not revenue_by_month:
    anchor = effective_today(state.get("transactions", []))
    next_month = (anchor.replace(day=1) + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    return {
      "month": next_month,
      "predicted_revenue": 0.0,
      "predicted_revenue_low": 0.0,
      "predicted_revenue_high": 0.0,
      "mom_delta_pct": None,
      "predicted_by_fuel_type": {},
      "confidence": "low",
      "trend_value": 0.0,
      "variability": 0.0,
      "last_month_revenue": 0.0,
      "basis_months": [],
    }

  ordered_months = sorted(revenue_by_month.keys())
  recent_months = ordered_months[-3:]
  last_month_key = ordered_months[-1]
  last_year, last_month = map(int, last_month_key.split("-"))
  rollover_month = 1 if last_month == 12 else last_month + 1
  rollover_year = last_year + 1 if rollover_month == 1 else last_year
  next_month_key = f"{rollover_year:04d}-{rollover_month:02d}"

  all_revenues = [revenue_by_month[m] for m in ordered_months if math.isfinite(revenue_by_month[m])]
  trend = 0.0
  if len(all_revenues) >= 2:
    diffs = [curr - prev for prev, curr in zip(all_revenues[:-1], all_revenues[1:])]
    trend = statistics.mean(diffs) if diffs else 0.0

  base = statistics.mean(all_revenues) if all_revenues else 0.0
  predicted_revenue = max(base + trend, 0.0)

  last_fuel_breakdown: Dict[str, float] = {}
  for (fuel, month_key), revenue in revenue_by_fuel_and_month.items():
    if month_key != last_month_key:
      continue
    last_fuel_breakdown[fuel] = revenue

  total_last_breakdown = sum(last_fuel_breakdown.values()) or 1.0
  predicted_by_fuel = {
    fuel: predicted_revenue * (value / total_last_breakdown)
    for fuel, value in last_fuel_breakdown.items()
  }

  confidence_value = "high" if len(all_revenues) >= 3 else "medium" if len(all_revenues) == 2 else "low"
  variability = statistics.pstdev(all_revenues) if len(all_revenues) > 1 else 0.0
  if base > 0 and variability / base > 0.35:
    confidence_value = "medium" if confidence_value == "high" else "low"
  low_band = max(predicted_revenue - variability, 0.0)
  high_band = max(predicted_revenue + variability, 0.0)
  last_month_revenue = revenue_by_month.get(last_month_key, 0.0)
  mom_delta_pct = None
  if last_month_revenue > 0:
    mom_delta_pct = (predicted_revenue - last_month_revenue) / last_month_revenue

  forecast_payload = {
    "month": next_month_key,
    "predicted_revenue": predicted_revenue,
    "predicted_revenue_low": low_band,
    "predicted_revenue_high": high_band,
    "mom_delta_pct": mom_delta_pct,
    "predicted_by_fuel_type": predicted_by_fuel,
    "confidence": confidence_value,
    "trend_value": trend,
    "variability": variability,
    "last_month_revenue": last_month_revenue,
    "basis_months": [{"month": m, "revenue": revenue_by_month[m]} for m in ordered_months],
  }
  cache["forecast"] = forecast_payload
  return forecast_payload
