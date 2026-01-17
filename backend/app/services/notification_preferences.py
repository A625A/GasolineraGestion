from __future__ import annotations

from typing import Any, Dict

from app.services.state import load_state


def _normalize_lead_days(value: Any, default: int = 3, *, minimum: int = 0, maximum: int = 30) -> int:
  try:
    lead = int(value)
  except (TypeError, ValueError):
    lead = default
  return max(minimum, min(lead, maximum))


def get_email_notification_config(state: Dict[str, Any] | None = None) -> tuple[str, int] | None:
  if state is None:
    state = load_state()
  settings = state.get("settings", {})
  contact = settings.get("contact", {})
  preferences = settings.get("notificationPreferences", {})

  email = contact.get("email")
  email_verified = contact.get("emailVerified", False)
  gmail_enabled = preferences.get("gmail", False)

  if not (email and email_verified and gmail_enabled):
    return None

  lead_days = _normalize_lead_days(preferences.get("leadDaysBefore", 3))
  return email, lead_days
