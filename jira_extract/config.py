"""
jira_extract/config.py
----------------------
Centralized default settings and constants for the JIRA extraction pipeline.
"""

BASE_URL = "https://issues.apache.org/jira"
OUT_DIR = "./extract_out"

# Default target date range for requirement issues
FROM_DATE = "2022-01-01"
TO_DATE = "2026-01-01"

# Target issue types that count as requirements (Story, New Feature, Improvement, Epic)
TYPE_FILTER = 'issuetype in (Story, "New Feature", Improvement, Epic)'

# Target fields to map in 01_discover_custom_fields.py
TARGET_NAMES = {
    "Epic Link",
    "Epic Name",
    "Fix Version/s",
    "Story Points",
}
