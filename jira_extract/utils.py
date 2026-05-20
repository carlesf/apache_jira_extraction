"""
jira_extract/utils.py
---------------------
Unified datetime parsing and cleanup routines for the JIRA extraction pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_jira_timestamp(s: Any) -> Optional[datetime]:
    """Parse JIRA's ISO timestamp format, normalizing older timezone offsets (e.g. +0000 to +00:00)."""
    if not isinstance(s, str) or not s.strip():
        return None
    normalized = s.replace("+0000", "+00:00").replace("-0000", "-00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_jira_timestamp_utc(s: Any) -> datetime:
    """Parse JIRA's ISO timestamp, returning datetime.min in UTC if invalid or missing."""
    dt = parse_jira_timestamp(s)
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt
