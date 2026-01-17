from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, model_validator

from app.services.state import (
  InventoryItemDict,
  InventoryItemNotFoundError,
  InventoryRemovalLogDict,
  create_inventory_item,
  delete_inventory_item,
  list_inventory_items,
  list_inventory_removal_log,
  set_inventory_alert_days,
  update_inventory_item
)

router = APIRouter(prefix="/api/shop-inventory", tags=["shop-inventory"])


class InventoryItemPayload(BaseModel):
  name: str = Field(min_length=1, max_length=160)
  quantity: int = Field(ge=0)
  supplier: str = Field(default="", max_length=160)
  price: float = Field(ge=0.0)
  expirationDate: date
  reminderDaysBefore: int | None = Field(default=None, ge=0, le=365)

  @model_validator(mode="after")
  def _validate_expiration(self) -> "InventoryItemPayload":
    if self.expirationDate <= date.today():
      raise ValueError("Expiration date must be in the future.")
    return self


class InventoryItemUpdatePayload(BaseModel):
  name: Optional[str] = Field(default=None, min_length=1, max_length=160)
  quantity: Optional[int] = Field(default=None, ge=0)
  supplier: Optional[str] = Field(default=None, max_length=160)
  price: Optional[float] = Field(default=None, ge=0.0)
  expirationDate: Optional[date] = None
  reminderDaysBefore: Optional[int] = Field(default=None, ge=0, le=365)

  @model_validator(mode="after")
  def _validate_payload(self) -> "InventoryItemUpdatePayload":
    if not any([
      self.name is not None,
      self.quantity is not None,
      self.supplier is not None,
      self.price is not None,
      self.expirationDate is not None,
      self.reminderDaysBefore is not None,
    ]):
      raise ValueError("At least one field is required for update.")
    return self


class AlertSettingsPayload(BaseModel):
  alertDaysBefore: int = Field(ge=0, le=60)


class InventoryDeletePayload(BaseModel):
  reason: str = Field(default="manual", max_length=160)


class InventoryNotification(BaseModel):
  id: str
  name: str
  quantity: int
  supplier: str | None
  expirationDate: date
  price: float
  daysRemaining: int
  reminderDaysBefore: int


class InventoryItemResponse(BaseModel):
  id: str
  name: str
  quantity: int
  supplier: str | None
  price: float
  expirationDate: date
  createdAt: str
  updatedAt: str
  notificationSentAt: Optional[str] = None
  reminderDaysBefore: Optional[int] = None


class InventorySummaryResponse(BaseModel):
  alertDaysBefore: int
  generatedAt: str
  items: List[InventoryItemResponse]
  notifications: List[InventoryNotification]


class InventoryRemovalLogResponse(BaseModel):
  id: str
  itemId: str
  name: str
  supplier: Optional[str]
  quantity: int
  removedAt: str
  reason: str
  expirationDate: date


def _map_item_response(item: InventoryItemDict) -> InventoryItemResponse:
  return InventoryItemResponse(
    id=item["id"],
    name=item["name"],
    quantity=int(item.get("quantity", 0)),
    supplier=item.get("supplier") or None,
    price=float(item.get("price", 0.0)),
    expirationDate=date.fromisoformat(item["expirationDate"]),
    createdAt=item.get("createdAt", ""),
    updatedAt=item.get("updatedAt", ""),
    notificationSentAt=item.get("notificationSentAt"),
    reminderDaysBefore=item.get("reminderLeadDays"),
  )


def _map_notification(entry: Dict[str, Any]) -> InventoryNotification:
  return InventoryNotification(
    id=str(entry.get("id", "")),
    name=str(entry.get("name", "")),
    quantity=int(entry.get("quantity", 0)),
    supplier=str(entry.get("supplier", "")) or None,
    expirationDate=date.fromisoformat(str(entry.get("expirationDate"))),
    price=float(entry.get("price", 0.0)),
    daysRemaining=int(entry.get("daysRemaining", 0)),
    reminderDaysBefore=int(entry.get("reminderLeadDays", entry.get("alertDaysBefore", 0))),
  )


@router.get("", response_model=InventorySummaryResponse)
def get_inventory(
  search: str | None = Query(default=None, description="Filter by product name"),
  supplier: str | None = Query(default=None, description="Filter by supplier"),
  expires_from: date | None = Query(default=None),
  expires_to: date | None = Query(default=None),
  expires_on: date | None = Query(default=None, description="Products expiring exactly on this date"),
  expires_in_from: int | None = Query(default=None, ge=0, le=365, description="Min days until expiration"),
  expires_in_to: int | None = Query(default=None, ge=0, le=365, description="Max days until expiration"),
) -> InventorySummaryResponse:
  filters: Dict[str, str] = {}
  if search:
    filters["search"] = search
  if supplier:
    filters["supplier"] = supplier
  if expires_from:
    filters["expiresFrom"] = expires_from.isoformat()
  if expires_to:
    filters["expiresTo"] = expires_to.isoformat()
  if expires_on:
    filters["expiresOn"] = expires_on.isoformat()
  if expires_in_from is not None:
    filters["expiresInFrom"] = str(expires_in_from)
  if expires_in_to is not None:
    filters["expiresInTo"] = str(expires_in_to)

  summary = list_inventory_items(filters if filters else None)
  items = [_map_item_response(item) for item in summary.get("items", [])]
  notifications = [_map_notification(entry) for entry in summary.get("notifications", [])]

  return InventorySummaryResponse(
    alertDaysBefore=int(summary.get("alertDaysBefore", 5)),
    generatedAt=str(summary.get("generatedAt", "")),
    items=items,
    notifications=notifications,
  )


@router.post("", response_model=InventoryItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(payload: InventoryItemPayload) -> InventoryItemResponse:
  data = payload.model_dump()
  # Normalize reminder
  reminder = data.pop("reminderDaysBefore", None)
  if reminder is None or reminder == "":
    data.pop("reminderDaysBefore", None)
  else:
    try:
      reminder_val = max(0, min(int(reminder), 365))
    except (TypeError, ValueError):
      reminder_val = None
    if reminder_val is not None:
      data["reminderDaysBefore"] = reminder_val
    else:
      data.pop("reminderDaysBefore", None)
  item = create_inventory_item(data)
  return _map_item_response(item)


@router.put("/{item_id}", response_model=InventoryItemResponse)
def update_item(item_id: str, payload: InventoryItemUpdatePayload) -> InventoryItemResponse:
  raw = payload.model_dump(exclude_unset=True)
  data: Dict[str, Any] = {key: value for key, value in raw.items()}
  if "reminderDaysBefore" in data:
    reminder = data.pop("reminderDaysBefore")
    if reminder is None or reminder == "":
      data["reminderLeadDays"] = None
    else:
      try:
        data["reminderLeadDays"] = max(0, min(int(reminder), 365))
      except (TypeError, ValueError):
        data["reminderLeadDays"] = None
  try:
    item = update_inventory_item(item_id, data)
  except InventoryItemNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.") from exc
  return _map_item_response(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: str, payload: InventoryDeletePayload | None = None) -> Response:
  reason = payload.reason if payload else "manual"
  try:
    delete_inventory_item(item_id, reason=reason)
  except InventoryItemNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.") from exc
  return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/settings/alert-days", response_model=InventorySummaryResponse)
def update_alert_days(payload: AlertSettingsPayload) -> InventorySummaryResponse:
  summary = set_inventory_alert_days(payload.alertDaysBefore)
  items = [_map_item_response(item) for item in summary.get("items", [])]
  notifications = [_map_notification(entry) for entry in summary.get("notifications", [])]
  return InventorySummaryResponse(
    alertDaysBefore=int(summary.get("alertDaysBefore", 5)),
    generatedAt=str(summary.get("generatedAt", "")),
    items=items,
    notifications=notifications,
  )


@router.get("/logs", response_model=List[InventoryRemovalLogResponse])
def get_removal_logs(limit: int | None = Query(default=None, ge=1, le=200)) -> List[InventoryRemovalLogResponse]:
  logs = list_inventory_removal_log(limit)
  return [
    InventoryRemovalLogResponse(
      id=log["id"],
      itemId=log["itemId"],
      name=log["name"],
      supplier=log.get("supplier"),
      quantity=int(log.get("quantity", 0)),
      removedAt=log.get("removedAt", ""),
      reason=log.get("reason", ""),
      expirationDate=date.fromisoformat(log.get("expirationDate", date.today().isoformat())),
    )
    for log in logs
  ]
