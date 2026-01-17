from __future__ import annotations

import uuid
import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.gasoline import build_gasoline_stats, set_fuel_price_override
from app.services.email_client import EmailDeliveryError, credentials_available, send_email
from app.services.notification_preferences import get_email_notification_config
from app.services import sales_data, fuel_types
from app.services.state import (
  default_finance_state,
  default_staffing_state,
  delete_event,
  update_state,
  upsert_event
)

FuelTypeId = str
RecurringType = Literal["one_time", "recurring"]
DataMode = Literal["manual", "synced"]

router = APIRouter(prefix="/api/finance", tags=["finance"])
_FINANCE_SUMMARY_CACHE: Dict[str, Any] = {"key": None, "response": None}


def _utc_now() -> datetime:
  return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> date:
  return date.today()


def _sales_today() -> date:
  return sales_data.effective_today()


def _parse_date(value: str, fallback: date | None = None) -> date:
  try:
    return date.fromisoformat(value)
  except ValueError:
    return fallback or _today()


def _current_month_key(today: date | None = None) -> str:
  today = today or _today()
  return today.strftime("%Y-%m")


def _current_sales_month_key(today: date | None = None) -> str:
  return _current_month_key(today or _sales_today())


def _parse_month_key(value: str | None) -> str:
  if not value:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mes inválido, use el formato YYYY-MM.")
  try:
    datetime.strptime(value, "%Y-%m")
    return value
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mes inválido, use el formato YYYY-MM.") from exc


def _ensure_month_rollover(finance: Dict[str, Any], state: Dict[str, Any]) -> bool:
  today = _today()
  current_month = _current_month_key(today)
  meta = finance.setdefault("meta", {})
  last_month = meta.get("currentMonth")
  if last_month == current_month:
    return False

  # Send monthly report email before resetting
  _send_monthly_report(finance, state, last_month or current_month)

  earnings = finance.setdefault("earnings", {})
  fuel = earnings.setdefault("fuel", {})
  for fuel_id, entry in fuel.items():
    entry["litersSold"] = 0.0
    entry.setdefault("mode", "synced")
    entry["updatedAt"] = _iso(_utc_now())
  shop = earnings.setdefault("shop", {})
  shop["amount"] = 0.0
  earnings["otherIncome"] = []

  costs = finance.setdefault("costs", {})
  for entry in costs.get("entries", []):
    metadata = entry.setdefault("metadata", {})
    reset_freq = str(metadata.get("resetFrequency") or "monthly").lower()
    if reset_freq != "never":
      metadata["bonus"] = 0.0
      metadata["discount"] = 0.0
    meta["currentMonth"] = current_month
  meta["lastPayrollEmailSent"] = None
  return True


def _send_monthly_report(finance: Dict[str, Any], state: Dict[str, Any], month_key: str) -> None:
  if not credentials_available():
    return
  config = get_email_notification_config(state)
  if not config:
    return
  recipient, _ = config
  monthly = sales_data.monthly_earnings(month_key)
  lines = [
    f"Reporte mensual {month_key}",
    f"Ingresos totales: {monthly.get('total_revenue', 0.0):.2f}",
    f"Galones vendidos: {monthly.get('total_gallons_sold', 0.0):.2f}",
    "",
    "Desglose por combustible:",
  ]
  for fuel, revenue in monthly.get("revenue_by_fuel_type", {}).items():
    gallons = monthly.get("gallons_by_fuel_type", {}).get(fuel, 0.0)
    lines.append(f"- {fuel}: Q{revenue:.2f} ({gallons:.2f} gal)")

  costs_total = 0.0
  for entry in finance.get("costs", {}).get("entries", []):
    try:
      due_date = _parse_date(str(entry.get("dueDate", "")))
      if due_date.strftime("%Y-%m") != month_key:
        continue
    except Exception:
      continue
    costs_total += float(entry.get("amount", 0.0))
  lines.append("")
  lines.append(f"Gastos del mes: Q{costs_total:.2f}")

  try:
    send_email(recipients=recipient, subject=f"Reporte mensual {month_key}", body="\n".join(lines))
  except EmailDeliveryError:
    return


def _maybe_send_payroll_email(finance: Dict[str, Any], state: Dict[str, Any]) -> None:
  meta = finance.setdefault("meta", {})
  today = _today().isoformat()
  if meta.get("lastPayrollEmailSent") == today:
    return
  costs = finance.get("costs", {}).get("entries", [])
  due_today: list[Dict[str, Any]] = []
  for entry in costs:
    if entry.get("source") != "staff":
      continue
    try:
      due_date = _parse_date(str(entry.get("dueDate", today)))
    except Exception:
      continue
    if due_date != _today():
      continue
    metadata = entry.get("metadata", {}) or {}
    bonus = float(metadata.get("bonus", 0.0))
    discount = float(metadata.get("discount", 0.0))
    total = float(entry.get("amount", 0.0)) + bonus - discount
    due_today.append({
      "name": entry.get("name", "Pago"),
      "base": entry.get("amount", 0.0),
      "bonus": bonus,
      "discount": discount,
      "total": total,
    })
  if not due_today or not credentials_available():
    return
  config = get_email_notification_config(state)
  if not config:
    return
  recipient, _ = config
  lines = ["Pagos programados para hoy:", ""]
  for item in due_today:
    lines.append(f"- {item['name']}: base Q{item['base']:.2f} + bono Q{item['bonus']:.2f} - descuento Q{item['discount']:.2f} = Q{item['total']:.2f}")
  try:
    send_email(recipients=recipient, subject="Resumen de pagos del día", body="\n".join(lines))
  except EmailDeliveryError:
    return
  meta["lastPayrollEmailSent"] = today


def _compute_reminder_date(due_date: str, days_before: int) -> str:
  due = _parse_date(due_date)
  reminder_day = due - timedelta(days=max(days_before, 0))
  return reminder_day.isoformat()


def _reset_period_key(freq: str, today: date) -> str:
  """Return a period key used to decide when to reset bonuses/discounts."""
  freq_lower = freq.lower()
  if freq_lower in {"never"}:
    return "never"
  if freq_lower in {"week", "weekly"}:
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"
  # default monthly
  return today.strftime("%Y-%m")


def _maybe_fire_due_notification(entry: Dict[str, Any]) -> None:
  """If a reminder is enabled and its reminderDate is today, send an email now."""
  reminder = entry.get("reminder", {})
  if not (reminder.get("enabled") and reminder.get("reminderDate")):
    return
  try:
    reminder_date = date.fromisoformat(str(reminder.get("reminderDate")))
  except ValueError:
    return
  if reminder_date != date.today():
    return

  config = get_email_notification_config()
  if not (config and credentials_available()):
    return

  recipient, _ = config
  subject = f"Recordatorio de pago: {entry.get('name', 'Costo')}"
  body = "\n".join([
    f"Pago pendiente: {entry.get('name', 'Costo')}",
    f"Monto: {entry.get('amount', 0.0)}",
    f"Fecha de pago: {entry.get('dueDate', '')}",
    "",
    "Este aviso se envió porque el recordatorio está programado para hoy.",
  ])
  try:
    send_email(recipients=recipient, subject=subject, body=body)
  except EmailDeliveryError:
    print(f"[FINANCE_EMAIL_ERROR] Failed to send reminder for cost {entry.get('id')}")


def _add_months(base: date, months: int) -> date:
  month = base.month - 1 + months
  year = base.year + month // 12
  month = month % 12 + 1
  day = min(base.day, calendar.monthrange(year, month)[1])
  return date(year, month, day)


def _align_due_date(due: date, frequency: str) -> date:
  today = _today()
  if due >= today:
    return due
  freq = frequency.lower()
  if freq == "weekly":
    step = 7
    while due < today:
      due += timedelta(days=step)
    return due
  if freq in {"biweekly", "fortnightly"}:
    step = 14
    while due < today:
      due += timedelta(days=step)
    return due
  while due < today:
    due = _add_months(due, 1)
  return due


def _ensure_staff_costs(
  finance: Dict[str, Any],
  staffing: Dict[str, Any] | None,
  now_iso: str,
  *,
  sync_calendar: bool = False
) -> None:
  if not staffing:
    return

  workers = staffing.get("workers") or []
  if not workers:
    return

  costs = finance.setdefault("costs", {})
  entries = costs.setdefault("entries", [])
  entry_map = {entry.get("id"): entry for entry in entries if entry.get("id")}
  staff_ids: set[str] = set()
  today = _today()
  for worker in workers:
    worker_id = str(worker.get("id", "")).strip()
    if not worker_id:
      continue
    cost_id = f"staff-{worker_id}"
    staff_ids.add(cost_id)
    name = worker.get("fullName", "Pago de nómina")
    role = worker.get("role", "")
    amount = float(worker.get("basePay", 0.0))
    pay_type = str(worker.get("payType", "salary")).lower()
    pay_frequency = str(worker.get("payFrequency", "monthly"))
    hourly_rate = float(worker.get("payRate", amount))
    raw_due = worker.get("nextPayDate")
    due_date = _parse_date(str(raw_due), fallback=today)
    due_date = _align_due_date(due_date, pay_frequency)
    due_iso = due_date.isoformat()
    reminder_lead = int(worker.get("reminderLeadDays", 3))
    default_notify = bool(worker.get("notificationDefault", False))

    existing = entry_map.get(cost_id)
    changed_worker = False
    if existing:
      reminder = existing.setdefault("reminder", {})
      # If the finance entry was manually adjusted, align the worker record to keep it in sync.
      existing_due_raw = existing.get("dueDate")
      if existing_due_raw:
        existing_due_iso = _parse_date(str(existing_due_raw), fallback=due_date).isoformat()
        if existing_due_iso != due_iso:
          due_iso = existing_due_iso
          worker["nextPayDate"] = due_iso
          changed_worker = True
      existing_reminder_days = reminder.get("daysBefore")
      if isinstance(existing_reminder_days, int) and existing_reminder_days != reminder_lead:
        reminder_lead = existing_reminder_days
        worker["reminderLeadDays"] = reminder_lead
        changed_worker = True
      if reminder.get("enabled") is not None:
        existing_enabled = bool(reminder.get("enabled"))
        if existing_enabled != default_notify:
          default_notify = existing_enabled
          worker["notificationDefault"] = default_notify
          changed_worker = True
      existing["name"] = name
      existing["type"] = "recurring"
      existing["dueDate"] = due_iso
      existing["notes"] = role
      existing["source"] = "staff"
      existing["updatedAt"] = now_iso
      reminder.setdefault("enabled", default_notify)
      reminder.setdefault("daysBefore", reminder_lead)
      reminder.setdefault("syncToCalendar", False)
      reminder.setdefault("calendarEventId", None)
    else:
      reminder = {
        "enabled": default_notify,
        "daysBefore": reminder_lead,
        "syncToCalendar": False,
        "calendarEventId": None,
      }
      if reminder["enabled"]:
        reminder["reminderDate"] = _compute_reminder_date(due_iso, reminder["daysBefore"])
      else:
        reminder["reminderDate"] = None
      existing = {
        "id": cost_id,
        "name": name,
        "amount": amount,
        "type": "recurring",
        "dueDate": due_iso,
        "notes": role,
        "reminder": reminder,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "source": "staff",
        "metadata": {},
      }
      entries.append(existing)
      entry_map[cost_id] = existing
    metadata = existing.setdefault("metadata", {})
    metadata.setdefault("payType", pay_type)
    metadata.setdefault("payFrequency", pay_frequency)
    metadata.setdefault("hourlyRate", hourly_rate)
    metadata.setdefault("hours", None)
    metadata.setdefault("bonus", 0.0)
    metadata.setdefault("discount", 0.0)
    reset_freq = str(metadata.get("resetFrequency") or pay_frequency or "monthly")
    metadata["resetFrequency"] = reset_freq
    metadata.setdefault("resetPeriod", _reset_period_key(reset_freq, today))
    metadata.setdefault("lastResetDue", None)

    current_period = _reset_period_key(reset_freq, today)
    stored_period = metadata.get("resetPeriod")
    if reset_freq != "never":
      if stored_period and stored_period != current_period:
        metadata["bonus"] = 0.0
        metadata["discount"] = 0.0
      metadata["resetPeriod"] = current_period
    else:
      metadata["resetPeriod"] = "never"

    try:
      bonus = float(metadata.get("bonus", 0.0))
    except (TypeError, ValueError):
      bonus = 0.0
    try:
      discount = float(metadata.get("discount", 0.0))
    except (TypeError, ValueError):
      discount = 0.0
    hours_val = metadata.get("hours", None)
    hours_num = None
    try:
      hours_num = float(hours_val) if hours_val is not None else None
    except (TypeError, ValueError):
      hours_num = None
    if pay_type == "hourly" and hours_num is not None:
      computed_amount = (metadata.get("hourlyRate") or hourly_rate) * hours_num
    else:
      computed_amount = amount
    # Bonus/discount remain tracked separately; keep base amount unchanged.
    existing["amount"] = float(max(computed_amount, 0.0))

    if changed_worker:
      worker["updatedAt"] = now_iso
      staffing["updatedAt"] = now_iso

    reminder = existing.setdefault("reminder", {})
    if reminder.get("enabled"):
      reminder["reminderDate"] = _compute_reminder_date(due_iso, reminder.get("daysBefore", 3))
    else:
      reminder["reminderDate"] = None
    if sync_calendar:
      _sync_calendar_event(existing)

  removed: list[Dict[str, Any]] = []
  retained: list[Dict[str, Any]] = []
  for entry in entries:
    if entry.get("source") == "staff" and entry.get("id") not in staff_ids:
      removed.append(entry)
    else:
      retained.append(entry)

  if sync_calendar:
    for entry in removed:
      reminder = entry.get("reminder", {})
      if reminder.get("calendarEventId"):
        reminder = {**reminder, "enabled": False}
        entry["reminder"] = reminder
        _sync_calendar_event(entry)

  if len(retained) != len(entries):
    costs["entries"] = retained

  costs["updatedAt"] = now_iso
  costs["entries"].sort(key=lambda cost: (cost.get("dueDate", ""), cost.get("name", "")))


def _normalize_finance(
  finance: Dict[str, Any],
  staffing: Dict[str, Any] | None = None,
  *,
  sync_calendar: bool = False,
  state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
  now_iso = _iso(_utc_now())
  finance.setdefault("updatedAt", now_iso)
  finance.setdefault("currency", "GTQ")

  earnings = finance.setdefault("earnings", {})
  earnings.setdefault("updatedAt", now_iso)

  fuel = earnings.setdefault("fuel", {})
  fuel_types_list = fuel_types.get_fuel_types(state)
  existing_ids = [fuel_types.normalize_fuel_key(key) or str(key) for key in fuel.keys()]
  fuel_ids = sorted({entry.get("id") for entry in fuel_types_list if entry.get("id")} | set(existing_ids))
  for fuel_id in fuel_ids:
    fuel_entry = fuel.setdefault(fuel_id, {})
    fuel_entry.setdefault("pricePerGallon", 0.0)
    fuel_entry.setdefault("litersSold", 0.0)
    mode = fuel_entry.get("mode")
    if mode not in ("manual", "synced"):
      fuel_entry["mode"] = "synced"
    elif mode == "manual":
      price_value = fuel_entry.get("pricePerGallon", 0.0)
      liters_value = fuel_entry.get("litersSold", 0.0)
      if (isinstance(price_value, (int, float)) and price_value == 0.0) and (
        isinstance(liters_value, (int, float)) and liters_value == 0.0
      ):
        fuel_entry["mode"] = "synced"
    fuel_entry.setdefault("updatedAt", now_iso)

    if not isinstance(fuel_entry.get("pricePerGallon"), (int, float)):
      fuel_entry["pricePerGallon"] = 0.0
    if not isinstance(fuel_entry.get("litersSold"), (int, float)):
      fuel_entry["litersSold"] = 0.0
    if fuel_entry.get("mode") not in ("manual", "synced"):
      fuel_entry["mode"] = "manual"

  shop = earnings.setdefault("shop", {})
  shop.setdefault("amount", 0.0)
  shop.setdefault("mode", "manual")
  shop.setdefault("notes", "")
  shop.setdefault("updatedAt", now_iso)
  if not isinstance(shop.get("amount"), (int, float)):
    shop["amount"] = 0.0
  if shop.get("mode") not in ("manual", "synced"):
    shop["mode"] = "manual"
  if shop.get("notes") is None:
    shop["notes"] = ""

  other_income = earnings.setdefault("otherIncome", [])
  normalized_income: list[Dict[str, Any]] = []
  for entry in other_income:
    item = dict(entry) if isinstance(entry, dict) else {}
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("name", "")
    item.setdefault("amount", 0.0)
    item.setdefault("category", None)
    item.setdefault("source", "manual")
    item.setdefault("createdAt", now_iso)
    item.setdefault("updatedAt", now_iso)
    if not isinstance(item.get("amount"), (int, float)):
      item["amount"] = 0.0
    if item.get("source") not in ("manual", "synced"):
      item["source"] = "manual"
    normalized_income.append(item)
  earnings["otherIncome"] = normalized_income

  costs = finance.setdefault("costs", {})
  costs.setdefault("updatedAt", now_iso)
  entries = costs.setdefault("entries", [])
  normalized_costs: list[Dict[str, Any]] = []
  for entry in entries:
    cost = dict(entry) if isinstance(entry, dict) else {}
    cost.setdefault("id", str(uuid.uuid4()))
    cost.setdefault("name", "")
    cost.setdefault("amount", 0.0)
    cost.setdefault("type", "one_time")
    cost.setdefault("dueDate", _today().isoformat())
    cost.setdefault("notes", "")
    cost.setdefault("createdAt", now_iso)
    cost.setdefault("updatedAt", now_iso)

    if cost.get("type") not in ("one_time", "recurring"):
      cost["type"] = "one_time"
    if not isinstance(cost.get("amount"), (int, float)):
      cost["amount"] = 0.0
    due_date = cost.get("dueDate")
    if not isinstance(due_date, str):
      due_date = _today().isoformat()
    cost["dueDate"] = _parse_date(due_date).isoformat()
    if cost.get("notes") is None:
      cost["notes"] = ""

    reminder = cost.setdefault("reminder", {})
    reminder.setdefault("enabled", False)
    reminder.setdefault("daysBefore", 3)
    reminder.setdefault("syncToCalendar", False)
    reminder.setdefault("calendarEventId", None)
    if reminder.get("enabled"):
      reminder_date = reminder.get("reminderDate") or _compute_reminder_date(cost["dueDate"], reminder.get("daysBefore", 3))
    else:
      reminder_date = None
    if not isinstance(reminder.get("daysBefore"), int):
      reminder["daysBefore"] = 3
    reminder["reminderDate"] = reminder_date
    if not isinstance(cost.get("metadata"), dict):
      cost["metadata"] = {}
    normalized_costs.append(cost)
  costs["entries"] = normalized_costs

  _ensure_staff_costs(finance, staffing, now_iso, sync_calendar=sync_calendar)

  return finance


def _get_finance_state() -> Dict[str, Any]:
  def _mutator(state: Dict[str, Any]) -> None:
    finance_state = state.get("finance") or default_finance_state()
    staffing = state.setdefault("staffing", default_staffing_state())
    finance = _normalize_finance(finance_state, staffing, state=state)
    now_iso = _iso(_utc_now())
    finance["updatedAt"] = finance.get("updatedAt", now_iso)
    changed = _ensure_month_rollover(finance, state)
    _ensure_staff_costs(finance, staffing, now_iso, sync_calendar=False)
    _maybe_send_payroll_email(finance, state)
    if changed:
      finance["updatedAt"] = now_iso
    state["finance"] = finance

  updated_state = update_state(_mutator)
  finance = updated_state.get("finance", default_finance_state())
  staffing = updated_state.get("staffing")
  return _normalize_finance(finance, staffing, state=updated_state)


def _mutate_finance(mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
  def _apply(state: Dict[str, Any]) -> None:
    finance_state = state.get("finance")
    if finance_state is None:
      finance_state = default_finance_state()
    staffing = state.setdefault("staffing", default_staffing_state())
    finance = _normalize_finance(finance_state, staffing, state=state)
    _ensure_month_rollover(finance, state)
    mutator(finance)
    now_iso = _iso(_utc_now())
    finance["updatedAt"] = now_iso
    _ensure_staff_costs(finance, staffing, now_iso, sync_calendar=True)
    _maybe_send_payroll_email(finance, state)
    state["finance"] = finance

  updated_state = update_state(_apply)
  staffing_state = updated_state.get("staffing")
  return _normalize_finance(updated_state.get("finance", default_finance_state()), staffing_state, state=updated_state)


class FuelEarningsPayload(BaseModel):
  pricePerGallon: float | None = Field(default=None, ge=0.0)
  litersSold: float | None = Field(default=None, ge=0.0)
  mode: DataMode | None = None


class ShopEarningsPayload(BaseModel):
  amount: float = Field(ge=0.0)
  mode: DataMode = "manual"
  notes: str | None = None


class IncomeSourcePayload(BaseModel):
  name: str = Field(min_length=1, max_length=120)
  amount: float = Field(ge=0.0)
  category: str | None = Field(default=None, max_length=80)
  source: DataMode = "manual"


class IncomeSourceUpdatePayload(BaseModel):
  name: str | None = Field(default=None, min_length=1, max_length=120)
  amount: float | None = Field(default=None, ge=0.0)
  category: str | None = Field(default=None, max_length=80)
  source: DataMode | None = None


class CostReminderPayload(BaseModel):
  enabled: bool = False
  daysBefore: int = Field(default=3, ge=0, le=365)
  syncToCalendar: bool = False


class CostEntryPayload(BaseModel):
  name: str = Field(min_length=1, max_length=160)
  amount: float = Field(ge=0.0)
  type: RecurringType = "one_time"
  dueDate: date
  notes: str | None = Field(default=None, max_length=400)
  reminder: CostReminderPayload | None = None
  metadata: Dict[str, Any] | None = None


class CostEntryUpdatePayload(BaseModel):
  name: str | None = Field(default=None, min_length=1, max_length=160)
  amount: float | None = Field(default=None, ge=0.0)
  type: RecurringType | None = None
  dueDate: date | None = None
  notes: str | None = Field(default=None, max_length=400)
  reminder: CostReminderPayload | None = None
  metadata: Dict[str, Any] | None = None


class FuelEarningsSnapshot(BaseModel):
  fuelType: FuelTypeId
  pricePerGallon: float
  litersSold: float
  totalRevenue: float
  mode: DataMode
  updatedAt: str


class ShopEarningsSnapshot(BaseModel):
  amount: float
  mode: DataMode
  notes: str
  updatedAt: str


class IncomeSource(BaseModel):
  id: str
  name: str
  amount: float
  category: str | None
  source: DataMode
  createdAt: str
  updatedAt: str


class EarningsTotals(BaseModel):
  fuel: float
  shop: float
  other: float
  gross: float


class EarningsSnapshot(BaseModel):
  fuel: list[FuelEarningsSnapshot]
  shop: ShopEarningsSnapshot
  otherIncome: list[IncomeSource]
  totals: EarningsTotals
  updatedAt: str


class CostReminder(BaseModel):
  enabled: bool
  daysBefore: int
  reminderDate: str | None
  syncToCalendar: bool
  calendarEventId: str | None


class CostEntry(BaseModel):
  id: str
  name: str
  amount: float
  type: RecurringType
  dueDate: date
  notes: str
  reminder: CostReminder
  createdAt: str
  updatedAt: str
  source: str | None = None
  metadata: Dict[str, Any] | None = None


class CostTotals(BaseModel):
  recurring: float
  oneTime: float
  overall: float


class CostsSnapshot(BaseModel):
  entries: list[CostEntry]
  totals: CostTotals
  updatedAt: str


class ReminderPreview(BaseModel):
  costId: str
  name: str
  amount: float
  dueDate: date
  daysUntilDue: int
  reminderDate: date | None
  daysUntilReminder: int | None


class RemindersSnapshot(BaseModel):
  upcoming: list[ReminderPreview]
  generatedAt: str


class ProfitSnapshot(BaseModel):
  netProfit: float
  grossRevenue: float
  totalCosts: float
  marginPercent: float
  currency: str
  isNegative: bool


class ComparisonPoint(BaseModel):
  label: str
  value: float


class ProfitChart(BaseModel):
  comparison: list[ComparisonPoint]


class PriceRange(BaseModel):
  min: float
  max: float


class MonthlyFuelEarnings(BaseModel):
  fuelType: str
  gallons: float
  liters: float
  revenue: float
  pricePerGallonRange: PriceRange


class MonthlyFuelEarningsResponse(BaseModel):
  month: str
  totalGallonsSold: float
  totalLitersSold: float
  totalRevenue: float
  totalRevenueRaw: float
  transactions: int
  revenueByFuelType: Dict[str, float]
  rawRevenueByFuelType: Dict[str, float]
  averagePricePerGallonByType: Dict[str, float]
  pricePerGallonRangeByType: Dict[str, PriceRange]
  fuels: list[MonthlyFuelEarnings]


class FinanceSummaryResponse(BaseModel):
  generatedAt: str
  earnings: EarningsSnapshot
  costs: CostsSnapshot
  reminders: RemindersSnapshot
  profit: ProfitSnapshot
  chart: ProfitChart


def _format_monthly_response(payload: sales_data.MonthlyEarnings) -> MonthlyFuelEarningsResponse:
  fuels: list[MonthlyFuelEarnings] = []
  revenue_by_fuel = payload.get("revenue_by_fuel_type", {})
  gallons_by_fuel = payload.get("gallons_by_fuel_type", {})
  avg_price_gal = payload.get("average_price_per_gallon_by_type", {})
  raw_ranges = payload.get("price_per_gallon_range_by_type", {})
  range_price_gal = raw_ranges if isinstance(raw_ranges, dict) else {}

  for fuel, revenue in revenue_by_fuel.items():
    gallons = gallons_by_fuel.get(fuel, 0.0)
    range_entry = range_price_gal.get(fuel, {}) if isinstance(range_price_gal, dict) else {}
    min_price = float(range_entry.get("min", 0.0)) if isinstance(range_entry, dict) else 0.0
    max_price = float(range_entry.get("max", 0.0)) if isinstance(range_entry, dict) else 0.0
    fuels.append(MonthlyFuelEarnings(
      fuelType=fuel,
      gallons=gallons,
      liters=gallons * sales_data.GALLON_TO_LITER,
      revenue=revenue,
      pricePerGallonRange=PriceRange(min=min_price, max=max_price),
    ))

  return MonthlyFuelEarningsResponse(
    month=payload.get("month") or _current_sales_month_key(),
    totalGallonsSold=payload.get("total_gallons_sold", 0.0),
    totalLitersSold=payload.get("total_liters_sold", 0.0),
    totalRevenue=payload.get("total_revenue", 0.0),
    totalRevenueRaw=payload.get("total_revenue_raw", 0.0),
    transactions=payload.get("transactions", 0),
    revenueByFuelType=revenue_by_fuel,
    rawRevenueByFuelType=payload.get("raw_revenue_by_fuel_type", {}),
    averagePricePerGallonByType=avg_price_gal,
    pricePerGallonRangeByType={
      fuel: PriceRange(min=float(entry.get("min", 0.0)), max=float(entry.get("max", 0.0)))
      for fuel, entry in range_price_gal.items()
      if isinstance(entry, dict)
    },
    fuels=fuels,
  )


def _sync_calendar_event(cost: Dict[str, Any]) -> None:
  reminder = cost.get("reminder", {})
  enabled = reminder.get("enabled", False)
  sync_to_calendar = reminder.get("syncToCalendar", False)
  event_id = reminder.get("calendarEventId") or f"finance-cost-{cost['id']}"

  if enabled and sync_to_calendar:
    due_date = _parse_date(cost["dueDate"])
    start_dt = datetime.combine(due_date, time(hour=15, minute=0, tzinfo=timezone.utc))
    end_dt = start_dt + timedelta(hours=1)
    upsert_event({
      "id": event_id,
      "title": f"Pago pendiente: {cost['name']}",
      "description": cost.get("notes", ""),
      "start": _iso(start_dt),
      "end": _iso(end_dt),
      "allDay": False,
      "source": "finance",
      "metadata": {
        "amount": cost.get("amount", 0.0),
        "reminderDate": reminder.get("reminderDate"),
        "type": cost.get("type", "one_time"),
      }
    })
    reminder["calendarEventId"] = event_id
  else:
    if reminder.get("calendarEventId"):
      delete_event(reminder["calendarEventId"])
    reminder["calendarEventId"] = None


def _build_summary(finance: Dict[str, Any]) -> FinanceSummaryResponse:
  sales_state = sales_data.load_sales_state()
  fingerprint = sales_state.get("sourceFingerprint")
  cache_key = f"{finance.get('updatedAt')}:{fingerprint}"
  cached = _FINANCE_SUMMARY_CACHE.get("response") if _FINANCE_SUMMARY_CACHE.get("key") == cache_key else None
  if cached is not None:
    return cached

  now_iso = _iso(_utc_now())
  currency = finance.get("currency", "GTQ")
  earnings_state = finance.get("earnings", {})
  sales_today = sales_data.effective_today(sales_state.get("transactions", []))
  current_month = _current_month_key(sales_today)
  monthly_sales = sales_data.monthly_earnings(current_month)
  revenue_by_fuel = monthly_sales.get("revenue_by_fuel_type", {})
  raw_revenue_by_fuel = monthly_sales.get("raw_revenue_by_fuel_type", {})
  gallons_by_fuel = monthly_sales.get("gallons_by_fuel_type", {})
  avg_price_gal_by_fuel = monthly_sales.get("average_price_per_gallon_by_type", {})

  gasoline_stats = build_gasoline_stats(with_side_effects=False)
  gasoline_updated_at = _iso(gasoline_stats.generatedAt)
  gasoline_fuel_map = {fuel.id: fuel for fuel in gasoline_stats.fuelTypes}

  fuel_snapshot: list[FuelEarningsSnapshot] = []
  fuel_total = 0.0
  fuel_ids = [fuel.id for fuel in gasoline_stats.fuelTypes]

  for fuel_id in fuel_ids:
    entry = earnings_state.get("fuel", {}).get(fuel_id, {})
    stats = gasoline_fuel_map.get(fuel_id)
    mode = entry.get("mode", "synced")
    if mode not in ("manual", "synced"):
      mode = "synced"

    raw_price = entry.get("pricePerGallon", 0.0)
    raw_gallons = entry.get("litersSold", 0.0)
    price = float(raw_price) if isinstance(raw_price, (int, float)) else 0.0
    gallons = float(raw_gallons) if isinstance(raw_gallons, (int, float)) else 0.0
    updated_at = str(entry.get("updatedAt", finance.get("updatedAt", now_iso)))

    # Sync with monthly sales where available
    synced_revenue = None
    pos_revenue_available = fuel_id in raw_revenue_by_fuel
    if mode == "synced":
      price = float(avg_price_gal_by_fuel.get(fuel_id, price))
      gallons = float(gallons_by_fuel.get(fuel_id, 0.0))
      synced_revenue = revenue_by_fuel.get(fuel_id)
      updated_at = gasoline_updated_at

    price = round(price, 4)
    gallons = round(gallons, 4)
    if pos_revenue_available:
      total = round(float(raw_revenue_by_fuel.get(fuel_id, 0.0)), 4)
    elif isinstance(synced_revenue, (int, float)):
      total = round(float(synced_revenue), 4)
    else:
      total = round(price * gallons, 4)
    fuel_total += total

    fuel_snapshot.append(FuelEarningsSnapshot(
      fuelType=fuel_id,  # type: ignore[arg-type]
      pricePerGallon=price,
      litersSold=gallons,
      totalRevenue=total,
      mode=mode,
      updatedAt=updated_at
    ))

  shop_state = earnings_state.get("shop", {})
  shop_amount = float(shop_state.get("amount", 0.0))
  shop_snapshot = ShopEarningsSnapshot(
    amount=shop_amount,
    mode=shop_state.get("mode", "manual"),
    notes=shop_state.get("notes") or "",
    updatedAt=str(shop_state.get("updatedAt", finance.get("updatedAt", now_iso)))
  )

  other_income_snapshot: list[IncomeSource] = []
  other_total = 0.0
  for entry in earnings_state.get("otherIncome", []):
    amount = float(entry.get("amount", 0.0))
    other_total += amount
    other_income_snapshot.append(IncomeSource(
      id=str(entry.get("id")),
      name=str(entry.get("name", "")),
      amount=amount,
      category=entry.get("category"),
      source=entry.get("source", "manual"),
      createdAt=str(entry.get("createdAt", now_iso)),
      updatedAt=str(entry.get("updatedAt", now_iso))
    ))

  gross = fuel_total + shop_amount + other_total

  costs_state = finance.get("costs", {})
  cost_entries: list[CostEntry] = []
  recurring_total = 0.0
  one_time_total = 0.0
  for entry in costs_state.get("entries", []):
    due_date = _parse_date(str(entry.get("dueDate", _today().isoformat())))
    if entry.get("source") != "staff" and due_date.strftime("%Y-%m") != current_month:
      continue

    amount = float(entry.get("amount", 0.0))
    if entry.get("type") == "recurring":
      recurring_total += amount
    else:
      one_time_total += amount

    reminder = entry.get("reminder", {})
    reminder_date = reminder.get("reminderDate")
    cost_entries.append(CostEntry(
      id=str(entry.get("id")),
      name=str(entry.get("name", "")),
      amount=amount,
      type=entry.get("type", "one_time"),
      dueDate=due_date,
      notes=str(entry.get("notes", "")),
      reminder=CostReminder(
        enabled=bool(reminder.get("enabled", False)),
        daysBefore=int(reminder.get("daysBefore", 3)),
        reminderDate=reminder_date,
        syncToCalendar=bool(reminder.get("syncToCalendar", False)),
        calendarEventId=reminder.get("calendarEventId"),
      ),
      createdAt=str(entry.get("createdAt", now_iso)),
      updatedAt=str(entry.get("updatedAt", now_iso)),
      source=entry.get("source")
    ))

  total_costs = recurring_total + one_time_total
  net_profit = gross - total_costs
  margin = (net_profit / gross * 100) if gross > 0 else 0.0

  reminders: list[ReminderPreview] = []
  today = _today()
  for entry in cost_entries:
    if not entry.reminder.enabled:
      continue
    due_date = entry.dueDate
    reminder_date_str = entry.reminder.reminderDate
    reminder_date = date.fromisoformat(reminder_date_str) if reminder_date_str else None
    days_until_due = (due_date - today).days
    days_until_reminder = (reminder_date - today).days if reminder_date else None
    if days_until_due < -30:
      # Skip very old items
      continue
    reminders.append(ReminderPreview(
      costId=entry.id,
      name=entry.name,
      amount=entry.amount,
      dueDate=due_date,
      daysUntilDue=days_until_due,
      reminderDate=reminder_date,
      daysUntilReminder=days_until_reminder
    ))

  reminders.sort(key=lambda item: (item.reminderDate or item.dueDate))

  response = FinanceSummaryResponse(
    generatedAt=now_iso,
    earnings=EarningsSnapshot(
      fuel=fuel_snapshot,
      shop=shop_snapshot,
      otherIncome=other_income_snapshot,
      totals=EarningsTotals(
        fuel=fuel_total,
        shop=shop_amount,
        other=other_total,
        gross=gross
      ),
      updatedAt=str(earnings_state.get("updatedAt", now_iso))
    ),
    costs=CostsSnapshot(
      entries=cost_entries,
      totals=CostTotals(
        recurring=recurring_total,
        oneTime=one_time_total,
        overall=total_costs
      ),
      updatedAt=str(costs_state.get("updatedAt", now_iso))
    ),
    reminders=RemindersSnapshot(
      upcoming=reminders,
      generatedAt=now_iso
    ),
    profit=ProfitSnapshot(
      netProfit=net_profit,
      grossRevenue=gross,
      totalCosts=total_costs,
      marginPercent=margin,
      currency=currency,
      isNegative=net_profit < 0
    ),
    chart=ProfitChart(
      comparison=[
        ComparisonPoint(label="earnings", value=gross),
        ComparisonPoint(label="costs", value=total_costs)
      ]
    )
  )
  _FINANCE_SUMMARY_CACHE.update({"key": cache_key, "response": response})
  return response


@router.get("/fuel-earnings", response_model=MonthlyFuelEarningsResponse | list[MonthlyFuelEarningsResponse])
def get_monthly_fuel_earnings(
  month: str | None = None,
  from_month: str | None = None,
  to_month: str | None = None,
) -> MonthlyFuelEarningsResponse | list[MonthlyFuelEarningsResponse]:
  if month:
    month_key = _parse_month_key(month)
    return _format_monthly_response(sales_data.monthly_earnings(month_key))

  if from_month and to_month:
    start = _parse_month_key(from_month)
    end = _parse_month_key(to_month)
    payloads = sales_data.monthly_earnings_range(start, end)
    return [_format_monthly_response(item) for item in payloads]

  current_month = _current_sales_month_key()
  return _format_monthly_response(sales_data.monthly_earnings(current_month))


def _mutate_and_summarize(mutator: Callable[[Dict[str, Any]], None]) -> FinanceSummaryResponse:
  finance = _mutate_finance(mutator)
  return _build_summary(finance)


@router.get("", response_model=FinanceSummaryResponse)
def get_finance_summary() -> FinanceSummaryResponse:
  finance = _get_finance_state()
  return _build_summary(finance)


@router.put("/fuel/{fuel_id}", response_model=FinanceSummaryResponse)
def update_fuel_earnings(fuel_id: FuelTypeId, payload: FuelEarningsPayload) -> FinanceSummaryResponse:
  if not payload.model_dump(exclude_unset=True):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No hay cambios para guardar")

  try:
    normalized_id = fuel_types.validate_fuel_id(fuel_id)
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

  override_instruction: Dict[str, Any] = {"action": None, "value": None}

  def _mutator(finance: Dict[str, Any]) -> None:
    earnings = finance.setdefault("earnings", {})
    fuel = earnings.setdefault("fuel", {})
    target = fuel.setdefault(normalized_id, {})
    if payload.pricePerGallon is not None:
      target["pricePerGallon"] = payload.pricePerGallon
    if payload.litersSold is not None:
      target["litersSold"] = payload.litersSold
    if payload.mode is not None:
      target["mode"] = payload.mode
    target["updatedAt"] = _iso(_utc_now())
    earnings["updatedAt"] = target["updatedAt"]

    final_price = float(target.get("pricePerGallon", 0.0))
    mode = target.get("mode")
    if payload.mode == "synced":
      override_instruction["action"] = "clear"
      override_instruction["value"] = None
    elif payload.pricePerGallon is not None or payload.mode == "manual":
      override_instruction["action"] = "set"
      override_instruction["value"] = final_price

  finance_state = _mutate_finance(_mutator)

  if override_instruction["action"] == "set":
    price_value = override_instruction.get("value")
    if isinstance(price_value, (int, float)):
      set_fuel_price_override(normalized_id, float(price_value))
  elif override_instruction["action"] == "clear":
    set_fuel_price_override(normalized_id, None)

  return _build_summary(finance_state)


@router.put("/shop", response_model=FinanceSummaryResponse)
def update_shop_earnings(payload: ShopEarningsPayload) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    earnings = finance.setdefault("earnings", {})
    shop = earnings.setdefault("shop", {})
    shop["amount"] = payload.amount
    shop["mode"] = payload.mode
    shop["notes"] = payload.notes or ""
    shop["updatedAt"] = _iso(_utc_now())
    earnings["updatedAt"] = shop["updatedAt"]

  return _mutate_and_summarize(_mutator)


@router.post("/other-income", response_model=FinanceSummaryResponse, status_code=status.HTTP_201_CREATED)
def create_other_income(payload: IncomeSourcePayload) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    earnings = finance.setdefault("earnings", {})
    entries = earnings.setdefault("otherIncome", [])
    now_iso = _iso(_utc_now())
    entries.append({
      "id": str(uuid.uuid4()),
      "name": payload.name,
      "amount": payload.amount,
      "category": payload.category,
      "source": payload.source,
      "createdAt": now_iso,
      "updatedAt": now_iso,
    })
    earnings["updatedAt"] = now_iso

  return _mutate_and_summarize(_mutator)


@router.put("/other-income/{income_id}", response_model=FinanceSummaryResponse)
def update_other_income(income_id: str, payload: IncomeSourceUpdatePayload) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    earnings = finance.setdefault("earnings", {})
    entries = earnings.setdefault("otherIncome", [])
    data = payload.model_dump(exclude_unset=True)
    if not data:
      raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No hay cambios para guardar")
    for entry in entries:
      if entry.get("id") == income_id:
        if "name" in data:
          entry["name"] = data["name"]
        if "amount" in data:
          entry["amount"] = data["amount"]
        if "category" in data:
          entry["category"] = data["category"]
        if "source" in data:
          entry["source"] = data["source"]
        entry["updatedAt"] = _iso(_utc_now())
        earnings["updatedAt"] = entry["updatedAt"]
        break
    else:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Income source not found")

  return _mutate_and_summarize(_mutator)


@router.delete("/other-income/{income_id}", response_model=FinanceSummaryResponse)
def delete_other_income(income_id: str) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    earnings = finance.setdefault("earnings", {})
    entries = earnings.setdefault("otherIncome", [])
    filtered = [entry for entry in entries if entry.get("id") != income_id]
    if len(filtered) == len(entries):
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Income source not found")
    earnings["otherIncome"] = filtered
    earnings["updatedAt"] = _iso(_utc_now())

  return _mutate_and_summarize(_mutator)


def _upsert_cost_entry(
  entries: list[Dict[str, Any]],
  cost_id: str,
  apply_changes: Callable[[Dict[str, Any]], None]
) -> None:
  for entry in entries:
    if entry.get("id") == cost_id:
      apply_changes(entry)
      entry["updatedAt"] = _iso(_utc_now())
      reminder = entry.setdefault("reminder", {})
      if reminder.get("enabled"):
        reminder["reminderDate"] = _compute_reminder_date(entry["dueDate"], reminder.get("daysBefore", 3))
      else:
        reminder["reminderDate"] = None
      if not isinstance(entry.get("metadata"), dict):
        entry["metadata"] = {}
      _maybe_fire_due_notification(entry)
      _sync_calendar_event(entry)
      return
  raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost entry not found")


@router.post("/costs", response_model=FinanceSummaryResponse, status_code=status.HTTP_201_CREATED)
def create_cost_entry(payload: CostEntryPayload) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    costs = finance.setdefault("costs", {})
    entries = costs.setdefault("entries", [])
    now_iso = _iso(_utc_now())
    cost_id = str(uuid.uuid4())
    reminder_payload = payload.reminder or CostReminderPayload()
    reminder_dict = {
      "enabled": reminder_payload.enabled,
      "daysBefore": reminder_payload.daysBefore,
      "syncToCalendar": reminder_payload.syncToCalendar,
      "calendarEventId": None,
    }
    if reminder_payload.enabled:
      reminder_dict["reminderDate"] = _compute_reminder_date(payload.dueDate.isoformat(), reminder_payload.daysBefore)
    else:
      reminder_dict["reminderDate"] = None

    cost_entry = {
      "id": cost_id,
      "name": payload.name,
      "amount": payload.amount,
      "type": payload.type,
      "dueDate": payload.dueDate.isoformat(),
      "notes": payload.notes or "",
      "reminder": reminder_dict,
      "createdAt": now_iso,
      "updatedAt": now_iso,
    }
    entries.append(cost_entry)
    costs["updatedAt"] = now_iso
    if reminder_payload.enabled:
      _maybe_fire_due_notification(cost_entry)
      _sync_calendar_event(cost_entry)

  return _mutate_and_summarize(_mutator)


@router.put("/costs/{cost_id}", response_model=FinanceSummaryResponse)
def update_cost_entry(cost_id: str, payload: CostEntryUpdatePayload) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    costs = finance.setdefault("costs", {})
    entries = costs.setdefault("entries", [])

    data = payload.model_dump(exclude_unset=True)
    if not data:
      raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No hay cambios para guardar")

    def _apply(entry: Dict[str, Any]) -> None:
      if "name" in data:
        entry["name"] = data["name"]
      if "amount" in data:
        entry["amount"] = data["amount"]
      if "type" in data:
        entry["type"] = data["type"]
      if "dueDate" in data:
        entry["dueDate"] = data["dueDate"].isoformat() if isinstance(data["dueDate"], date) else str(data["dueDate"])
      if "notes" in data:
        entry["notes"] = data["notes"]
      if "reminder" in data:
        reminder = entry.setdefault("reminder", {})
        reminder_patch = data["reminder"]
        if "enabled" in reminder_patch:
          reminder["enabled"] = reminder_patch["enabled"]
        if "daysBefore" in reminder_patch:
          reminder["daysBefore"] = reminder_patch["daysBefore"]
        if "syncToCalendar" in reminder_patch:
          reminder["syncToCalendar"] = reminder_patch["syncToCalendar"]
        reminder.setdefault("enabled", False)
        reminder.setdefault("daysBefore", 3)
        reminder.setdefault("syncToCalendar", False)
        if reminder["enabled"]:
          reminder["reminderDate"] = _compute_reminder_date(entry["dueDate"], reminder["daysBefore"])
        else:
          reminder["reminderDate"] = None
      if "metadata" in data:
        meta = entry.setdefault("metadata", {})
        meta_patch = data["metadata"] or {}
        meta.update(meta_patch)
        if any(key in meta_patch for key in ("bonus", "discount", "resetFrequency", "hours")):
          reset_freq = str(meta.get("resetFrequency") or meta.get("payFrequency") or "monthly")
          meta["resetFrequency"] = reset_freq
          meta["resetPeriod"] = _reset_period_key(reset_freq, _today())

    _upsert_cost_entry(entries, cost_id, _apply)
    costs["updatedAt"] = _iso(_utc_now())

  return _mutate_and_summarize(_mutator)


@router.delete("/costs/{cost_id}", response_model=FinanceSummaryResponse)
def delete_cost_entry(cost_id: str) -> FinanceSummaryResponse:
  def _mutator(finance: Dict[str, Any]) -> None:
    costs = finance.setdefault("costs", {})
    entries = costs.setdefault("entries", [])
    remaining = []
    removed_entry: Dict[str, Any] | None = None
    for entry in entries:
      if entry.get("id") == cost_id:
        removed_entry = entry
      else:
        remaining.append(entry)
    if removed_entry is None:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost entry not found")
    entries[:] = remaining
    costs["entries"] = entries
    costs["updatedAt"] = _iso(_utc_now())

    reminder = removed_entry.get("reminder", {})
    if reminder.get("calendarEventId"):
      delete_event(reminder["calendarEventId"])

  return _mutate_and_summarize(_mutator)
