from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, FieldValidationInfo, field_validator

from app.services.email_client import EmailDeliveryError, credentials_available, send_email
from app.services.notification_preferences import get_email_notification_config
from app.services.state import (
  CalendarEventDict,
  delete_event,
  list_events,
  load_state,
  update_state,
  upsert_event
)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_reminder_task = None


def _ensure_utc(dt: datetime) -> datetime:
  if dt.tzinfo is None:
    return dt.replace(tzinfo=timezone.utc)
  return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
  return _ensure_utc(dt).replace(microsecond=0).isoformat()


_GT_TZ = ZoneInfo("America/Guatemala")


def _ensure_gt(dt: datetime) -> datetime:
  if dt.tzinfo is None:
    return dt.replace(tzinfo=_GT_TZ)
  return dt.astimezone(_GT_TZ)


def _utc_now() -> datetime:
  return datetime.now(timezone.utc)


class Metadata(BaseModel):
  confidence: float | None = None
  projectedDepletionDate: str | None = None
  recommendedVolume: float | None = None
  notes: str | None = None
  reminderEnabled: bool | None = None
  reminderLeadDays: int | None = None


class CalendarEventBase(BaseModel):
  title: str = Field(min_length=1, max_length=160)
  description: str | None = Field(default="", max_length=400)
  start: datetime
  end: datetime
  allDay: bool = False
  metadata: Metadata = Field(default_factory=Metadata)

  @field_validator("end")
  @classmethod
  def validate_chronology(cls, end: datetime, info: FieldValidationInfo) -> datetime:
    start: datetime = info.data.get("start")  # type: ignore[assignment]
    if start and _ensure_utc(end) <= _ensure_utc(start):
      raise ValueError("La fecha de finalización debe ser posterior al inicio")
    return end


class CalendarEventCreate(CalendarEventBase):
  pass


class CalendarEventUpdate(BaseModel):
  title: str | None = Field(default=None, max_length=160)
  description: str | None = Field(default=None, max_length=400)
  start: datetime | None = None
  end: datetime | None = None
  allDay: bool | None = None
  metadata: Metadata | None = None

  @field_validator("end")
  @classmethod
  def validate_update_chronology(cls, end: datetime | None, info: FieldValidationInfo) -> datetime | None:
    if end is None:
      return None
    start: datetime | None = info.data.get("start")  # type: ignore[assignment]
    if start and _ensure_utc(end) <= _ensure_utc(start):
      raise ValueError("La fecha de finalización debe ser posterior al inicio")
    return end


class CalendarEventResponse(CalendarEventBase):
  id: str
  source: str = "manual"
  fuelType: str | None = None
  createdAt: str
  updatedAt: str


def _to_response(event: CalendarEventDict) -> CalendarEventResponse:
  metadata = event.get("metadata") or {}
  response = CalendarEventResponse(
    id=event["id"],
    title=event["title"],
    description=event.get("description", ""),
    start=_ensure_utc(datetime.fromisoformat(event["start"])),
    end=_ensure_utc(datetime.fromisoformat(event["end"])),
    allDay=event.get("allDay", False),
    metadata=Metadata(**metadata),
    source=event.get("source", "manual"),
    fuelType=event.get("fuelType"),
    createdAt=event.get("createdAt", event["start"]),
    updatedAt=event.get("updatedAt", event["end"])
  )
  return response


def _dispatch_calendar_email_reminders() -> None:
  if not credentials_available():
    return
  state = load_state()
  config = get_email_notification_config(state)
  if not config:
    return
  recipient, _ = config

  calendar_state = state.get("calendar", {})
  events = calendar_state.get("events", [])
  if not events:
    return

  tz = ZoneInfo("America/Guatemala")
  today = datetime.now(tz).date()
  notifications: list[tuple[str, str, str, str]] = []

  for event in events:
    metadata = dict(event.get("metadata") or {})
    if not metadata.get("reminderEnabled"):
      continue
    lead_value = metadata.get("reminderLeadDays", 1)
    try:
      lead_days = max(0, min(int(lead_value), 60))
    except (TypeError, ValueError):
      lead_days = 1

    start_raw = event.get("start")
    if not start_raw:
      continue
    try:
      start_dt = datetime.fromisoformat(start_raw)
    except ValueError:
      continue
    start_local = start_dt.astimezone(tz) if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc).astimezone(tz)
    days_until = (start_local.date() - today).days
    if days_until != lead_days:
      continue
    if metadata.get("reminderEmailSentFor") == start_raw:
      continue

    description = event.get("description", "")
    subject = f"Recordatorio de evento: {event.get('title', 'Actividad programada')}"
    body_lines = [
      f"Título: {event.get('title', 'Evento')}",
      f"Fecha y hora: {start_local:%d/%m/%Y %H:%M} {start_local.tzinfo}",
    ]
    if description:
      body_lines.extend(["", f"Descripción: {description}"])
    body_lines.extend([
      "",
      "Este recordatorio se envió porque activaste notificaciones para este evento.",
      "",
      "--- English version ---",
      f"Reminder: {event.get('title', 'Scheduled event')}",
      f"Date & time: {start_local:%d/%m/%Y %H:%M} UTC",
    ])
    if description:
      body_lines.extend(["", f"Details: {description}"])
    body = "\n".join(body_lines)
    notifications.append((event["id"], start_raw, subject, body))

  if not notifications:
    return

  sent_marker = _iso(_utc_now())
  sent_ids: list[tuple[str, str]] = []
  for event_id, start_raw, subject, body in notifications:
    try:
      send_email(recipients=recipient, subject=subject, body=body)
    except EmailDeliveryError as exc:  # pragma: no cover - best-effort logging
      print(f"[CALENDAR_EMAIL_ERROR] Failed to send reminder for event {event_id}: {exc}")
    else:
      sent_ids.append((event_id, start_raw))

  if not sent_ids:
    return

  def _mutator(state: dict) -> None:
    events = state.setdefault("calendar", {}).setdefault("events", [])
    for event in events:
      for event_id, start_raw in sent_ids:
        if event.get("id") == event_id:
          metadata = dict(event.get("metadata") or {})
          metadata["reminderEmailSentFor"] = start_raw
          metadata["reminderSentAt"] = sent_marker
          event["metadata"] = metadata
          break

  update_state(_mutator)


async def reminder_loop(interval_seconds: int = 900) -> None:
  """Background loop to dispatch reminders periodically."""
  import asyncio
  while True:
    try:
      _dispatch_calendar_email_reminders()
    except Exception as exc:  # pragma: no cover - best effort logging
      print(f"[CALENDAR_EMAIL_ERROR] Background dispatch failed: {exc}")
    await asyncio.sleep(interval_seconds)


@router.get("/events", response_model=list[CalendarEventResponse])
def get_events() -> list[CalendarEventResponse]:
  _dispatch_calendar_email_reminders()
  events = list_events()
  return [_to_response(event) for event in events]


@router.post("/events", response_model=CalendarEventResponse, status_code=status.HTTP_201_CREATED)
def create_event(payload: CalendarEventCreate) -> CalendarEventResponse:
  event_id = str(uuid.uuid4())
  start_gt = _ensure_gt(payload.start)
  end_gt = _ensure_gt(payload.end)
  event: CalendarEventDict = {
    "id": event_id,
    "title": payload.title,
    "description": payload.description or "",
    "start": _iso(start_gt),
    "end": _iso(end_gt),
    "allDay": payload.allDay,
    "source": "manual",
    "fuelType": None,
    "metadata": payload.metadata.model_dump(exclude_none=True)
  }
  upsert_event(event)
  _dispatch_calendar_email_reminders()
  stored = next((item for item in list_events() if item["id"] == event_id), event)
  return _to_response(stored)


@router.put("/events/{event_id}", response_model=CalendarEventResponse)
def update_event(event_id: str, payload: CalendarEventUpdate) -> CalendarEventResponse:
  events = {event["id"]: event for event in list_events()}
  if event_id not in events:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento no encontrado")

  event = events[event_id]
  existing_metadata = dict(event.get("metadata") or {})
  incoming_metadata = payload.metadata.model_dump(exclude_none=True) if payload.metadata else {}
  merged_metadata = {**existing_metadata, **incoming_metadata}

  # If reminder settings or timing changed, allow future emails again.
  if payload.metadata or payload.start:
    if {"reminderEnabled", "reminderLeadDays"} & incoming_metadata.keys() or payload.start:
      merged_metadata.pop("reminderEmailSentFor", None)
      merged_metadata.pop("reminderSentAt", None)

  updated: CalendarEventDict = {
    **event,
    "title": payload.title if payload.title is not None else event["title"],
    "description": payload.description if payload.description is not None else event.get("description", ""),
    "start": _iso(_ensure_gt(payload.start)) if payload.start else event["start"],
    "end": _iso(_ensure_gt(payload.end)) if payload.end else event["end"],
    "allDay": payload.allDay if payload.allDay is not None else event.get("allDay", False),
    "metadata": merged_metadata,
  }
  upsert_event(updated)
  _dispatch_calendar_email_reminders()
  refreshed = next((item for item in list_events() if item["id"] == event_id), updated)
  return _to_response(refreshed)


@router.delete(
  "/events/{event_id}",
  status_code=status.HTTP_204_NO_CONTENT,
  response_class=Response
)
def remove_event(event_id: str) -> Response:
  events = {event["id"]: event for event in list_events()}
  if event_id not in events:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento no encontrado")
  delete_event(event_id)
  return Response(status_code=status.HTTP_204_NO_CONTENT)
