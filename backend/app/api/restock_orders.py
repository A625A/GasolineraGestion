from __future__ import annotations

from datetime import date, datetime, timezone
import uuid
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import fuel_types, sales_data
from app.services.state import load_state, update_state

router = APIRouter(prefix="/api/restock-orders", tags=["restock-orders"])


def _utc_now() -> datetime:
  return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _is_admin(request: Request) -> bool:
  user = getattr(request.state, "user", {}) or {}
  roles = user.get("roles", []) if isinstance(user, dict) else []
  return isinstance(roles, list) and "admin" in roles


class RestockOrderItemPayload(BaseModel):
  fuelType: str = Field(min_length=1, max_length=40)
  gallons: float = Field(gt=0)
  unitPriceOverride: float | None = Field(default=None, ge=0)


class RestockOrderCreatePayload(BaseModel):
  supplierId: str = Field(min_length=1)
  etaAt: datetime | None = None
  status: Literal["planned", "confirmed"] = "confirmed"
  items: List[RestockOrderItemPayload] = Field(min_length=1)
  currency: str = Field(default="GTQ", min_length=3, max_length=6)
  financeTiming: Literal["order", "receive"] = "order"
  requestId: str | None = Field(default=None, max_length=120)
  totalOverride: float | None = Field(default=None, ge=0)
  overrideApprovedBy: str | None = Field(default=None, max_length=120)


class RestockOrderItemResponse(BaseModel):
  fuelType: str
  gallons: float
  unitPriceSnapshot: float
  lineTotalSnapshot: float
  receivedGallons: float


class RestockReceiptResponse(BaseModel):
  id: str
  receivedAt: str
  items: List[Dict[str, Any]]
  createdBy: str | None = None


class RestockOrderResponse(BaseModel):
  id: str
  supplierId: str
  etaAt: str | None
  status: str
  currency: str
  totalCostSnapshot: float
  overrideApprovedBy: str | None = None
  createdBy: str | None = None
  financeTiming: str
  requestId: str | None = None
  items: List[RestockOrderItemResponse]
  receipts: List[RestockReceiptResponse]
  calendarEventId: str | None = None
  financeExpenseId: str | None = None
  createdAt: str
  updatedAt: str


class RestockReceiveItemPayload(BaseModel):
  fuelType: str = Field(min_length=1, max_length=40)
  gallons: float = Field(gt=0)


class RestockReceivePayload(BaseModel):
  receiptId: str | None = Field(default=None, max_length=120)
  items: List[RestockReceiveItemPayload] = Field(min_length=1)
  receivedAt: datetime | None = None
  createdBy: str | None = Field(default=None, max_length=120)


def _find_supplier(state: Dict[str, Any], supplier_id: str) -> Dict[str, Any]:
  suppliers = state.get("suppliers", {}).get("entries", [])
  for supplier in suppliers:
    if supplier.get("id") == supplier_id:
      return supplier
  raise HTTPException(status_code=404, detail="Supplier not found")


def _build_price_map(supplier: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
  price_map: Dict[str, Dict[str, Any]] = {}
  for entry in supplier.get("fuelPrices", []) or []:
    fuel_key = fuel_types.normalize_fuel_key(entry.get("fuelType")) or str(entry.get("fuelType", "")).strip()
    if not fuel_key:
      continue
    try:
      price = float(entry.get("pricePerGallon", 0.0))
    except (TypeError, ValueError):
      continue
    if price <= 0:
      continue
    price_map[fuel_key] = {
      "price": price,
      "currency": entry.get("currency") or "GTQ",
    }
  return price_map


def _upsert_calendar_event(state: Dict[str, Any], order_id: str, payload: Dict[str, Any]) -> str:
  event_id = payload.get("calendarEventId") or f"restock-order-{order_id}"
  eta_at = payload.get("etaAt")
  if not eta_at:
    return ""
  start_dt = datetime.fromisoformat(str(eta_at)) if isinstance(eta_at, str) else eta_at
  end_dt = start_dt
  now_iso = _iso(_utc_now())
  events = state.setdefault("calendar", {}).setdefault("events", [])
  for idx, event in enumerate(events):
    if event.get("id") == event_id:
      events[idx] = {
        **event,
        "title": payload["title"],
        "description": payload.get("description", ""),
        "start": _iso(start_dt),
        "end": _iso(end_dt),
        "allDay": True,
        "source": "restock-order",
        "metadata": payload.get("metadata", {}),
        "updatedAt": now_iso,
      }
      state["calendar"]["updatedAt"] = now_iso
      return event_id
  events.append({
    "id": event_id,
    "title": payload["title"],
    "description": payload.get("description", ""),
    "start": _iso(start_dt),
    "end": _iso(end_dt),
    "allDay": True,
    "source": "restock-order",
    "metadata": payload.get("metadata", {}),
    "createdAt": now_iso,
    "updatedAt": now_iso,
  })
  state["calendar"]["updatedAt"] = now_iso
  return event_id


def _ensure_finance_entry(state: Dict[str, Any], order_id: str, payload: Dict[str, Any]) -> str:
  finance = state.setdefault("finance", {})
  costs = finance.setdefault("costs", {}).setdefault("entries", [])
  now_iso = _iso(_utc_now())
  expense_id = payload.get("financeExpenseId") or f"restock-order-expense-{order_id}"
  for idx, entry in enumerate(costs):
    if entry.get("id") == expense_id:
      costs[idx] = {
        **entry,
        "name": payload["name"],
        "amount": payload["amount"],
        "type": "one_time",
        "dueDate": payload["dueDate"],
        "notes": payload.get("notes", ""),
        "source": "restock-order",
        "metadata": payload.get("metadata", {}),
        "updatedAt": now_iso,
      }
      finance["updatedAt"] = now_iso
      return expense_id
  costs.append({
    "id": expense_id,
    "name": payload["name"],
    "amount": payload["amount"],
    "type": "one_time",
    "dueDate": payload["dueDate"],
    "notes": payload.get("notes", ""),
    "reminder": {"enabled": False, "daysBefore": 0, "syncToCalendar": False, "calendarEventId": None},
    "createdAt": now_iso,
    "updatedAt": now_iso,
    "source": "restock-order",
    "metadata": payload.get("metadata", {}),
  })
  finance["updatedAt"] = now_iso
  return expense_id


def _apply_inventory_receipt(state: Dict[str, Any], fuel_id: str, gallons: float) -> None:
  liters = gallons * sales_data.GALLON_TO_LITER
  overrides = state.setdefault("gasolineOverrides", {})
  entry = dict(overrides.get(fuel_id, {}))
  baseline_liters = entry.get("inventoryBaselineLiters")
  current_override = entry.get("currentInventoryLiters")
  baseline_at = entry.get("inventoryBaselineAt")

  try:
    baseline_value = float(baseline_liters)
  except (TypeError, ValueError):
    baseline_value = None
  try:
    current_value = float(current_override)
  except (TypeError, ValueError):
    current_value = None

  current_inventory = current_value if current_value is not None else (baseline_value or 0.0)
  if baseline_at and baseline_value is not None:
    consumed = sales_data.sum_liters_since(fuel_id, baseline_at)
    current_inventory = max(baseline_value - consumed, 0.0)

  updated_inventory = max(current_inventory + liters, 0.0)
  entry["currentInventoryLiters"] = round(updated_inventory, 4)
  entry["inventoryBaselineLiters"] = round(updated_inventory, 4)
  entry["inventoryBaselineAt"] = _iso(_utc_now())
  entry["lastRestockDate"] = date.today().isoformat()
  overrides[fuel_id] = entry
  state["gasolineOverrides"] = overrides


def _normalize_order(entry: Dict[str, Any]) -> Dict[str, Any]:
  return {
    **entry,
    "items": entry.get("items") or [],
    "receipts": entry.get("receipts") or [],
    "financeTiming": entry.get("financeTiming", "order"),
  }


@router.get("", response_model=List[RestockOrderResponse])
def list_restock_orders() -> List[RestockOrderResponse]:
  state = load_state()
  entries = state.get("restockOrders", {}).get("entries", [])
  return [RestockOrderResponse(**_normalize_order(entry)) for entry in entries]


@router.post("", response_model=RestockOrderResponse, status_code=201)
def create_restock_order(payload: RestockOrderCreatePayload, request: Request) -> RestockOrderResponse:
  state = load_state()
  existing_orders = state.get("restockOrders", {}).get("entries", [])
  if payload.requestId:
    for order in existing_orders:
      if order.get("requestId") == payload.requestId:
        return RestockOrderResponse(**order)

  supplier = _find_supplier(state, payload.supplierId)
  price_map = _build_price_map(supplier)
  if not price_map:
    raise HTTPException(status_code=400, detail="Supplier fuel prices are missing.")

  items: list[Dict[str, Any]] = []
  total_cost = 0.0
  for item in payload.items:
    try:
      fuel_id = fuel_types.validate_fuel_id(item.fuelType)
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    price_entry = price_map.get(fuel_id)
    if not price_entry:
      raise HTTPException(status_code=400, detail=f"Missing price for fuel {fuel_id}.")
    unit_price = price_entry["price"]
    if item.unitPriceOverride is not None:
      if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Unit price override requires admin role.")
      unit_price = float(item.unitPriceOverride)
    line_total = unit_price * item.gallons
    total_cost += line_total
    items.append({
      "fuelType": fuel_id,
      "gallons": float(item.gallons),
      "unitPriceSnapshot": round(unit_price, 4),
      "lineTotalSnapshot": round(line_total, 4),
      "receivedGallons": 0.0,
    })

  if payload.totalOverride is not None:
    if not _is_admin(request):
      raise HTTPException(status_code=403, detail="Total override requires admin role.")
    if not payload.overrideApprovedBy:
      raise HTTPException(status_code=400, detail="overrideApprovedBy is required for total override.")
    total_cost = float(payload.totalOverride)

  now_iso = _iso(_utc_now())
  order_id = f"restock-{uuid.uuid4().hex}"
  order_data: Dict[str, Any] = {
    "id": order_id,
    "supplierId": payload.supplierId,
    "etaAt": payload.etaAt.isoformat() if payload.etaAt else None,
    "status": payload.status,
    "currency": payload.currency,
    "totalCostSnapshot": round(total_cost, 4),
    "overrideApprovedBy": payload.overrideApprovedBy if payload.totalOverride is not None else None,
    "createdBy": getattr(request.state, "user", {}).get("username") if hasattr(request.state, "user") else None,
    "financeTiming": payload.financeTiming,
    "requestId": payload.requestId,
    "items": items,
    "receipts": [],
    "calendarEventId": None,
    "financeExpenseId": None,
    "createdAt": now_iso,
    "updatedAt": now_iso,
  }

  def _mutator(state_mut: Dict[str, Any]) -> None:
    restock_orders = state_mut.setdefault("restockOrders", {}).setdefault("entries", [])
    restock_orders.append(order_data)
    state_mut["restockOrders"]["updatedAt"] = now_iso

    if payload.etaAt:
      title = f"Restock order - {supplier.get('name', payload.supplierId)}"
      event_id = _upsert_calendar_event(state_mut, order_id, {
        "calendarEventId": None,
        "etaAt": payload.etaAt,
        "title": title,
        "description": f"Proveedor: {supplier.get('name', payload.supplierId)}",
        "metadata": {
          "restockOrderId": order_id,
          "supplierId": payload.supplierId,
          "status": payload.status,
          "totalCostSnapshot": order_data["totalCostSnapshot"],
          "currency": payload.currency,
        },
      })
      order_data["calendarEventId"] = event_id

    if payload.financeTiming == "order":
      expense_id = _ensure_finance_entry(state_mut, order_id, {
        "financeExpenseId": None,
        "name": f"Restock order {supplier.get('name', payload.supplierId)}",
        "amount": order_data["totalCostSnapshot"],
        "dueDate": (payload.etaAt.date() if payload.etaAt else date.today()).isoformat(),
        "notes": supplier.get("name", payload.supplierId),
        "metadata": {
          "restockOrderId": order_id,
          "supplierId": payload.supplierId,
          "status": payload.status,
        },
      })
      order_data["financeExpenseId"] = expense_id

  update_state(_mutator)
  return RestockOrderResponse(**order_data)


@router.post("/{order_id}/receive", response_model=RestockOrderResponse)
def receive_restock_order(order_id: str, payload: RestockReceivePayload, request: Request) -> RestockOrderResponse:
  receipt_id = payload.receiptId or f"receipt-{int(_utc_now().timestamp())}"
  now_iso = _iso(payload.receivedAt or _utc_now())

  updated_order: Dict[str, Any] = {}

  def _mutator(state: Dict[str, Any]) -> None:
    restock_state = state.setdefault("restockOrders", {}).setdefault("entries", [])
  for idx, order in enumerate(restock_state):
      if order.get("id") != order_id:
        continue
      if order.get("status") == "received":
        updated_order.update(order)
        return
      receipts = order.setdefault("receipts", [])
      if any(receipt.get("id") == receipt_id for receipt in receipts):
        updated_order.update(order)
        return
      items = order.get("items", [])
      item_map = {item.get("fuelType"): item for item in items}
      applied: list[Dict[str, Any]] = []

      for entry in payload.items:
        try:
          fuel_id = fuel_types.validate_fuel_id(entry.fuelType)
        except ValueError as exc:
          raise HTTPException(status_code=400, detail=str(exc)) from exc
        order_item = item_map.get(fuel_id)
        if not order_item:
          raise HTTPException(status_code=400, detail=f"Fuel {fuel_id} is not part of this order.")
        try:
          ordered = float(order_item.get("gallons", 0.0))
          received = float(order_item.get("receivedGallons", 0.0))
        except (TypeError, ValueError):
          ordered = 0.0
          received = 0.0
        remaining = max(ordered - received, 0.0)
        incoming = min(float(entry.gallons), remaining)
        if incoming <= 0:
          continue
        order_item["receivedGallons"] = round(received + incoming, 4)
        applied.append({
          "fuelType": fuel_id,
          "gallons": round(incoming, 4),
          "liters": round(incoming * sales_data.GALLON_TO_LITER, 4),
        })
        _apply_inventory_receipt(state, fuel_id, incoming)

      if not applied:
        raise HTTPException(status_code=400, detail="No valid receipt items provided.")

      receipts.append({
        "id": receipt_id,
        "receivedAt": now_iso,
        "items": applied,
        "createdBy": payload.createdBy or (getattr(request.state, "user", {}).get("username") if hasattr(request.state, "user") else None),
      })

      all_received = True
      for item in items:
        ordered = float(item.get("gallons", 0.0))
        received = float(item.get("receivedGallons", 0.0))
        if received + 1e-6 < ordered:
          all_received = False
          break
      order["status"] = "received" if all_received else "partial"
      order["updatedAt"] = now_iso

      if order.get("financeTiming") == "receive" and order.get("status") == "received":
        supplier_name = None
        for supplier in state.get("suppliers", {}).get("entries", []):
          if supplier.get("id") == order.get("supplierId"):
            supplier_name = supplier.get("name")
            break
        expense_id = _ensure_finance_entry(state, order_id, {
          "financeExpenseId": order.get("financeExpenseId"),
          "name": f"Restock order {supplier_name or order.get('supplierId')}",
          "amount": float(order.get("totalCostSnapshot", 0.0)),
          "dueDate": date.today().isoformat(),
          "notes": supplier_name or order.get("supplierId"),
          "metadata": {
            "restockOrderId": order_id,
            "supplierId": order.get("supplierId"),
            "status": order.get("status"),
          },
        })
        order["financeExpenseId"] = expense_id

      if order.get("calendarEventId"):
        events = state.setdefault("calendar", {}).setdefault("events", [])
        for event in events:
          if event.get("id") == order.get("calendarEventId"):
            metadata = dict(event.get("metadata") or {})
            metadata["status"] = order.get("status")
            event["metadata"] = metadata
            event["updatedAt"] = now_iso
            break

      restock_state[idx] = order
      state["restockOrders"]["updatedAt"] = now_iso
      updated_order.update(order)
      return
    raise HTTPException(status_code=404, detail="Restock order not found")

  update_state(_mutator)
  return RestockOrderResponse(**updated_order)
