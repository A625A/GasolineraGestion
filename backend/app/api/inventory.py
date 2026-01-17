from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app.services.gas_inventory import (
  GasInventoryItemDict,
  GasInventoryItemNotFoundError,
  GasInventoryListResult,
  GasInventoryTagConflictError,
  GasInventoryTagDict,
  GasInventoryTagInUseError,
  GasInventoryTagNotFoundError,
  create_gas_inventory_item,
  create_gas_inventory_tag,
  delete_gas_inventory_item,
  delete_gas_inventory_tag,
  list_gas_inventory_items,
  list_gas_inventory_tags,
  update_gas_inventory_item,
  update_gas_inventory_tag
)

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


class InventoryTagSummary(BaseModel):
  id: str
  label: str
  slug: str


class InventoryTagResponse(InventoryTagSummary):
  createdAt: str
  updatedAt: str
  usageCount: int = 0


class InventoryItemPayload(BaseModel):
  name: str = Field(min_length=1, max_length=160)
  description: str = Field(default="", max_length=2000)
  quantity: int = Field(default=0, ge=0)
  tags: List[str] = Field(default_factory=list)

  @field_validator("tags", mode="before")
  @classmethod
  def _ensure_tags(cls, value: Any) -> List[str]:
    if value is None:
      return []
    if isinstance(value, list):
      return [str(entry) for entry in value]
    raise TypeError("Tags must be an array.")


class InventoryItemUpdatePayload(BaseModel):
  name: Optional[str] = Field(default=None, min_length=1, max_length=160)
  description: Optional[str] = Field(default=None, max_length=2000)
  quantity: Optional[int] = Field(default=None, ge=0)
  tags: Optional[List[str]] = None

  @field_validator("tags", mode="before")
  @classmethod
  def _normalize_tags(cls, value: Any) -> Optional[List[str]]:
    if value is None:
      return None
    if isinstance(value, list):
      return [str(entry) for entry in value]
    raise TypeError("Tags must be an array.")


class InventoryItemResponse(BaseModel):
  id: str
  name: str
  description: str
  quantity: int
  tags: List[InventoryTagSummary]
  createdAt: str
  updatedAt: str


class InventoryListResponse(BaseModel):
  updatedAt: str
  total: int
  items: List[InventoryItemResponse]
  tags: List[InventoryTagResponse]


class InventoryTagPayload(BaseModel):
  label: str = Field(min_length=1, max_length=64)


def _map_tag_response(tag: GasInventoryTagDict, usage: Dict[str, int]) -> InventoryTagResponse:
  return InventoryTagResponse(
    id=tag["id"],
    label=tag["label"],
    slug=tag["slug"],
    createdAt=tag.get("createdAt", ""),
    updatedAt=tag.get("updatedAt", ""),
    usageCount=int(usage.get(tag["id"], 0))
  )


def _map_item_response(item: GasInventoryItemDict, tag_lookup: Dict[str, GasInventoryTagDict]) -> InventoryItemResponse:
  tag_ids = item.get("tags", [])
  tags = [
    InventoryTagSummary(
      id=tag_lookup[tag_id]["id"],
      label=tag_lookup[tag_id]["label"],
      slug=tag_lookup[tag_id]["slug"],
    )
    for tag_id in tag_ids
    if tag_lookup.get(tag_id)
  ]
  return InventoryItemResponse(
    id=item["id"],
    name=item.get("name", ""),
    description=item.get("description", ""),
    quantity=int(item.get("quantity", 0)),
    tags=tags,
    createdAt=item.get("createdAt", ""),
    updatedAt=item.get("updatedAt", "")
  )


def _build_summary(filters: Dict[str, Any] | None = None) -> InventoryListResponse:
  summary: GasInventoryListResult = list_gas_inventory_items(filters)
  tag_lookup = {tag["id"]: tag for tag in summary.get("tags", [])}
  usage = summary.get("usage", {})
  items = [_map_item_response(item, tag_lookup) for item in summary.get("items", [])]
  tags = [_map_tag_response(tag, usage) for tag in summary.get("tags", [])]
  return InventoryListResponse(
    updatedAt=summary.get("updatedAt", ""),
    total=int(summary.get("total", len(items))),
    items=items,
    tags=tags
  )


@router.get("", response_model=InventoryListResponse)
def get_inventory(
  search: str | None = Query(default=None, description="Filter by name, description or tags"),
  tags: List[str] = Query(default_factory=list, description="Filter by tag ids"),
) -> InventoryListResponse:
  filters: Dict[str, Any] = {}
  if search and search.strip():
    filters["search"] = search.strip()
  if tags:
    filters["tags"] = tags
  return _build_summary(filters if filters else None)


@router.post("", response_model=InventoryItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(payload: InventoryItemPayload) -> InventoryItemResponse:
  item = create_gas_inventory_item(payload.model_dump())
  tag_lookup = {tag["id"]: tag for tag in list_gas_inventory_tags()}
  return _map_item_response(item, tag_lookup)


@router.put("/{item_id}", response_model=InventoryItemResponse)
def update_item(item_id: str, payload: InventoryItemUpdatePayload) -> InventoryItemResponse:
  changes: Dict[str, Any] = payload.model_dump(exclude_unset=True)
  if not changes:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update.")
  try:
    item = update_gas_inventory_item(item_id, changes)
  except GasInventoryItemNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.") from exc
  tag_lookup = {tag["id"]: tag for tag in list_gas_inventory_tags()}
  return _map_item_response(item, tag_lookup)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def remove_item(item_id: str) -> Response:
  try:
    delete_gas_inventory_item(item_id)
  except GasInventoryItemNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.") from exc
  return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tags", response_model=List[InventoryTagResponse])
def list_tags() -> List[InventoryTagResponse]:
  summary = list_gas_inventory_items()
  usage = summary.get("usage", {})
  return [_map_tag_response(tag, usage) for tag in summary.get("tags", [])]


@router.post("/tags", response_model=InventoryTagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(payload: InventoryTagPayload) -> InventoryTagResponse:
  try:
    tag = create_gas_inventory_tag(payload.label)
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  usage = list_gas_inventory_items().get("usage", {})
  return _map_tag_response(tag, usage)


@router.put("/tags/{tag_id}", response_model=InventoryTagResponse)
def rename_tag(tag_id: str, payload: InventoryTagPayload) -> InventoryTagResponse:
  try:
    tag = update_gas_inventory_tag(tag_id, payload.label)
  except GasInventoryTagNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found.") from exc
  except GasInventoryTagConflictError as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
  usage = list_gas_inventory_items().get("usage", {})
  return _map_tag_response(tag, usage)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def remove_tag(tag_id: str) -> Response:
  try:
    delete_gas_inventory_tag(tag_id)
  except GasInventoryTagNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found.") from exc
  except GasInventoryTagInUseError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  return Response(status_code=status.HTTP_204_NO_CONTENT)
