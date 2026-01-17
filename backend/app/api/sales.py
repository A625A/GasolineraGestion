from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app.services import sales_data

router = APIRouter(prefix="/api/sales", tags=["sales"])


class ValidationErrors(BaseModel):
  count: int
  examples: list[dict[str, Any]]


class SalesIngestionResponse(BaseModel):
  rows_read: int
  rows_inserted: int
  rows_skipped: int
  adjusted_rows: int
  needs_review: int
  validation_errors: ValidationErrors
  ingested_at: str
  source: str


class FuelSummary(BaseModel):
  transactions: int
  gallons: float
  liters: float
  revenue: float
  average_price_per_gallon: float


class SalesSummaryResponse(BaseModel):
  range: Dict[str, str | None]
  ingested_at: str | None
  source: str | None
  totals: Dict[str, float | int]
  by_fuel_type: Dict[str, FuelSummary]
  has_hour_data: bool


def _parse_date(value: str | None) -> date | None:
  if not value:
    return None
  try:
    return date.fromisoformat(value)
  except ValueError:
    return None


@router.post("/import", response_model=SalesIngestionResponse)
async def import_sales(file: UploadFile | None = File(default=None), use_default: bool = True) -> SalesIngestionResponse:
  if file is not None:
    content = await file.read()
    try:
      report = sales_data.ingest_sales_from_excel(file_bytes=content, source=file.filename)
    except RuntimeError as exc:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return SalesIngestionResponse(**report)  # type: ignore[arg-type]

  if not use_default:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide an Excel file or enable use_default.")

  default_path: Path = sales_data._DEFAULT_EXCEL_PATH  # pylint: disable=protected-access
  if not default_path.exists():
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Default Excel path is not configured.")

  try:
    report = sales_data.ingest_sales_from_excel(file_path=default_path, source=str(default_path))
  except RuntimeError as exc:
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
  return SalesIngestionResponse(**report)  # type: ignore[arg-type]


@router.get("/summary", response_model=SalesSummaryResponse)
def get_sales_summary(
  from_date: str | None = Query(default=None, description="YYYY-MM-DD"),
  to_date: str | None = Query(default=None, description="YYYY-MM-DD"),
) -> SalesSummaryResponse:
  start = _parse_date(from_date)
  end = _parse_date(to_date)
  if from_date and start is None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 'from' date format.")
  if to_date and end is None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 'to' date format.")

  summary = sales_data.compute_sales_summary(start=start, end=end)
  return SalesSummaryResponse(**summary)  # type: ignore[arg-type]
