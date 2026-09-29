"""Audit subsystem."""

from app.audit.writer import record, record_system

__all__ = ["record", "record_system"]
