from __future__ import annotations

import uuid
from typing import Any, Dict, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.services.email_client import EmailDeliveryError, credentials_available, send_email
from app.services.email_templates import (
  NotificationTemplate,
  TemplateValidationError,
  render_notification_email,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _validate_email(value: str | None) -> str | None:
  if value is None:
    return None
  email = value.strip()
  if not email or "@" not in email:
    raise ValueError("Invalid email address.")
  local, _, domain = email.partition("@")
  if not local or not domain or "." not in domain:
    raise ValueError("Invalid email address.")
  return email.lower()


class EmailNotificationPayload(BaseModel):
  template: NotificationTemplate
  recipient: str
  locale: Literal["es", "en"] = "es"
  bilingualCopy: bool = True
  senderName: str | None = None
  replyTo: str | None = None
  context: Dict[str, Any] = Field(default_factory=dict)

  @field_validator("recipient", "replyTo")
  @classmethod
  def _ensure_email(cls, value: str | None) -> str | None:
    return _validate_email(value)


class EmailNotificationResponse(BaseModel):
  messageId: str
  template: NotificationTemplate
  recipient: str
  locale: Literal["es", "en"]
  bilingualCopy: bool


@router.post("/email", response_model=EmailNotificationResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_email_notification(payload: EmailNotificationPayload) -> EmailNotificationResponse:
  if not credentials_available():
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail="Email service is not configured.",
    )

  try:
    subject, body = render_notification_email(
      payload.template,
      payload.context,
      locale=payload.locale,
      bilingual=payload.bilingualCopy,
    )
  except TemplateValidationError as exc:
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail=str(exc),
    ) from exc

  try:
    send_email(
      recipients=payload.recipient,
      subject=subject,
      body=body,
      sender_name=payload.senderName,
      reply_to=str(payload.replyTo) if payload.replyTo else None,
    )
  except EmailDeliveryError as exc:
    raise HTTPException(
      status_code=status.HTTP_502_BAD_GATEWAY,
      detail=str(exc),
    ) from exc

  return EmailNotificationResponse(
    messageId=str(uuid.uuid4()),
    template=payload.template,
    recipient=payload.recipient,
    locale=payload.locale,
    bilingualCopy=payload.bilingualCopy,
  )
