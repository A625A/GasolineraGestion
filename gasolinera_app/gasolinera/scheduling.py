"""
Scheduling utilities and calendar integration scaffolding.

The production system is expected to integrate with Google Calendar (or an
internal tool) for deliveries, maintenance windows, and staffing events.  The
classes defined here provide a consistent interface while keeping the actual
API calls behind a thin wrapper that can be mocked during tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional


@dataclass
class CalendarEvent:
    """Canonical representation for events exchanged with calendar backends."""

    summary: str
    start: datetime
    end: datetime
    description: str | None = None
    metadata: Dict[str, str] = field(default_factory=dict)


class CalendarClient:
    """
    Abstract calendar client.

    Sub-classes should implement the CRUD operations for the provider in use.
    """

    def create_event(self, event: CalendarEvent) -> CalendarEvent:  # pragma: no cover - interface stub
        raise NotImplementedError

    def list_events(self, start: datetime, end: datetime) -> List[CalendarEvent]:  # pragma: no cover - interface stub
        raise NotImplementedError

    def delete_event(self, event_id: str) -> None:  # pragma: no cover - interface stub
        raise NotImplementedError


class GoogleCalendarClient(CalendarClient):
    """
    Placeholder Google Calendar implementation.

    The real integration would use `google-api-python-client` with OAuth
    credentials.  For now we keep events in-memory so other modules can still
    exercise the workflow end-to-end.
    """

    def __init__(self):
        self._events: Dict[str, CalendarEvent] = {}

    def create_event(self, event: CalendarEvent) -> CalendarEvent:
        event_id = f"event-{len(self._events) + 1}"
        self._events[event_id] = event
        event.metadata["id"] = event_id
        return event

    def list_events(self, start: datetime, end: datetime) -> List[CalendarEvent]:
        return [event for event in self._events.values() if start <= event.start <= end]

    def delete_event(self, event_id: str) -> None:
        self._events.pop(event_id, None)


class DeliveryScheduler:
    """
    High-level orchestration for delivery scheduling.

    The scheduler understands domain-specific event templates such as fuel
    deliveries, maintenance windows, and expected peak traffic hours.
    """

    def __init__(self, calendar_client: CalendarClient):
        self.calendar_client = calendar_client

    def schedule_fuel_delivery(
        self,
        summary: str,
        start: datetime,
        duration: timedelta = timedelta(hours=2),
        *,
        station_id: str,
        notes: str | None = None,
    ) -> CalendarEvent:
        event = CalendarEvent(
            summary=summary,
            start=start,
            end=start + duration,
            description=notes,
            metadata={"category": "delivery", "station_id": station_id},
        )
        return self.calendar_client.create_event(event)

    def schedule_maintenance(
        self,
        station_id: str,
        start: datetime,
        *,
        duration: timedelta = timedelta(hours=4),
        description: str | None = "Routine maintenance",
    ) -> CalendarEvent:
        summary = f"Maintenance - Station {station_id}"
        event = CalendarEvent(
            summary=summary,
            start=start,
            end=start + duration,
            description=description,
            metadata={"category": "maintenance", "station_id": station_id},
        )
        return self.calendar_client.create_event(event)

    def schedule_peak_alerts(
        self,
        peaks: Iterable[datetime],
        *,
        station_id: str,
        lead_time: timedelta = timedelta(minutes=30),
    ) -> List[CalendarEvent]:
        """Create reminder events ahead of predicted peak hours."""

        events: List[CalendarEvent] = []
        for peak in peaks:
            alert_start = peak - lead_time
            event = CalendarEvent(
                summary=f"Prepare for peak traffic (Station {station_id})",
                start=alert_start,
                end=peak,
                description="Review staffing and pump availability before rush.",
                metadata={"category": "peak_alert", "station_id": station_id},
            )
            events.append(self.calendar_client.create_event(event))
        return events


def suggest_delivery_window(last_delivery: datetime, usage_rate_per_day: float, reorder_threshold: float) -> datetime:
    """
    Estimate the next delivery slot based on average consumption.

    The heuristic assumes a linear consumption rate.  It returns the datetime at
    which the remaining inventory is expected to hit the reorder threshold.
    """

    if usage_rate_per_day <= 0:
        raise ValueError("Usage rate must be positive.")
    days_until_threshold = reorder_threshold / usage_rate_per_day
    return last_delivery + timedelta(days=days_until_threshold)
