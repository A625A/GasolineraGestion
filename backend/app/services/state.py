from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, TypedDict

State = Dict[str, Any]

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _BASE_DIR / "data"
_STATE_PATH = _DATA_DIR / "state.json"

_LOCK = threading.RLock()


class CalendarEventDict(TypedDict, total=False):
  id: str
  title: str
  description: str
  start: str
  end: str
  allDay: bool
  source: str
  fuelType: str | None
  metadata: Dict[str, Any]
  createdAt: str
  updatedAt: str


class InventoryItemDict(TypedDict, total=False):
  id: str
  name: str
  quantity: int
  supplier: str
  price: float
  expirationDate: str
  createdAt: str
  updatedAt: str
  notificationSentAt: str | None
  reminderLeadDays: int | None


class InventoryRemovalLogDict(TypedDict, total=False):
  id: str
  itemId: str
  name: str
  supplier: str
  quantity: int
  removedAt: str
  reason: str
  expirationDate: str


class InventoryItemNotFoundError(Exception):
  """Raised when an inventory item cannot be located."""


class WorkerDict(TypedDict, total=False):
  id: str
  fullName: str
  jobType: str
  role: str
  profileImage: str | None
  contact: Dict[str, Any]
  workSchedule: Dict[str, Any]
  payRate: float
  payType: str
  payFrequency: str
  basePay: float
  reminderLeadDays: int
  notificationDefault: bool
  nextPayDate: str
  createdAt: str
  updatedAt: str


class WorkerNotFoundError(Exception):
  """Raised when a worker cannot be located in staffing state."""


class SupplierDict(TypedDict, total=False):
  id: str
  name: str
  category: str  # "fuel" or "store"
  tags: List[str]
  products: List[str]
  active: bool
  basePriceNote: str | None
  leadTimeHours: int | None
  fuelPrices: List[Dict[str, Any]]
  contact: Dict[str, Any]
  notes: str | None
  createdAt: str
  updatedAt: str


class RestockOrderItemDict(TypedDict, total=False):
  fuelType: str
  gallons: float
  unitPriceSnapshot: float
  lineTotalSnapshot: float
  receivedGallons: float


class RestockReceiptDict(TypedDict, total=False):
  id: str
  receivedAt: str
  items: List[Dict[str, Any]]
  createdBy: str | None


class RestockOrderDict(TypedDict, total=False):
  id: str
  supplierId: str
  etaAt: str | None
  status: str  # planned | confirmed | partial | received | cancelled
  currency: str
  totalCostSnapshot: float
  createdBy: str | None
  financeTiming: str  # order | receive
  requestId: str | None
  items: List[RestockOrderItemDict]
  receipts: List[RestockReceiptDict]
  calendarEventId: str | None
  financeExpenseId: str | None
  createdAt: str
  updatedAt: str


class DeliveryDict(TypedDict, total=False):
  id: str
  supplierId: str
  fuelType: str
  quantity: float
  expectedCost: float | None
  scheduledAt: str
  status: str  # planned | confirmed | completed | cancelled
  calendarEventId: str | None
  financeExpenseId: str | None
  createdAt: str
  updatedAt: str


def _utc_now() -> datetime:
  return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def default_finance_state() -> State:
  now_iso = _isoformat(_utc_now())
  return {
    "updatedAt": now_iso,
    "currency": "GTQ",
    "earnings": {
      "updatedAt": now_iso,
      "fuel": {},
      "shop": {
        "amount": 0.0,
        "mode": "manual",
        "notes": "",
        "updatedAt": now_iso
      },
      "otherIncome": []
    },
    "costs": {
      "updatedAt": now_iso,
      "entries": []
    }
  }


def default_suppliers_state() -> Dict[str, Any]:
  now_iso = _isoformat(_utc_now())
  return {
    "updatedAt": now_iso,
    "entries": []
  }


def default_deliveries_state() -> Dict[str, Any]:
  now_iso = _isoformat(_utc_now())
  return {
    "updatedAt": now_iso,
    "entries": []
  }


def default_restock_orders_state() -> Dict[str, Any]:
  now_iso = _isoformat(_utc_now())
  return {
    "updatedAt": now_iso,
    "entries": []
  }


def default_staffing_state() -> Dict[str, Any]:
  now_iso = _isoformat(_utc_now())
  return {
    "updatedAt": now_iso,
    "workers": []
  }


def _bootstrap_state() -> State:
  now = _utc_now()
  today = date.today()
  default_meeting_start = datetime.combine(today, time(hour=15, minute=0, tzinfo=timezone.utc))
  default_meeting_end = default_meeting_start + timedelta(hours=1)

  default_state: State = {
    "settings": {
      "language": "es",
      "theme": "light",
      "personalInfo": {
        "fullName": "Coordinadora de Operaciones",
        "role": "Administración Central",
        "location": "Ciudad de Guatemala"
      },
      "contact": {
        "phone": "+502 5555 1234",
        "email": "operaciones.gasolinera@gmail.com",
        "emailVerified": True
      },
      "notificationPreferences": {
        "phone": True,
        "gmail": True,
        "inApp": True,
        "leadDaysBefore": 3
      },
      "predictive": {
        "confidenceThreshold": 0.75
      },
      "updatedAt": _isoformat(now)
    },
    "gasolineOverrides": {},
    "customFuelTypes": [],
    "discoveredFuelTypes": [],
    "calendar": {
      "events": [
        {
          "id": str(uuid.uuid4()),
          "title": "Revisión operativa semanal",
          "description": "Revisión de inventario, seguridad y ventas.",
          "start": _isoformat(default_meeting_start),
          "end": _isoformat(default_meeting_end),
          "allDay": False,
          "source": "manual",
          "fuelType": None,
          "metadata": {},
          "createdAt": _isoformat(now),
          "updatedAt": _isoformat(now)
        }
      ],
      "updatedAt": _isoformat(now)
    },
    "finance": default_finance_state()
  }
  default_state["staffing"] = default_staffing_state()
  default_state["suppliers"] = default_suppliers_state()
  default_state["deliveries"] = default_deliveries_state()
  default_state["restockOrders"] = default_restock_orders_state()
  default_state["gasInventory"] = {
    "items": [],
    "tags": [],
    "updatedAt": _isoformat(now)
  }
  return default_state


def _ensure_store() -> None:
  _DATA_DIR.mkdir(parents=True, exist_ok=True)
  if not _STATE_PATH.exists():
    state = _bootstrap_state()
    with _STATE_PATH.open("w", encoding="utf-8") as fh:
      json.dump(state, fh, ensure_ascii=False, indent=2)


def _ensure_suppliers(state: State) -> Dict[str, Any]:
  suppliers = state.setdefault("suppliers", default_suppliers_state())
  if "entries" not in suppliers:
    suppliers["entries"] = []
  suppliers.setdefault("updatedAt", _isoformat(_utc_now()))
  return suppliers


def _ensure_deliveries(state: State) -> Dict[str, Any]:
  deliveries = state.setdefault("deliveries", default_deliveries_state())
  if "entries" not in deliveries:
    deliveries["entries"] = []
  deliveries.setdefault("updatedAt", _isoformat(_utc_now()))
  return deliveries


def _ensure_restock_orders(state: State) -> Dict[str, Any]:
  restock_orders = state.setdefault("restockOrders", default_restock_orders_state())
  if "entries" not in restock_orders:
    restock_orders["entries"] = []
  restock_orders.setdefault("updatedAt", _isoformat(_utc_now()))
  return restock_orders


def _read_state_unlocked() -> State:
  with _STATE_PATH.open("r", encoding="utf-8") as fh:
    return json.load(fh)


def _write_state_unlocked(state: State) -> None:
  with _STATE_PATH.open("w", encoding="utf-8") as fh:
    json.dump(state, fh, ensure_ascii=False, indent=2)


def load_state() -> State:
  with _LOCK:
    _ensure_store()
    return deepcopy(_read_state_unlocked())


def update_state(mutator: Callable[[State], None]) -> State:
  with _LOCK:
    _ensure_store()
    state = _read_state_unlocked()
    mutator(state)
    _write_state_unlocked(state)
    return deepcopy(state)


def list_events() -> List[CalendarEventDict]:
  state = load_state()
  events = state.get("calendar", {}).get("events", [])
  return sorted(events, key=lambda event: event.get("start", ""))


def upsert_event(event: CalendarEventDict) -> CalendarEventDict:
  def _mutator(state: State) -> None:
    events: List[CalendarEventDict] = state.setdefault("calendar", {}).setdefault("events", [])
    for index, existing in enumerate(events):
      if existing["id"] == event["id"]:
        merged = {**existing, **event}
        merged["updatedAt"] = _isoformat(_utc_now())
        events[index] = merged
        break
    else:
      now_iso = _isoformat(_utc_now())
      event.setdefault("createdAt", now_iso)
      event["updatedAt"] = now_iso
      events.append(event)
    state.setdefault("calendar", {})["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)
  return event


def delete_event(event_id: str) -> None:
  def _mutator(state: State) -> None:
    events: List[CalendarEventDict] = state.setdefault("calendar", {}).setdefault("events", [])
    filtered = [event for event in events if event["id"] != event_id]
    if len(filtered) != len(events):
      state["calendar"]["events"] = filtered
      state["calendar"]["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)


def sync_predictive_events(payloads: List[Dict[str, Any]]) -> None:
  def _mutator(state: State) -> None:
    events: List[CalendarEventDict] = state.setdefault("calendar", {}).setdefault("events", [])
    existing_map = {event["id"]: event for event in events if event.get("source") == "predictive"}
    active_ids: set[str] = set()
    now_iso = _isoformat(_utc_now())

    for payload in payloads:
      fuel_id = payload["fuelType"]
      event_id = f"predictive-{fuel_id}"
      active_ids.add(event_id)
      event_start: str = payload["start"]
      event_end: str = payload["end"]
      base_event: CalendarEventDict = {
        "id": event_id,
        "title": payload["title"],
        "description": payload.get("description", ""),
        "start": event_start,
        "end": event_end,
        "allDay": payload.get("allDay", True),
        "source": "predictive",
        "fuelType": fuel_id,
        "metadata": payload.get("metadata", {}),
        "updatedAt": now_iso
      }

      existing = existing_map.get(event_id)
      if existing:
        base_event["createdAt"] = existing.get("createdAt", now_iso)
        for idx, current in enumerate(events):
          if current["id"] == event_id:
            events[idx] = {**current, **base_event}
            break
      else:
        base_event["createdAt"] = now_iso
        events.append(base_event)

    updated_events = [
      event for event in events if not (
        event.get("source") == "predictive" and event["id"] not in active_ids
      )
    ]
    state["calendar"]["events"] = updated_events
    state["calendar"]["updatedAt"] = now_iso

  update_state(_mutator)


def _ensure_shop_inventory(state: State) -> Dict[str, Any]:
  inventory = state.setdefault("shopInventory", {})
  inventory.setdefault("items", [])
  inventory.setdefault("removalLog", [])
  inventory.setdefault("alertDaysBefore", 5)
  inventory.setdefault("updatedAt", _isoformat(_utc_now()))
  return inventory


def _matches_inventory_filters(item: InventoryItemDict, filters: Dict[str, Any]) -> bool:
  search = filters.get("search")
  if isinstance(search, str) and search.strip():
    term = search.strip().lower()
    haystack = f"{item.get('name', '')} {item.get('supplier', '')}".lower()
    if term not in haystack:
      return False
  supplier = filters.get("supplier")
  if isinstance(supplier, str) and supplier.strip():
    if (item.get("supplier") or "").lower() != supplier.strip().lower():
      return False
  expires_from = filters.get("expiresFrom")
  if expires_from:
    try:
      dt_from = date.fromisoformat(expires_from)
      if date.fromisoformat(item["expirationDate"]) < dt_from:
        return False
    except ValueError:
      pass
  expires_to = filters.get("expiresTo")
  if expires_to:
    try:
      dt_to = date.fromisoformat(expires_to)
      if date.fromisoformat(item["expirationDate"]) > dt_to:
        return False
    except ValueError:
      pass
  expires_on = filters.get("expiresOn")
  if expires_on:
    try:
      dt_on = date.fromisoformat(expires_on)
      if date.fromisoformat(item["expirationDate"]) != dt_on:
        return False
    except ValueError:
      pass
  expires_in_from = filters.get("expiresInFrom")
  expires_in_to = filters.get("expiresInTo")
  if expires_in_from is not None or expires_in_to is not None:
    try:
      today = date.today()
      expires_dt = date.fromisoformat(item["expirationDate"])
      delta_days = (expires_dt - today).days
      if expires_in_from is not None and delta_days < int(expires_in_from):
        return False
      if expires_in_to is not None and delta_days > int(expires_in_to):
        return False
    except ValueError:
      pass
  return True


def _build_notifications(items: List[InventoryItemDict], alert_days: int) -> List[Dict[str, Any]]:
  today = date.today()
  notifications: List[Dict[str, Any]] = []
  for item in items:
    try:
      expires = date.fromisoformat(item["expirationDate"])
    except ValueError:
      continue
    item_lead = item.get("reminderLeadDays")
    effective_lead = item_lead if isinstance(item_lead, int) else alert_days
    days_remaining = (expires - today).days
    if days_remaining <= effective_lead:
      notifications.append({
        "id": item["id"],
        "name": item.get("name", ""),
        "quantity": int(item.get("quantity", 0)),
        "supplier": item.get("supplier"),
        "expirationDate": item.get("expirationDate", ""),
        "price": float(item.get("price", 0.0)),
        "daysRemaining": days_remaining,
        "reminderLeadDays": effective_lead,
        "alertDaysBefore": alert_days
      })
  notifications.sort(key=lambda entry: entry["daysRemaining"])
  return notifications


def list_inventory_items(filters: Dict[str, Any] | None = None) -> Dict[str, Any]:
  state = load_state()
  inventory = _ensure_shop_inventory(state)
  raw_items: List[InventoryItemDict] = inventory.get("items", [])
  applied_filters = filters or {}
  filtered = [deepcopy(item) for item in raw_items if _matches_inventory_filters(item, applied_filters)]
  filtered.sort(key=lambda entry: entry.get("expirationDate", ""))
  alert_days = int(inventory.get("alertDaysBefore", 5))
  notifications = _build_notifications(filtered, alert_days)
  return {
    "alertDaysBefore": alert_days,
    "generatedAt": _isoformat(_utc_now()),
    "items": filtered,
    "notifications": notifications
  }


def create_inventory_item(payload: Dict[str, Any]) -> InventoryItemDict:
  name = payload.get("name", "").strip()
  if not name:
    raise ValueError("Item name is required.")
  supplier = payload.get("supplier", "").strip()
  price = float(payload.get("price", 0.0))
  quantity = max(int(payload.get("quantity", 0)), 0)
  expiration = payload.get("expirationDate")
  if isinstance(expiration, date):
    expiration_str = expiration.isoformat()
  else:
    expiration_str = str(expiration)
  reminder_lead = payload.get("reminderDaysBefore")
  if reminder_lead is not None:
    try:
      reminder_lead = max(0, min(int(reminder_lead), 365))
    except (TypeError, ValueError):
      reminder_lead = None

  created: Dict[str, InventoryItemDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_shop_inventory(state)
    now_iso = _isoformat(_utc_now())
    item: InventoryItemDict = {
      "id": str(uuid.uuid4()),
      "name": name,
      "supplier": supplier,
      "price": price,
      "quantity": quantity,
      "expirationDate": expiration_str,
      "createdAt": now_iso,
      "updatedAt": now_iso,
      "notificationSentAt": None
    }
    if reminder_lead is not None:
      item["reminderLeadDays"] = reminder_lead
    inventory.setdefault("items", []).append(item)
    inventory["updatedAt"] = now_iso
    created["item"] = deepcopy(item)

  update_state(_mutator)
  return created["item"]


def update_inventory_item(item_id: str, data: Dict[str, Any]) -> InventoryItemDict:
  if not item_id:
    raise InventoryItemNotFoundError("Invalid inventory item id.")

  updated: Dict[str, InventoryItemDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_shop_inventory(state)
    items: List[InventoryItemDict] = inventory.setdefault("items", [])
    for index, item in enumerate(items):
      if item["id"] != item_id:
        continue
      now_iso = _isoformat(_utc_now())
      if "name" in data and isinstance(data["name"], str) and data["name"].strip():
        item["name"] = data["name"].strip()
      if "supplier" in data and isinstance(data["supplier"], str):
        item["supplier"] = data["supplier"].strip()
      if "price" in data and data["price"] is not None:
        try:
          item["price"] = float(data["price"])
        except (TypeError, ValueError):
          pass
      if "quantity" in data and data["quantity"] is not None:
        try:
          quantity = int(data["quantity"])
        except (TypeError, ValueError):
          quantity = item.get("quantity", 0)
        item["quantity"] = max(quantity, 0)
      if "expirationDate" in data and data["expirationDate"]:
        expires = data["expirationDate"]
        if isinstance(expires, date):
          item["expirationDate"] = expires.isoformat()
        else:
          item["expirationDate"] = str(expires)
      if "reminderLeadDays" in data or "reminderDaysBefore" in data:
        lead_val = data.get("reminderLeadDays", data.get("reminderDaysBefore"))
        try:
          if lead_val is None or lead_val == "":
            item.pop("reminderLeadDays", None)
          else:
            item["reminderLeadDays"] = max(0, min(int(lead_val), 365))
        except (TypeError, ValueError):
          item.pop("reminderLeadDays", None)
      item["updatedAt"] = now_iso
      inventory["updatedAt"] = now_iso
      items[index] = item
      updated["item"] = deepcopy(item)
      return
    raise InventoryItemNotFoundError("Inventory item not found.")

  update_state(_mutator)
  return updated["item"]


def delete_inventory_item(item_id: str, reason: str = "manual") -> None:
  if not item_id:
    raise InventoryItemNotFoundError("Invalid inventory item id.")

  def _mutator(state: State) -> None:
    inventory = _ensure_shop_inventory(state)
    items: List[InventoryItemDict] = inventory.setdefault("items", [])
    removal_log: List[InventoryRemovalLogDict] = inventory.setdefault("removalLog", [])
    now_iso = _isoformat(_utc_now())
    for index, item in enumerate(items):
      if item["id"] != item_id:
        continue
      removal_log.insert(0, {
        "id": str(uuid.uuid4()),
        "itemId": item["id"],
        "name": item.get("name", ""),
        "supplier": item.get("supplier", ""),
        "quantity": int(item.get("quantity", 0)),
        "removedAt": now_iso,
        "reason": reason,
        "expirationDate": item.get("expirationDate", now_iso[:10])
      })
      items.pop(index)
      inventory["updatedAt"] = now_iso
      return
    raise InventoryItemNotFoundError("Inventory item not found.")

  update_state(_mutator)


def set_inventory_alert_days(alert_days_before: int) -> Dict[str, Any]:
  def _mutator(state: State) -> None:
    inventory = _ensure_shop_inventory(state)
    inventory["alertDaysBefore"] = max(alert_days_before, 0)
    inventory["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)
  return list_inventory_items()


def list_inventory_removal_log(limit: int | None = None) -> List[InventoryRemovalLogDict]:
  state = load_state()
  inventory = _ensure_shop_inventory(state)
  logs: List[InventoryRemovalLogDict] = deepcopy(inventory.get("removalLog", []))
  logs.sort(key=lambda entry: entry.get("removedAt", ""), reverse=True)
  if limit is not None:
    return logs[:limit]
  return logs


def _ensure_staffing(state: State) -> Dict[str, Any]:
  staffing = state.setdefault("staffing", default_staffing_state())
  staffing.setdefault("workers", [])
  staffing.setdefault("updatedAt", _isoformat(_utc_now()))
  return staffing


def list_workers() -> List[WorkerDict]:
  state = load_state()
  staffing = _ensure_staffing(state)
  workers: List[WorkerDict] = staffing.get("workers", [])
  return deepcopy(workers)


def create_worker(payload: Dict[str, Any]) -> WorkerDict:
  created: Dict[str, WorkerDict] = {}

  def _mutator(state: State) -> None:
    staffing = _ensure_staffing(state)
    workers: List[WorkerDict] = staffing.setdefault("workers", [])
    now_iso = _isoformat(_utc_now())
    worker: WorkerDict = {
      "id": str(uuid.uuid4()),
      **payload,
      "createdAt": now_iso,
      "updatedAt": now_iso,
    }
    workers.append(worker)
    staffing["updatedAt"] = now_iso
    created["worker"] = deepcopy(worker)

  update_state(_mutator)
  return created["worker"]


def update_worker(worker_id: str, data: Dict[str, Any]) -> WorkerDict:
  updated: Dict[str, WorkerDict] = {}

  def _mutator(state: State) -> None:
    staffing = _ensure_staffing(state)
    workers: List[WorkerDict] = staffing.setdefault("workers", [])
    for index, worker in enumerate(workers):
      if worker.get("id") != worker_id:
        continue
      now_iso = _isoformat(_utc_now())
      worker.update(data)
      worker["updatedAt"] = now_iso
      workers[index] = worker
      staffing["updatedAt"] = now_iso
      updated["worker"] = deepcopy(worker)
      return
    raise WorkerNotFoundError("Worker not found.")

  update_state(_mutator)
  return updated["worker"]


def delete_worker(worker_id: str) -> None:
  def _mutator(state: State) -> None:
    staffing = _ensure_staffing(state)
    workers: List[WorkerDict] = staffing.setdefault("workers", [])
    filtered = [worker for worker in workers if worker.get("id") != worker_id]
    if len(filtered) == len(workers):
      raise WorkerNotFoundError("Worker not found.")
    staffing["workers"] = filtered
    staffing["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)


def list_suppliers(category: str | None = None) -> List[SupplierDict]:
  state = load_state()
  suppliers_state = _ensure_suppliers(state)
  entries: List[SupplierDict] = suppliers_state.get("entries", [])
  if category:
    return [entry for entry in entries if str(entry.get("category")) == category]
  return entries


def create_supplier(payload: Dict[str, Any]) -> SupplierDict:
  created: Dict[str, SupplierDict] = {}

  def _mutator(state: State) -> None:
    suppliers = _ensure_suppliers(state)
    now_iso = _isoformat(_utc_now())
    supplier: SupplierDict = {
      "id": str(uuid.uuid4()),
      "name": payload.get("name", ""),
      "category": payload.get("category", "fuel"),
      "tags": payload.get("tags") or [],
      "products": payload.get("products") or [],
      "active": bool(payload.get("active", True)),
      "basePriceNote": payload.get("basePriceNote"),
      "leadTimeHours": payload.get("leadTimeHours"),
      "fuelPrices": payload.get("fuelPrices") or [],
      "contact": payload.get("contact") or {},
      "notes": payload.get("notes"),
      "createdAt": now_iso,
      "updatedAt": now_iso,
    }
    suppliers.setdefault("entries", []).append(supplier)
    suppliers["updatedAt"] = now_iso
    created["supplier"] = deepcopy(supplier)

  update_state(_mutator)
  return created["supplier"]


def update_supplier(supplier_id: str, data: Dict[str, Any]) -> SupplierDict:
  updated: Dict[str, SupplierDict] = {}

  def _mutator(state: State) -> None:
    suppliers = _ensure_suppliers(state)
    entries: List[SupplierDict] = suppliers.setdefault("entries", [])
    for idx, supplier in enumerate(entries):
      if supplier.get("id") != supplier_id:
        continue
      now_iso = _isoformat(_utc_now())
      merged = {**supplier, **data, "updatedAt": now_iso}
      entries[idx] = merged
      suppliers["updatedAt"] = now_iso
      updated["supplier"] = deepcopy(merged)
      return
    raise KeyError("Supplier not found")

  update_state(_mutator)
  return updated["supplier"]


def delete_supplier(supplier_id: str) -> None:
  def _mutator(state: State) -> None:
    suppliers = _ensure_suppliers(state)
    entries: List[SupplierDict] = suppliers.setdefault("entries", [])
    filtered = [entry for entry in entries if entry.get("id") != supplier_id]
    if len(filtered) == len(entries):
      raise KeyError("Supplier not found")
    suppliers["entries"] = filtered
    suppliers["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)


def list_deliveries() -> List[DeliveryDict]:
  state = load_state()
  deliveries = _ensure_deliveries(state)
  return deliveries.get("entries", [])


def list_restock_orders() -> List[RestockOrderDict]:
  state = load_state()
  restock_orders = _ensure_restock_orders(state)
  return restock_orders.get("entries", [])


def create_delivery(payload: Dict[str, Any]) -> DeliveryDict:
  created: Dict[str, DeliveryDict] = {}

  def _mutator(state: State) -> None:
    deliveries = _ensure_deliveries(state)
    now_iso = _isoformat(_utc_now())
    delivery: DeliveryDict = {
      "id": str(uuid.uuid4()),
      "supplierId": payload.get("supplierId", ""),
      "fuelType": payload.get("fuelType", ""),
      "quantity": float(payload.get("quantity", 0.0) or 0.0),
      "expectedCost": payload.get("expectedCost"),
      "scheduledAt": payload.get("scheduledAt", now_iso),
      "status": payload.get("status", "planned"),
      "calendarEventId": payload.get("calendarEventId"),
      "financeExpenseId": payload.get("financeExpenseId"),
      "createdAt": now_iso,
      "updatedAt": now_iso,
    }
    deliveries.setdefault("entries", []).append(delivery)
    deliveries["updatedAt"] = now_iso
    created["delivery"] = deepcopy(delivery)

  update_state(_mutator)
  return created["delivery"]


def update_delivery(delivery_id: str, data: Dict[str, Any]) -> DeliveryDict:
  updated: Dict[str, DeliveryDict] = {}

  def _mutator(state: State) -> None:
    deliveries = _ensure_deliveries(state)
    entries: List[DeliveryDict] = deliveries.setdefault("entries", [])
    for idx, delivery in enumerate(entries):
      if delivery.get("id") != delivery_id:
        continue
      now_iso = _isoformat(_utc_now())
      merged = {**delivery, **data, "updatedAt": now_iso}
      entries[idx] = merged
      deliveries["updatedAt"] = now_iso
      updated["delivery"] = deepcopy(merged)
      return
    raise KeyError("Delivery not found")

  update_state(_mutator)
  return updated["delivery"]


__all__ = [
  "CalendarEventDict",
  "delete_event",
  "InventoryItemDict",
  "InventoryItemNotFoundError",
  "InventoryRemovalLogDict",
  "WorkerDict",
  "WorkerNotFoundError",
  "list_events",
  "list_inventory_items",
  "list_inventory_removal_log",
  "list_workers",
  "load_state",
  "set_inventory_alert_days",
  "sync_predictive_events",
  "update_state",
  "create_inventory_item",
  "update_inventory_item",
  "delete_inventory_item",
  "create_worker",
  "update_worker",
  "delete_worker",
  "default_finance_state",
  "default_staffing_state",
  "default_suppliers_state",
  "list_suppliers",
  "create_supplier",
  "update_supplier",
  "delete_supplier",
  "default_deliveries_state",
  "default_restock_orders_state",
  "list_deliveries",
  "list_restock_orders",
  "create_delivery",
  "update_delivery",
  "upsert_event"
]
