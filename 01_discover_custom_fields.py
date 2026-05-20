"""
01_discover_custom_fields.py
----------------------------------------------------------------------------
Queries the Apache Jira REST API to map human-readable field names to their
instance-specific IDs (e.g., "Epic Link" -> "customfield_12311120").
Must be run once before any extraction or reconstruction.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import os
import sys

from jira_extract import config
from jira_extract.client import JiraClient

OUT_FILE = os.path.join(config.OUT_DIR, "field_map.json")


def main() -> None:
    os.makedirs(config.OUT_DIR, exist_ok=True)

    print(f"Fetching field metadata from {config.BASE_URL} ...")
    with JiraClient() as client:
        fields = client.get("rest/api/2/field")

    if not fields:
        print(f"ERROR: Failed to fetch fields from JIRA. Check network access to {config.BASE_URL}.", file=sys.stderr)
        sys.exit(1)

    field_map: dict[str, str] = {}
    for f in fields:
        if f.get("name") in config.TARGET_NAMES:
            field_map[f["name"]] = f["id"]
            print(f"  {f['name']:<22} -> {f['id']}")

    if not field_map:
        print(f"ERROR: No target fields resolved. Check network access to {config.BASE_URL}.", file=sys.stderr)
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(field_map, fh, indent=2)

    print(f"\nField map written to {OUT_FILE}")
    print(f"Resolved {len(field_map)} of {len(config.TARGET_NAMES)} target fields.")


if __name__ == "__main__":
    main()
