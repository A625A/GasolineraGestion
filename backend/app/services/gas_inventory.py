from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, TypedDict

from app.services.state import load_state, update_state

State = Dict[str, Any]


class GasInventoryItemDict(TypedDict, total=False):
  id: str
  name: str
  description: str
  quantity: int
  tags: List[str]
  createdAt: str
  updatedAt: str


class GasInventoryTagDict(TypedDict, total=False):
  id: str
  label: str
  slug: str
  createdAt: str
  updatedAt: str


class GasInventoryListResult(TypedDict, total=False):
  items: List[GasInventoryItemDict]
  tags: List[GasInventoryTagDict]
  usage: Dict[str, int]
  updatedAt: str
  total: int


class GasInventoryItemNotFoundError(Exception):
  """Raised when a gas inventory item cannot be located."""


class GasInventoryTagNotFoundError(Exception):
  """Raised when a gas inventory tag cannot be located."""


class GasInventoryTagInUseError(Exception):
  """Raised when attempting to remove a tag that is still referenced by items."""


class GasInventoryTagConflictError(Exception):
  """Raised when attempting to rename a tag to a label that already exists."""


def _utc_now() -> datetime:
  return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_inventory_container(state: State) -> Dict[str, Any]:
  inventory = state.setdefault("gasInventory", {})
  inventory.setdefault("items", [])
  inventory.setdefault("tags", [])
  inventory.setdefault("updatedAt", _isoformat(_utc_now()))
  return inventory


def _normalize_tag_label(label: str) -> str:
  return re.sub(r"\s+", " ", label.strip())


def _slugify(label: str) -> str:
  slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
  return slug or str(uuid.uuid4())


def _persist_tags(inventory: Dict[str, Any], raw_tags: List[str]) -> List[str]:
  tags: List[GasInventoryTagDict] = inventory.setdefault("tags", [])
  tag_map = {tag["slug"]: tag for tag in tags}
  ordered_ids: List[str] = []
  now_iso = _isoformat(_utc_now())

  for raw in raw_tags:
    normalized = _normalize_tag_label(raw)
    if not normalized:
      continue
    slug = _slugify(normalized)
    tag = tag_map.get(slug)
    if not tag:
      tag = {
        "id": str(uuid.uuid4()),
        "label": normalized,
        "slug": slug,
        "createdAt": now_iso,
        "updatedAt": now_iso
      }
      tags.append(tag)
      tag_map[slug] = tag
    elif tag["label"] != normalized:
      tag["label"] = normalized
      tag["updatedAt"] = now_iso
    if tag["id"] not in ordered_ids:
      ordered_ids.append(tag["id"])
  return ordered_ids


def _tag_usage(items: List[GasInventoryItemDict]) -> Dict[str, int]:
  usage: Dict[str, int] = {}
  for item in items:
    for tag_id in item.get("tags", []):
      usage[tag_id] = usage.get(tag_id, 0) + 1
  return usage


def list_gas_inventory_items(filters: Dict[str, Any] | None = None) -> GasInventoryListResult:
  state = load_state()
  inventory = _ensure_inventory_container(state)
  items: List[GasInventoryItemDict] = inventory.get("items", [])
  tags: List[GasInventoryTagDict] = inventory.get("tags", [])
  tag_lookup = {tag["id"]: tag for tag in tags}
  usage = _tag_usage(items)
  filters = filters or {}
  search_value = ""
  if isinstance(filters.get("search"), str):
    search_value = filters["search"].strip().lower()
  selected_tags = set(filters.get("tags") or [])

  def _matches(item: GasInventoryItemDict) -> bool:
    if selected_tags and not selected_tags.issubset(set(item.get("tags", []))):
      return False
    if search_value:
      haystack = f"{item.get('name', '')} {item.get('description', '')}".lower()
      if search_value in haystack:
        return True
      for tag_id in item.get("tags", []):
        tag = tag_lookup.get(tag_id)
        if tag and search_value in tag.get("label", "").lower():
          return True
      return False
    return True

  filtered_items = [deepcopy(item) for item in items if _matches(item)]
  filtered_items.sort(key=lambda entry: entry.get("updatedAt", ""), reverse=True)

  return {
    "items": filtered_items,
    "tags": deepcopy(tags),
    "usage": usage,
    "updatedAt": inventory.get("updatedAt", _isoformat(_utc_now())),
    "total": len(filtered_items)
  }


def create_gas_inventory_item(payload: Dict[str, Any]) -> GasInventoryItemDict:
  name = payload.get("name", "").strip()
  if not name:
    raise ValueError("Item name is required.")
  quantity = max(int(payload.get("quantity", 0)), 0)
  description = payload.get("description", "").strip()
  tags = payload.get("tags") or []

  created: Dict[str, GasInventoryItemDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    now_iso = _isoformat(_utc_now())
    tag_ids = _persist_tags(inventory, tags)
    item: GasInventoryItemDict = {
      "id": str(uuid.uuid4()),
      "name": name,
      "description": description,
      "quantity": quantity,
      "tags": tag_ids,
      "createdAt": now_iso,
      "updatedAt": now_iso
    }
    inventory.setdefault("items", []).append(item)
    inventory["updatedAt"] = now_iso
    created["item"] = deepcopy(item)

  update_state(_mutator)
  return created["item"]


def update_gas_inventory_item(item_id: str, data: Dict[str, Any]) -> GasInventoryItemDict:
  if not item_id:
    raise GasInventoryItemNotFoundError("Invalid inventory item id.")

  updated: Dict[str, GasInventoryItemDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    items: List[GasInventoryItemDict] = inventory.setdefault("items", [])
    for index, item in enumerate(items):
      if item["id"] != item_id:
        continue
      now_iso = _isoformat(_utc_now())
      if "name" in data and isinstance(data["name"], str):
        name = data["name"].strip()
        if name:
          item["name"] = name
      if "description" in data and isinstance(data["description"], str):
        item["description"] = data["description"].strip()
      if "quantity" in data:
        try:
          quantity = int(data["quantity"])
        except (TypeError, ValueError):
          quantity = item.get("quantity", 0)
        item["quantity"] = max(quantity, 0)
      if "tags" in data and isinstance(data["tags"], list):
        tag_labels = [str(label) for label in data["tags"]]
        item["tags"] = _persist_tags(inventory, tag_labels)
      item["updatedAt"] = now_iso
      inventory["updatedAt"] = now_iso
      items[index] = item
      updated["item"] = deepcopy(item)
      return
    raise GasInventoryItemNotFoundError("Inventory item not found.")

  update_state(_mutator)
  return updated["item"]


def delete_gas_inventory_item(item_id: str) -> None:
  if not item_id:
    raise GasInventoryItemNotFoundError("Invalid inventory item id.")

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    items: List[GasInventoryItemDict] = inventory.setdefault("items", [])
    filtered = [item for item in items if item["id"] != item_id]
    if len(filtered) == len(items):
      raise GasInventoryItemNotFoundError("Inventory item not found.")
    inventory["items"] = filtered
    inventory["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)


def list_gas_inventory_tags() -> List[GasInventoryTagDict]:
  state = load_state()
  inventory = _ensure_inventory_container(state)
  return deepcopy(inventory.get("tags", []))


def create_gas_inventory_tag(label: str) -> GasInventoryTagDict:
  normalized = _normalize_tag_label(label)
  if not normalized:
    raise ValueError("Tag label is required.")
  slug = _slugify(normalized)
  created: Dict[str, GasInventoryTagDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    tags: List[GasInventoryTagDict] = inventory.setdefault("tags", [])
    now_iso = _isoformat(_utc_now())
    for tag in tags:
      if tag["slug"] == slug:
        tag["label"] = normalized
        tag["updatedAt"] = now_iso
        created["tag"] = deepcopy(tag)
        return
    tag = {
      "id": str(uuid.uuid4()),
      "label": normalized,
      "slug": slug,
      "createdAt": now_iso,
      "updatedAt": now_iso
    }
    tags.append(tag)
    inventory["updatedAt"] = now_iso
    created["tag"] = deepcopy(tag)

  update_state(_mutator)
  return created["tag"]


def update_gas_inventory_tag(tag_id: str, label: str) -> GasInventoryTagDict:
  normalized = _normalize_tag_label(label)
  if not (tag_id and normalized):
    raise GasInventoryTagNotFoundError("Invalid tag data.")
  slug = _slugify(normalized)
  updated: Dict[str, GasInventoryTagDict] = {}

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    tags: List[GasInventoryTagDict] = inventory.setdefault("tags", [])
    now_iso = _isoformat(_utc_now())
    if any(tag["slug"] == slug and tag["id"] != tag_id for tag in tags):
      raise GasInventoryTagConflictError("Another tag already uses that label.")
    for tag in tags:
      if tag["id"] == tag_id:
        tag["label"] = normalized
        tag["slug"] = slug
        tag["updatedAt"] = now_iso
        inventory["updatedAt"] = now_iso
        updated["tag"] = deepcopy(tag)
        return
    raise GasInventoryTagNotFoundError("Tag not found.")

  update_state(_mutator)
  return updated["tag"]


def delete_gas_inventory_tag(tag_id: str) -> None:
  if not tag_id:
    raise GasInventoryTagNotFoundError("Invalid tag id.")

  def _mutator(state: State) -> None:
    inventory = _ensure_inventory_container(state)
    tags: List[GasInventoryTagDict] = inventory.setdefault("tags", [])
    items: List[GasInventoryItemDict] = inventory.setdefault("items", [])
    if any(tag_id in item.get("tags", []) for item in items):
      raise GasInventoryTagInUseError("Tag is still assigned to at least one item.")
    filtered = [tag for tag in tags if tag["id"] != tag_id]
    if len(filtered) == len(tags):
      raise GasInventoryTagNotFoundError("Tag not found.")
    inventory["tags"] = filtered
    inventory["updatedAt"] = _isoformat(_utc_now())

  update_state(_mutator)


__all__ = [
  "GasInventoryItemDict",
  "GasInventoryTagDict",
  "GasInventoryListResult",
  "GasInventoryItemNotFoundError",
  "GasInventoryTagNotFoundError",
  "GasInventoryTagInUseError",
  "GasInventoryTagConflictError",
  "list_gas_inventory_items",
  "create_gas_inventory_item",
  "update_gas_inventory_item",
  "delete_gas_inventory_item",
  "list_gas_inventory_tags",
  "create_gas_inventory_tag",
  "update_gas_inventory_tag",
  "delete_gas_inventory_tag"
]
