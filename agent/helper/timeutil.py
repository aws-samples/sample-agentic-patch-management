"""Timestamps from AWS records: datetimes and ISO strings, always as UTC-aware datetimes."""

from datetime import datetime, timezone
from typing import Optional


def as_utc(value) -> Optional[datetime]:
    """A timezone-aware datetime from a datetime or ISO 8601 string ("Z" allowed); None if unreadable.

    Naive values are taken as UTC.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None
