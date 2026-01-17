from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Mapping, Sequence


SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
DEFAULT_SENDER_NAME = os.getenv("EMAIL_SENDER_NAME", "Gasolinera Inteligente").strip()


class EmailDeliveryError(RuntimeError):
  """Raised when the email service cannot deliver a message."""


def credentials_available() -> bool:
  return bool(EMAIL_USERNAME and EMAIL_PASSWORD)


def _format_address(address: str, sender_name: str | None = None) -> str:
  return f"{sender_name} <{address}>" if sender_name else address


def build_message(
  *,
  recipient: str,
  subject: str,
  body: str,
  sender: str | None = None,
  sender_name: str | None = None,
  reply_to: str | None = None,
  subtype: str = "plain",
  headers: Mapping[str, str] | None = None,
) -> EmailMessage:
  message = EmailMessage()
  message["To"] = recipient
  message["From"] = _format_address(sender or EMAIL_USERNAME, sender_name or DEFAULT_SENDER_NAME)
  message["Subject"] = subject
  if reply_to:
    message["Reply-To"] = reply_to
  if headers:
    for key, value in headers.items():
      message[key] = value
  message.set_content(body, subtype=subtype)
  return message


def send_email(
  *,
  recipients: Sequence[str] | str,
  subject: str,
  body: str,
  sender: str | None = None,
  sender_name: str | None = None,
  reply_to: str | None = None,
  subtype: str = "plain",
  headers: Mapping[str, str] | None = None,
) -> None:
  if not credentials_available():
    raise EmailDeliveryError("Email credentials are not configured.")

  if isinstance(recipients, str):
    target_recipients: Sequence[str] = [recipients]
  else:
    target_recipients = recipients

  context = ssl.create_default_context()
  message = build_message(
    recipient=", ".join(target_recipients),
    subject=subject,
    body=body,
    sender=sender,
    sender_name=sender_name,
    reply_to=reply_to,
    subtype=subtype,
    headers=headers,
  )

  last_error: Exception | None = None
  for attempt in range(1, 4):
    try:
      with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.send_message(message)
      return
    except Exception as exc:
      last_error = exc
      print(f"[EMAIL_DELIVERY_RETRY] attempt={attempt} failed: {exc}")
  raise EmailDeliveryError("Failed to deliver email.") from last_error
