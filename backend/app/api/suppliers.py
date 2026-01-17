from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.services.state import (
  SupplierDict,
  create_supplier,
  delete_supplier,
  list_suppliers,
  update_supplier,
)

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


class ContactPayload(BaseModel):
  name: Optional[str] = Field(default=None, max_length=160)
  email: Optional[str] = Field(default=None, max_length=200)
  phone: Optional[str] = Field(default=None, max_length=50)


class SupplierFuelPricePayload(BaseModel):
  fuelType: str = Field(min_length=1, max_length=40)
  pricePerGallon: float = Field(ge=0.0)
  currency: str = Field(default="GTQ", min_length=3, max_length=6)


class SupplierPayload(BaseModel):
  name: str = Field(min_length=1, max_length=200)
  category: str = Field(pattern="^(fuel|store)$")
  tags: List[str] = Field(default_factory=list)
  products: List[str] = Field(default_factory=list)
  active: bool = True
  basePriceNote: Optional[str] = Field(default=None, max_length=400)
  leadTimeHours: Optional[int] = Field(default=None, ge=0, le=240)
  fuelPrices: Optional[List[SupplierFuelPricePayload]] = None
  contact: Optional[ContactPayload] = None
  notes: Optional[str] = Field(default=None, max_length=1000)


class SupplierResponse(BaseModel):
  id: str
  name: str
  category: str
  tags: List[str]
  products: List[str]
  active: bool
  basePriceNote: Optional[str]
  leadTimeHours: Optional[int]
  fuelPrices: Optional[List[SupplierFuelPricePayload]] = None
  contact: Optional[Dict[str, Any]]
  notes: Optional[str]
  createdAt: str
  updatedAt: str


def _map_supplier(entry: SupplierDict) -> SupplierResponse:
  return SupplierResponse(**entry)


@router.get("", response_model=List[SupplierResponse])
def get_suppliers(category: str | None = Query(default=None, pattern="^(fuel|store)$")) -> List[SupplierResponse]:
  suppliers = list_suppliers(category)
  return [_map_supplier(entry) for entry in suppliers]


@router.post("", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier_api(payload: SupplierPayload) -> SupplierResponse:
  supplier = create_supplier(payload.model_dump())
  return _map_supplier(supplier)


@router.put("/{supplier_id}", response_model=SupplierResponse)
def update_supplier_api(supplier_id: str, payload: SupplierPayload) -> SupplierResponse:
  try:
    supplier = update_supplier(supplier_id, payload.model_dump())
  except KeyError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found") from exc
  return _map_supplier(supplier)


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_supplier_api(supplier_id: str) -> Response:
  try:
    delete_supplier(supplier_id)
  except KeyError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found") from exc
  return Response(status_code=status.HTTP_204_NO_CONTENT)
