"""
gasolinera package
==================

Modular analytics toolkit for gasoline distribution operations.  The package
is intentionally designed to be extensible: each sub-module is responsible for
a domain concern (data preparation, forecasting, scheduling, alerts, mapping,
etc.).  The modules expose lightweight classes and helper functions so that
they can be orchestrated from command-line scripts, notebooks, or a web UI.
"""

from __future__ import annotations

from . import analytics, contacts, data, forecast, inventory, mapping, notifications, scheduling, security


class _MissingAPIModule:
    """Helper to raise a friendly error when Flask is not installed."""

    def __getattr__(self, _name: str):
        raise ModuleNotFoundError("Flask is required to use the gasolinera.api module. Install it via 'pip install flask'.")


try:  # pragma: no cover - optional dependency path
    from . import api  # type: ignore
except ModuleNotFoundError as exc:
    if exc.name == "flask":
        api = _MissingAPIModule()  # type: ignore
    else:
        raise

__all__ = [
    "analytics",
    "api",
    "contacts",
    "data",
    "forecast",
    "inventory",
    "mapping",
    "notifications",
    "scheduling",
    "security",
]
