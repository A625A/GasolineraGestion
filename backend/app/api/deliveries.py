from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services import fuel_types
from app.services.state import (
  create_delivery,
  list_deliveries,
  update_delivery,
  upsert_event,
  update_state,
)

router = APIRouter(prefix="/api/deliveries", tags=["deliveries"])


class DeliveryPayload(BaseModel):
  supplierId: str = Field(min_length=1)
  fuelType: str = Field(min_length=1)
  quantity: float = Field(ge=0.0)
  expectedCost: Optional[float] = Field(default=None, ge=0.0)
  scheduledAt: datetime
  status: str = Field(default="planned", pattern="^(planned|confirmed|completed|cancelled)$")
  calendarEventId: Optional[str] = None
  financeExpenseId: Optional[str] = None


class DeliveryResponse(DeliveryPayload):
  id: str
  createdAt: str
  updatedAt: str


def _upsert_calendar_event(payload: DeliveryPayload, delivery_id: str, fuel_label: str) -> str:
  event_id = payload.calendarEventId or f"delivery-{delivery_id}"
  start_iso = payload.scheduledAt.isoformat()
  end_iso = (payload.scheduledAt).isoformat()
  upsert_event({
    "id": event_id,
    "title": f"Restock {fuel_label}",
    "description": f"Proveedor: {payload.supplierId}",
    "start": start_iso,
    "end": end_iso,
    "allDay": True,
    "source": "delivery",
    "fuelType": payload.fuelType,
    "metadata": {
      "deliveryId": delivery_id,
      "fuelLabel": fuel_label,
      "expectedCost": payload.expectedCost,
    }
  })
  return event_id


def _ensure_finance_entry(payload: DeliveryPayload, delivery_id: str, expense_id: str | None = None) -> str:
  def _mutator(state: Dict[str, Any]) -> None:
    finance = state.setdefault("finance", {})
    costs = finance.setdefault("costs", {}).setdefault("entries", [])
    now_iso = datetime.utcnow().isoformat()
    if expense_id:
      for idx, entry in enumerate(costs):
        if entry.get("id") == expense_id:
          merged = {
            **entry,
            "name": f"Fuel delivery {payload.fuelType}",
            "amount": float(payload.expectedCost or 0.0),
            "type": "one_time",
            "dueDate": payload.scheduledAt.date().isoformat(),
            "notes": payload.supplierId,
            "source": "delivery",
            "updatedAt": now_iso,
            "metadata": {
              **(entry.get("metadata") or {}),
              "deliveryId": delivery_id,
              "status": payload.status,
            }
          }
          costs[idx] = merged
          return
    new_id = expense_id or f"delivery-expense-{delivery_id}"
    costs.append({
      "id": new_id,
      "name": f"Fuel delivery {payload.fuelType}",
      "amount": float(payload.expectedCost or 0.0),
      "type": "one_time",
      "dueDate": payload.scheduledAt.date().isoformat(),
      "notes": payload.supplierId,
      "reminder": {"enabled": False, "daysBefore": 0, "syncToCalendar": False, "calendarEventId": None},
      "createdAt": now_iso,
      "updatedAt": now_iso,
      "source": "delivery",
      "metadata": {"deliveryId": delivery_id, "status": payload.status}
    })
    finance["updatedAt"] = now_iso

  update_state(_mutator)
  return expense_id or f"delivery-expense-{delivery_id}"


@router.get("", response_model=list[DeliveryResponse])
def get_deliveries() -> list[DeliveryResponse]:
  return list_deliveries()  # type: ignore[return-value]


@router.post("", response_model=DeliveryResponse, status_code=status.HTTP_201_CREATED)
def create_delivery_api(payload: DeliveryPayload) -> DeliveryResponse:
  try:
    fuel_id = fuel_types.validate_fuel_id(payload.fuelType)
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  fuel_label = fuel_id.replace("-", " ").title()
  normalized_payload = payload.model_copy(update={"fuelType": fuel_id})
  delivery = create_delivery(normalized_payload.model_dump())
  event_id = _upsert_calendar_event(normalized_payload, delivery["id"], fuel_label)
  expense_id = _ensure_finance_entry(normalized_payload, delivery["id"])
  delivery = update_delivery(delivery["id"], {"calendarEventId": event_id, "financeExpenseId": expense_id})
  return DeliveryResponse(**delivery)


@router.put("/{delivery_id}", response_model=DeliveryResponse)
def update_delivery_api(delivery_id: str, payload: DeliveryPayload) -> DeliveryResponse:
  try:
    fuel_id = fuel_types.validate_fuel_id(payload.fuelType)
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  fuel_label = fuel_id.replace("-", " ").title()
  normalized_payload = payload.model_copy(update={"fuelType": fuel_id})
  try:
    delivery = update_delivery(delivery_id, normalized_payload.model_dump())
  except KeyError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found") from exc
  event_id = _upsert_calendar_event(normalized_payload, delivery_id, fuel_label)
  expense_id = _ensure_finance_entry(normalized_payload, delivery_id, delivery.get("financeExpenseId"))
  delivery = update_delivery(delivery_id, {"calendarEventId": event_id, "financeExpenseId": expense_id})
  return DeliveryResponse(**delivery)
