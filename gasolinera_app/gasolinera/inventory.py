"""
Inventory tracking utilities for fuel tanks.

The goal is to offer a simple, testable in-memory representation that can be
swapped for a database-backed repository later on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict


@dataclass
class InventoryRecord:
    station_id: str
    gasoline_type: str
    current_level: float
    capacity: float
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InventoryManager:
    """Manage inventory levels per station and fuel type."""

    def __init__(self):
        self._inventory: Dict[tuple[str, str], InventoryRecord] = {}

    def record_delivery(self, station_id: str, gasoline_type: str, volume: float, capacity: float) -> InventoryRecord:
        key = (station_id, gasoline_type.title())
        record = self._inventory.get(key)
        if record:
            record.current_level = min(record.capacity, record.current_level + volume)
            record.capacity = capacity
            record.updated_at = datetime.now(timezone.utc)
        else:
            record = InventoryRecord(station_id=station_id, gasoline_type=gasoline_type.title(), current_level=volume, capacity=capacity)
            self._inventory[key] = record
        return record

    def consume(self, station_id: str, gasoline_type: str, volume: float) -> InventoryRecord:
        key = (station_id, gasoline_type.title())
        if key not in self._inventory:
            raise KeyError(f"No inventory record for station {station_id} and type {gasoline_type}.")
        record = self._inventory[key]
        record.current_level = max(0.0, record.current_level - volume)
        record.updated_at = datetime.now(timezone.utc)
        return record

    def reorder_needed(self, station_id: str, gasoline_type: str, threshold_ratio: float = 0.2) -> bool:
        key = (station_id, gasoline_type.title())
        record = self._inventory.get(key)
        if not record:
            return True
        return record.current_level <= record.capacity * threshold_ratio

    def get_snapshot(self) -> Dict[tuple[str, str], InventoryRecord]:
        return dict(self._inventory)
