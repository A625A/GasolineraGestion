"""
Notification scaffolding to alert stakeholders about supply issues or events.

In production these notifications would be delivered through email, SMS,
workplace chat bots, or push notifications.  The module keeps the integration
points flexible so the delivery mechanism can be chosen later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Protocol


@dataclass
class NotificationMessage:
    """Domain-level notification payload."""

    subject: str
    body: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = "general"


class NotificationBackend(Protocol):
    """Protocol for notification adapters."""

    def send(self, message: NotificationMessage) -> None:
        ...


class NotificationService:
    """
    Dispatcher that can fan out notifications to multiple backends.

    Backends can be anything implementing the :class:`NotificationBackend`
    protocol (email, SMS, Slack, etc.).  For now the project ships with a
    console backend so the flow can be demoed without credentials.
    """

    def __init__(self, backends: Iterable[NotificationBackend]):
        self.backends: List[NotificationBackend] = list(backends)

    def send(self, message: NotificationMessage) -> None:
        for backend in self.backends:
            backend.send(message)


class ConsoleNotifier:
    """Simple backend that prints notifications to stdout."""

    def send(self, message: NotificationMessage) -> None:
        print(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.category.upper()}: {message.subject}\n{message.body}\n")


class EmailNotifier:
    """
    Placeholder email backend.

    In a real deployment this class would rely on an SMTP server or a cloud
    provider such as SendGrid.  The ``send`` method currently just logs the
    event to demonstrate the invocation site.
    """

    def __init__(self, sender: str, recipient: str):
        self.sender = sender
        self.recipient = recipient

    def send(self, message: NotificationMessage) -> None:
        email_payload = {
            "from": self.sender,
            "to": self.recipient,
            "subject": message.subject,
            "body": message.body,
        }
        print(f"[EMAIL -> {self.recipient}] {email_payload}")  # pragma: no cover - placeholder output
