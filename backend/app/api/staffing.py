from __future__ import annotations

from datetime import date, datetime, time as time_type, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, model_validator

from app.api.finance import _normalize_finance
from app.services.staffing_predictions import build_staffing_recommendations
from app.services.state import (
  WorkerDict,
  WorkerNotFoundError,
  create_worker as state_create_worker,
  default_finance_state,
  default_staffing_state,
  delete_worker as state_delete_worker,
  load_state,
  list_workers as state_list_workers,
  update_state,
  update_worker as state_update_worker,
)


router = APIRouter(prefix="/api/staffing", tags=["staffing"])


def _now_iso() -> str:
  return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class WeekdayEnum(str, Enum):
  monday = "monday"
  tuesday = "tuesday"
  wednesday = "wednesday"
  thursday = "thursday"
  friday = "friday"
  saturday = "saturday"
  sunday = "sunday"


class WorkerContactPayload(BaseModel):
  type: Literal["phone", "email"] = "phone"
  value: str = Field(min_length=1, max_length=120)


class WorkerDailySchedulePayload(BaseModel):
  startTime: time_type
  endTime: time_type

  @model_validator(mode="after")
  def _validate_times(self) -> "WorkerDailySchedulePayload":
    if self.endTime <= self.startTime:
      raise ValueError("endTime must be after startTime")
    return self


class WorkerSchedulePayload(BaseModel):
  daysOfWeek: List[WeekdayEnum] = Field(default_factory=lambda: [
    WeekdayEnum.monday,
    WeekdayEnum.tuesday,
    WeekdayEnum.wednesday,
    WeekdayEnum.thursday,
    WeekdayEnum.friday,
  ], min_length=1)
  startTime: time_type
  endTime: time_type
  dayOverrides: Optional[Dict[WeekdayEnum, WorkerDailySchedulePayload]] = None

  @model_validator(mode="after")
  def _validate_times(self) -> "WorkerSchedulePayload":
    if self.endTime <= self.startTime:
      raise ValueError("endTime must be after startTime")
    if self.dayOverrides:
      for day, override in self.dayOverrides.items():
        if day not in self.daysOfWeek:
          raise ValueError(f"{day} must be part of daysOfWeek when using overrides.")
        if override.endTime <= override.startTime:
          raise ValueError("Override endTime must be after startTime")
    return self


class WorkerCreatePayload(BaseModel):
  fullName: str = Field(min_length=1, max_length=120)
  jobType: str = Field(min_length=1, max_length=80)
  payType: Literal["hourly", "salary"] = "salary"
  payRate: float = Field(ge=0.0)
  workSchedule: WorkerSchedulePayload
  contact: WorkerContactPayload
  profileImage: Optional[str] = None
  reminderLeadDays: int = Field(default=3, ge=0)
  notificationDefault: bool = True
  nextPayDate: Optional[date] = None


class WorkerUpdatePayload(BaseModel):
  fullName: Optional[str] = Field(default=None, min_length=1, max_length=120)
  jobType: Optional[str] = Field(default=None, min_length=1, max_length=80)
  workSchedule: Optional[WorkerSchedulePayload] = None
  payRate: Optional[float] = Field(default=None, ge=0.0)
  payType: Optional[Literal["hourly", "salary"]] = None
  reminderLeadDays: Optional[int] = Field(default=None, ge=0)
  notificationDefault: Optional[bool] = None
  contact: Optional[WorkerContactPayload] = None
  nextPayDate: Optional[date] = None
  profileImage: Optional[str] = Field(default=None, max_length=400)

  @model_validator(mode="after")
  def _ensure_payload(self) -> "WorkerUpdatePayload":
    fields_set = getattr(self, "model_fields_set", set())
    if not any([
      self.fullName is not None,
      self.jobType is not None,
      self.workSchedule is not None,
      self.payRate is not None,
      self.payType is not None,
      self.reminderLeadDays is not None,
      self.notificationDefault is not None,
      self.contact is not None,
      self.nextPayDate is not None,
      "profileImage" in fields_set,
    ]):
      raise ValueError("At least one field must be provided to update the worker.")
    return self


class WorkerContactResponse(BaseModel):
  type: Literal["phone", "email"]
  value: str


class WorkerDailyScheduleResponse(BaseModel):
  startTime: str
  endTime: str


class WorkerScheduleResponse(BaseModel):
  daysOfWeek: List[WeekdayEnum]
  startTime: str
  endTime: str
  dayOverrides: Optional[Dict[WeekdayEnum, WorkerDailyScheduleResponse]] = None


class WorkerResponse(BaseModel):
  id: str
  fullName: str
  jobType: str
  role: str
  profileImage: Optional[str] = None
  contact: WorkerContactResponse
  workSchedule: WorkerScheduleResponse
  payRate: float
  payType: Literal["hourly", "salary"]
  payFrequency: str
  basePay: float
  reminderLeadDays: int
  notificationDefault: bool
  nextPayDate: str
  createdAt: str
  updatedAt: str
  workingToday: bool


class StaffingAssignment(BaseModel):
  workerId: str
  fullName: str
  role: str
  start: str
  end: str


class StaffingBlock(BaseModel):
  start: str
  end: str
  demandLevel: Literal["low", "medium", "high"]
  required: int
  assigned: int
  assignments: List[StaffingAssignment]


class StaffingDayRecommendation(BaseModel):
  date: str
  weekday: str
  blocks: List[StaffingBlock]


class StaffingRecommendationResponse(BaseModel):
  generatedAt: str
  window: Dict[str, str]
  coverageScore: float
  warnings: List[str]
  schedule: List[StaffingDayRecommendation]
  hasHourData: bool


class ScheduleGridPayload(BaseModel):
  assignments: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)


class ScheduleGridResponse(BaseModel):
  updatedAt: str
  assignments: Dict[str, Dict[int, List[str]]]


_WEEKDAY_BY_INDEX: Dict[int, WeekdayEnum] = {
  0: WeekdayEnum.monday,
  1: WeekdayEnum.tuesday,
  2: WeekdayEnum.wednesday,
  3: WeekdayEnum.thursday,
  4: WeekdayEnum.friday,
  5: WeekdayEnum.saturday,
  6: WeekdayEnum.sunday,
}
_WEEKDAY_VALUE_MAP: Dict[str, WeekdayEnum] = {item.value: item for item in WeekdayEnum}


def _normalize_schedule_grid(
  assignments: Dict[str, Any],
  valid_worker_ids: set[str],
) -> Dict[str, Dict[int, List[str]]]:
  normalized: Dict[str, Dict[int, List[str]]] = {}
  for day_key, hours in assignments.items():
    if not isinstance(hours, dict):
      continue
    day = str(day_key).strip().lower()
    if day not in _WEEKDAY_VALUE_MAP:
      continue
    day_out: Dict[int, List[str]] = {}
    for hour_key, worker_ids in hours.items():
      try:
        hour = int(hour_key)
      except (TypeError, ValueError):
        continue
      if hour < 0 or hour > 23:
        continue
      if not isinstance(worker_ids, list):
        continue
      unique: list[str] = []
      seen: set[str] = set()
      for worker_id in worker_ids:
        if not isinstance(worker_id, str):
          continue
        if worker_id not in valid_worker_ids or worker_id in seen:
          continue
        seen.add(worker_id)
        unique.append(worker_id)
      day_out[hour] = unique
    if day_out:
      normalized[day] = day_out
  return normalized


def _serialize_schedule(worker: WorkerDict) -> WorkerScheduleResponse:
  schedule = worker.get("workSchedule") or {}
  days = schedule.get("daysOfWeek") or []
  day_values: list[str] = []
  for day in days:
    day_key = str(day).lower()
    if day_key in _WEEKDAY_VALUE_MAP:
      day_values.append(day_key)
  raw_overrides = schedule.get("dayOverrides") or {}
  overrides: Dict[WeekdayEnum, WorkerDailyScheduleResponse] = {}
  if isinstance(raw_overrides, dict):
    for key, value in raw_overrides.items():
      if not isinstance(key, str) or not isinstance(value, dict):
        continue
      day_key = key.strip().lower()
      if day_key not in _WEEKDAY_VALUE_MAP:
        continue
      if day_key not in day_values:
        continue
      start_override = str(value.get("startTime", schedule.get("startTime", "08:00")))
      end_override = str(value.get("endTime", schedule.get("endTime", "16:00")))
      overrides[_WEEKDAY_VALUE_MAP[day_key]] = WorkerDailyScheduleResponse(
        startTime=start_override,
        endTime=end_override,
      )
  response = WorkerScheduleResponse(
    daysOfWeek=[_WEEKDAY_VALUE_MAP[day] for day in day_values],
    startTime=str(schedule.get("startTime", "08:00")),
    endTime=str(schedule.get("endTime", "16:00")),
  )
  if overrides:
    response.dayOverrides = overrides
  return response


def _serialize_contact(worker: WorkerDict) -> WorkerContactResponse:
  contact = worker.get("contact") or {}
  contact_type = contact.get("type", "phone")
  if contact_type not in {"phone", "email"}:
    contact_type = "phone"
  return WorkerContactResponse(
    type=contact_type,  # type: ignore[arg-type]
    value=str(contact.get("value", "")),
  )


def _is_working_today(worker: WorkerDict) -> bool:
  schedule = worker.get("workSchedule") or {}
  days = schedule.get("daysOfWeek") or []
  weekday = _WEEKDAY_BY_INDEX[date.today().weekday()].value
  return weekday in days


def _serialize_worker(worker: WorkerDict) -> WorkerResponse:
  schedule = _serialize_schedule(worker)
  contact = _serialize_contact(worker)
  pay_type_raw = str(worker.get("payType", "salary"))
  pay_type = pay_type_raw if pay_type_raw in {"hourly", "salary"} else "salary"
  pay_frequency_raw = str(worker.get("payFrequency", "monthly"))
  pay_frequency = pay_frequency_raw if pay_frequency_raw else ("hourly" if pay_type == "hourly" else "monthly")
  pay_rate_value = worker.get("payRate", 0.0)
  try:
    pay_rate = float(pay_rate_value)
  except (TypeError, ValueError):
    pay_rate = 0.0
  base_pay_value = worker.get("basePay", pay_rate)
  try:
    base_pay = float(base_pay_value)
  except (TypeError, ValueError):
    base_pay = pay_rate
  reminder_value = worker.get("reminderLeadDays", 3)
  try:
    reminder = int(reminder_value)
  except (TypeError, ValueError):
    reminder = 3
  next_pay = str(worker.get("nextPayDate", date.today().isoformat()))
  created_at = str(worker.get("createdAt", date.today().isoformat()))
  updated_at = str(worker.get("updatedAt", created_at))
  return WorkerResponse(
    id=str(worker.get("id")),
    fullName=str(worker.get("fullName")),
    jobType=str(worker.get("jobType")),
    role=str(worker.get("role")),
    profileImage=worker.get("profileImage"),
    contact=contact,
    workSchedule=schedule,
    payRate=pay_rate,
    payType=pay_type,  # type: ignore[arg-type]
    payFrequency=pay_frequency,
    basePay=base_pay,
    reminderLeadDays=reminder,
    notificationDefault=bool(worker.get("notificationDefault", False)),
    nextPayDate=next_pay,
    createdAt=created_at,
    updatedAt=updated_at,
    workingToday=_is_working_today(worker),
  )


def _sync_finance_staffing() -> None:
  def _mutator(state: Dict[str, Any]) -> None:
    staffing = state.setdefault("staffing", default_staffing_state())
    finance_state = state.setdefault("finance", default_finance_state())
    state["finance"] = _normalize_finance(finance_state, staffing, sync_calendar=True, state=state)

  update_state(_mutator)


@router.get("/workers", response_model=List[WorkerResponse])
def list_workers_endpoint() -> List[WorkerResponse]:
  return [_serialize_worker(worker) for worker in state_list_workers()]


@router.get("/schedule/today", response_model=List[WorkerResponse])
def list_today_workers() -> List[WorkerResponse]:
  state = load_state()
  today_key = _WEEKDAY_BY_INDEX[date.today().weekday()].value
  schedule = state.get("staffingSchedule") or {}
  assignments = schedule.get("assignments") or {}
  day_assignments = assignments.get(today_key)
  if isinstance(day_assignments, dict) and day_assignments:
    worker_ids: set[str] = set()
    for entry in day_assignments.values():
      if not isinstance(entry, list):
        continue
      for worker_id in entry:
        if isinstance(worker_id, str):
          worker_ids.add(worker_id)
    workers = state.get("staffing", {}).get("workers", [])
    today_workers = [worker for worker in workers if worker.get("id") in worker_ids]
    return [_serialize_worker(worker) for worker in today_workers]
  today_workers = [worker for worker in state_list_workers() if _is_working_today(worker)]
  return [_serialize_worker(worker) for worker in today_workers]


@router.get("/schedule-grid", response_model=ScheduleGridResponse)
def get_schedule_grid() -> ScheduleGridResponse:
  state = load_state()
  schedule = state.get("staffingSchedule") or {}
  assignments = schedule.get("assignments") or {}
  workers = state_list_workers()
  valid_ids = {worker.get("id", "") for worker in workers if worker.get("id")}
  normalized = _normalize_schedule_grid(assignments, valid_ids)
  updated_at = schedule.get("updatedAt") or _now_iso()
  return ScheduleGridResponse(updatedAt=updated_at, assignments=normalized)


@router.put("/schedule-grid", response_model=ScheduleGridResponse)
def update_schedule_grid(payload: ScheduleGridPayload) -> ScheduleGridResponse:
  workers = state_list_workers()
  valid_ids = {worker.get("id", "") for worker in workers if worker.get("id")}
  normalized = _normalize_schedule_grid(payload.assignments, valid_ids)
  updated_at = _now_iso()

  def _mutator(state: Dict[str, Any]) -> None:
    state["staffingSchedule"] = {
      "updatedAt": updated_at,
      "assignments": normalized,
    }

  update_state(_mutator)
  return ScheduleGridResponse(updatedAt=updated_at, assignments=normalized)


@router.get("/recommendations", response_model=StaffingRecommendationResponse)
def get_staffing_recommendations(days: int = Query(default=7, ge=1, le=14)) -> StaffingRecommendationResponse:
  payload = build_staffing_recommendations(days=days)
  return StaffingRecommendationResponse(**payload)


@router.post("/workers", response_model=WorkerResponse, status_code=status.HTTP_201_CREATED)
def create_worker(payload: WorkerCreatePayload) -> WorkerResponse:
  schedule = payload.workSchedule
  contact = payload.contact
  worker_data: Dict[str, Any] = {
    "fullName": payload.fullName.strip(),
    "jobType": payload.jobType.strip(),
    "role": payload.jobType.strip(),
    "profileImage": payload.profileImage,
    "contact": {
      "type": contact.type,
      "value": contact.value.strip(),
    },
    "workSchedule": {
      "daysOfWeek": [day.value for day in schedule.daysOfWeek],
      "startTime": schedule.startTime.strftime("%H:%M"),
      "endTime": schedule.endTime.strftime("%H:%M"),
    },
    "payRate": payload.payRate,
    "basePay": payload.payRate,
    "payType": payload.payType,
    "payFrequency": "hourly" if payload.payType == "hourly" else "monthly",
    "reminderLeadDays": payload.reminderLeadDays,
    "notificationDefault": payload.notificationDefault,
    "nextPayDate": (payload.nextPayDate or date.today()).isoformat(),
  }

  if payload.workSchedule.dayOverrides:
    overrides: Dict[str, Dict[str, str]] = {}
    for day, override in payload.workSchedule.dayOverrides.items():
      overrides[day.value] = {
        "startTime": override.startTime.strftime("%H:%M"),
        "endTime": override.endTime.strftime("%H:%M"),
      }
    if overrides:
      worker_data["workSchedule"]["dayOverrides"] = overrides

  worker = state_create_worker(worker_data)
  _sync_finance_staffing()
  return _serialize_worker(worker)


@router.put("/workers/{worker_id}", response_model=WorkerResponse)
def update_worker(worker_id: str, payload: WorkerUpdatePayload) -> WorkerResponse:
  update_payload: Dict[str, Any] = {}

  if payload.fullName is not None:
    update_payload["fullName"] = payload.fullName.strip()

  if payload.jobType is not None:
    job_type = payload.jobType.strip()
    update_payload["jobType"] = job_type
    update_payload["role"] = job_type

  if payload.workSchedule is not None:
    schedule = payload.workSchedule
    update_payload["workSchedule"] = {
      "daysOfWeek": [day.value for day in schedule.daysOfWeek],
      "startTime": schedule.startTime.strftime("%H:%M"),
      "endTime": schedule.endTime.strftime("%H:%M"),
    }
    if schedule.dayOverrides:
      overrides: Dict[str, Dict[str, str]] = {}
      for day, override in schedule.dayOverrides.items():
        overrides[day.value] = {
          "startTime": override.startTime.strftime("%H:%M"),
          "endTime": override.endTime.strftime("%H:%M"),
        }
      update_payload["workSchedule"]["dayOverrides"] = overrides

  if payload.payRate is not None:
    update_payload["payRate"] = payload.payRate
    update_payload["basePay"] = payload.payRate

  if payload.payType is not None:
    update_payload["payType"] = payload.payType
    update_payload["payFrequency"] = "hourly" if payload.payType == "hourly" else "monthly"

  if payload.reminderLeadDays is not None:
    update_payload["reminderLeadDays"] = payload.reminderLeadDays

  if payload.notificationDefault is not None:
    update_payload["notificationDefault"] = payload.notificationDefault

  if payload.contact is not None:
    update_payload["contact"] = {
      "type": payload.contact.type,
      "value": payload.contact.value.strip(),
    }

  if payload.nextPayDate is not None:
    update_payload["nextPayDate"] = payload.nextPayDate.isoformat()

  if "profileImage" in payload.model_fields_set:
    image_value = (payload.profileImage or "").strip() if payload.profileImage else ""
    update_payload["profileImage"] = image_value or None

  if not update_payload:
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail="No valid fields provided for update.",
    )

  try:
    worker = state_update_worker(worker_id, update_payload)
  except WorkerNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found.") from exc

  _sync_finance_staffing()
  return _serialize_worker(worker)


@router.delete(
  "/workers/{worker_id}",
  status_code=status.HTTP_204_NO_CONTENT,
  response_class=Response
)
def delete_worker(worker_id: str) -> Response:
  try:
    state_delete_worker(worker_id)
  except WorkerNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found.") from exc
  _sync_finance_staffing()
  return Response(status_code=status.HTTP_204_NO_CONTENT)
