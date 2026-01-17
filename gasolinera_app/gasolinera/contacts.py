"""
Contact management utilities for station owners.

Contacts can be grouped into owner-defined categories (e.g. Suppliers, Fleet
Clients, Maintenance).  The module stores data in memory but exposes
serialization helpers so it can be persisted to JSON/CSV later on.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd


@dataclass
class Contact:
    """Represents a contact entry."""

    name: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    notes: str | None = None
    categories: List[str] = field(default_factory=list)
    contact_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def add_category(self, category: str) -> None:
        category = category.strip()
        if category and category not in self.categories:
            self.categories.append(category)

    def remove_category(self, category: str) -> None:
        self.categories = [c for c in self.categories if c.lower() != category.lower()]


class ContactBook:
    """In-memory registry for contacts and their categories."""

    def __init__(self, *, categories: Iterable[str] | None = None):
        self._categories: List[str] = sorted({c.strip() for c in (categories or []) if c})
        self._contacts: Dict[str, Contact] = {}

    @property
    def categories(self) -> List[str]:
        return list(self._categories)

    def add_category(self, category: str) -> None:
        category = category.strip()
        if category and category not in self._categories:
            self._categories.append(category)
            self._categories.sort()

    def remove_category(self, category: str) -> None:
        category_lower = category.lower()
        self._categories = [c for c in self._categories if c.lower() != category_lower]
        for contact in self._contacts.values():
            contact.remove_category(category)

    def rename_category(self, old: str, new: str) -> None:
        old_lower = old.lower()
        if new.strip() == "":
            raise ValueError("New category name must not be empty.")
        if old_lower not in [c.lower() for c in self._categories]:
            raise KeyError(f"Category '{old}' does not exist.")
        self.remove_category(old)
        self.add_category(new)
        for contact in self._contacts.values():
            if any(c.lower() == old_lower for c in contact.categories):
                contact.remove_category(old)
                contact.add_category(new)

    def add_contact(self, contact: Contact) -> Contact:
        if contact.contact_id in self._contacts:
            raise ValueError(f"Contact with id {contact.contact_id} already exists.")
        for category in contact.categories:
            self.add_category(category)
        self._contacts[contact.contact_id] = contact
        return contact

    def create_contact(
        self,
        *,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        company: str | None = None,
        notes: str | None = None,
        categories: Iterable[str] | None = None,
    ) -> Contact:
        contact = Contact(
            name=name.strip(),
            email=email,
            phone=phone,
            company=company,
            notes=notes,
            categories=[c.strip() for c in categories or [] if c.strip()],
        )
        return self.add_contact(contact)

    def get_contact(self, contact_id: str) -> Optional[Contact]:
        return self._contacts.get(contact_id)

    def remove_contact(self, contact_id: str) -> None:
        self._contacts.pop(contact_id, None)

    def list_contacts(self, *, category: str | None = None) -> List[Contact]:
        if category is None:
            return list(self._contacts.values())
        category_lower = category.lower()
        return [contact for contact in self._contacts.values() if any(c.lower() == category_lower for c in contact.categories)]

    def search(self, query: str) -> List[Contact]:
        query_lower = query.lower()
        return [
            contact
            for contact in self._contacts.values()
            if query_lower in contact.name.lower()
            or (contact.company and query_lower in contact.company.lower())
            or (contact.email and query_lower in contact.email.lower())
        ]

    def to_dataframe(self) -> pd.DataFrame:
        records = []
        for contact in self._contacts.values():
            record = asdict(contact)
            record["categories"] = ", ".join(contact.categories)
            records.append(record)
        return pd.DataFrame(records)

    def export_csv(self, destination: str | Path) -> Path:
        df = self.to_dataframe()
        path = Path(destination)
        df.to_csv(path, index=False)
        return path.resolve()

    def export_json(self, destination: str | Path) -> Path:
        path = Path(destination)
        payload = [asdict(contact) for contact in self._contacts.values()]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.resolve()

    def import_json(self, source: str | Path) -> None:
        path = Path(source)
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data:
            contact = Contact(**entry)
            self.add_contact(contact)


def sample_contact_book() -> ContactBook:
    """Create a populated contact book for demos."""

    book = ContactBook(categories=["Suppliers", "Fleet Clients", "Maintenance", "Emergency"])
    book.create_contact(
        name="Ana López",
        email="ana.lopez@energysuppliers.mx",
        phone="+52 55 1234 5678",
        company="Energy Suppliers MX",
        notes="Primary gasoline supplier for central region.",
        categories=["Suppliers"],
    )
    book.create_contact(
        name="Luis Martínez",
        email="luis.martinez@fleeterp.com",
        phone="+52 33 5555 7890",
        company="Fleet ERP",
        notes="Coordinates fleet refuelling schedules every Monday.",
        categories=["Fleet Clients"],
    )
    book.create_contact(
        name="Sofía Torres",
        email="sofia.torres@maintpro.com",
        phone="+52 81 2222 3344",
        company="MaintPro Services",
        notes="On-call maintenance technician.",
        categories=["Maintenance", "Emergency"],
    )
    return book
