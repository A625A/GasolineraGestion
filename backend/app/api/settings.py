from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, FieldValidationInfo, field_validator

from app.services.email_client import EmailDeliveryError, send_email
from app.services.state import load_state, update_state

router = APIRouter(prefix="/api/settings", tags=["settings"])

PHONE_PATTERN = re.compile(r"^\+?502[-\s]?\d{4}[-\s]?\d{4}$")


def _now_iso() -> str:
  return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class PersonalInfo(BaseModel):
  fullName: str = Field(min_length=1, max_length=120)
  role: str = Field(min_length=1, max_length=120)
  location: str | None = None


class ContactInfo(BaseModel):
  phone: str
  email: str
  emailVerified: bool = False
  verificationSentAt: str | None = None

  @field_validator("phone")
  @classmethod
  def validate_phone(cls, value: str) -> str:
    if not PHONE_PATTERN.match(value):
      raise ValueError("El número debe ser un teléfono válido de Guatemala (+502 ########)")
    return value

  @field_validator("email")
  @classmethod
  def validate_email(cls, value: str) -> str:
    email = value.strip()
    if not email.lower().endswith("@gmail.com"):
      raise ValueError("Debe proporcionar una cuenta de Gmail válida")
    return email


class NotificationPreferences(BaseModel):
  phone: bool = False
  gmail: bool = False
  inApp: bool = True
  leadDaysBefore: int = Field(3, ge=0, le=30)


class PredictiveSettings(BaseModel):
  confidenceThreshold: float = Field(0.75, ge=0.0, le=1.0)


class SettingsPayload(BaseModel):
  language: Literal["es", "en"] = "es"
  theme: Literal["light", "dark", "system"] = "light"
  personalInfo: PersonalInfo
  contact: ContactInfo
  notificationPreferences: NotificationPreferences
  predictive: PredictiveSettings = PredictiveSettings()

  @field_validator("notificationPreferences")
  @classmethod
  def validate_preferences(
    cls,
    preferences: NotificationPreferences,
    info: FieldValidationInfo
  ) -> NotificationPreferences:
    contact: ContactInfo = info.data.get("contact")  # type: ignore[assignment]
    if preferences.gmail and not (contact and contact.emailVerified):
      raise ValueError("El correo Gmail debe confirmarse antes de habilitar notificaciones")
    return preferences


class SettingsUpdatePayload(BaseModel):
  language: Literal["es", "en"] | None = None
  theme: Literal["light", "dark", "system"] | None = None
  personalInfo: PersonalInfo | None = None
  contact: ContactInfo | None = None
  notificationPreferences: NotificationPreferences | None = None
  predictive: PredictiveSettings | None = None


class SettingsResponse(SettingsPayload):
  updatedAt: str


@router.get("", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
  state = load_state()
  settings = state.get("settings", {})
  if "predictive" not in settings:
    settings["predictive"] = PredictiveSettings().model_dump()
  prefs = settings.get("notificationPreferences", {})
  if "leadDaysBefore" not in prefs:
    prefs["leadDaysBefore"] = NotificationPreferences().leadDaysBefore
    settings["notificationPreferences"] = prefs
  return SettingsResponse(**settings)


def _merge_settings(current: dict, payload: SettingsUpdatePayload | SettingsPayload) -> dict:
  merged = current.copy()
  data = payload.model_dump(exclude_unset=True)

  for key, value in data.items():
    if isinstance(value, dict):
      merged[key] = {**merged.get(key, {}), **value}
    else:
      merged[key] = value

  return merged


def _prepare_settings(current: dict, payload: SettingsUpdatePayload | SettingsPayload) -> dict:
  merged = _merge_settings(current, payload)
  contact = merged.get("contact") or {}
  prefs = merged.get("notificationPreferences") or {}
  if contact and not contact.get("emailVerified") and prefs.get("gmail"):
    prefs = {**prefs, "gmail": False}
    merged["notificationPreferences"] = prefs
  if contact.get("emailVerified"):
    prefs = merged.setdefault("notificationPreferences", {})
    prefs["gmail"] = True
  validated = SettingsPayload(**merged)
  merged = validated.model_dump()
  merged["updatedAt"] = _now_iso()
  return merged


def _compose_verification_email(full_name: str | None, email: str, *, email_changed: bool) -> tuple[str, str]:
  contact_name = full_name or "equipo"
  subject = "Verificación de correo - Gasolinera Inteligente"

  if email_changed:
    intro_es = (
      f"Hemos registrado {email} como nuevo correo para recibir notificaciones automáticas de Gasolinera Inteligente."
    )
    intro_en = (
      f"We have registered {email} as the new email to receive Gasolinera Inteligente notifications."
    )
  else:
    intro_es = f"Hemos verificado que {email} sigue activo para recibir tus notificaciones."
    intro_en = f"We confirmed that {email} is active and ready to receive your notifications."

  body_es = "\n".join([
    f"Hola {contact_name},",
    "",
    intro_es,
    "A partir de ahora recibirás alertas de inventario, recordatorios del calendario y avisos financieros en esta dirección.",
    "",
    "Si no reconoces este cambio, actualiza la configuración de contacto en tu panel de control.",
  ])

  body_en = "\n".join([
    f"Hello {contact_name},",
    "",
    intro_en,
    "From now on you will receive inventory alerts, calendar reminders, and financial notices at this address.",
    "",
    "If you did not request this change, please update your contact preferences in the dashboard.",
  ])

  body = f"{body_es}\n\n--- English version ---\n{body_en}"
  return subject, body



def _persist_settings(payload: SettingsUpdatePayload | SettingsPayload) -> SettingsResponse:
  snapshot = load_state()
  current_settings = snapshot.get("settings", {})
  prepared = _prepare_settings(current_settings, payload)

  previous_contact = current_settings.get("contact", {})
  new_contact = prepared.get("contact", {})
  previous_email = previous_contact.get("email")
  new_email = new_contact.get("email")
  previously_verified = bool(previous_contact.get("emailVerified"))
  now_verified = bool(new_contact.get("emailVerified"))

  email_changed = bool(new_email and new_email != previous_email)
  became_verified = now_verified and not previously_verified
  sent_marker: str | None = None
  if email_changed and new_email:
    subject, body = _compose_verification_email(
      prepared.get("personalInfo", {}).get("fullName"),
      str(new_email),
      email_changed=True,
    )
    try:
      send_email(recipients=str(new_email), subject=subject, body=body)
      sent_marker = _now_iso()
    except EmailDeliveryError as exc:  # pragma: no cover - best effort notification
      print(f"[SETTINGS_EMAIL_ERROR] Failed to send contact confirmation: {exc}")
  elif became_verified and new_email:
    # User confirmed an existing email; send confirmation/welcome message.
    subject, body = _compose_verification_email(
      prepared.get("personalInfo", {}).get("fullName"),
      str(new_email),
      email_changed=False,
    )
    try:
      send_email(recipients=str(new_email), subject=subject, body=body)
      sent_marker = _now_iso()
    except EmailDeliveryError as exc:  # pragma: no cover - best effort notification
      print(f"[SETTINGS_EMAIL_ERROR] Failed to send welcome verification: {exc}")

  def _mutator(state: dict) -> None:
    current = state.get("settings", {})
    merged = _prepare_settings(current, payload)
    # Keep shop inventory default reminder in sync with notification lead days.
    prefs = merged.get("notificationPreferences", {})
    if prefs and "leadDaysBefore" in prefs:
      inventory = state.setdefault("shopInventory", {})
      inventory["alertDaysBefore"] = max(int(prefs.get("leadDaysBefore", 0)), 0)
      inventory["updatedAt"] = _now_iso()
    if email_changed:
      contact = dict(merged.get("contact", {}))
      contact["emailVerified"] = False
      if sent_marker:
        contact["verificationSentAt"] = sent_marker
      else:
        contact.pop("verificationSentAt", None)
      merged["contact"] = contact
    elif became_verified and sent_marker:
      contact = dict(merged.get("contact", {}))
      contact["verificationSentAt"] = sent_marker
      merged["contact"] = contact
    state["settings"] = merged

  state = update_state(_mutator)
  return SettingsResponse(**state["settings"])


@router.put("", response_model=SettingsResponse, status_code=status.HTTP_200_OK)
def put_settings(payload: SettingsPayload) -> SettingsResponse:
  return _persist_settings(payload)


@router.patch("", response_model=SettingsResponse, status_code=status.HTTP_200_OK)
def patch_settings(payload: SettingsUpdatePayload) -> SettingsResponse:
  if not payload.model_dump(exclude_unset=True):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No hay cambios para guardar")
  return _persist_settings(payload)
