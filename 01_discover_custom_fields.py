"""
01_discover_custom_fields.py
----------------------------------------------------------------------------
Queries the Apache Jira REST API to map human-readable field names to their
instance-specific IDs (e.g., "Story Points" -> "customfield_12310243").
Must be run once before any extraction or reconstruction.
----------------------------------------------------------------------------
"""

import json
import os
import sys

import requests

BASE_URL = "https://issues.apache.org/jira"
OUT_DIR = "./extract_out"
OUT_FILE = os.path.join(OUT_DIR, "field_map.json")

TARGET_NAMES = {
    "Story Points",
    "Epic Link",
    "Epic Name",
    "Sprint",
    "Rank",
    "Target Version",
    "Fix Version/s",
}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Fetching field metadata from {BASE_URL} ...")
    resp = requests.get(f"{BASE_URL}/rest/api/2/field", timeout=30)
    resp.raise_for_status()
    fields = resp.json()

    field_map: dict[str, str] = {}
    for f in fields:
        if f.get("name") in TARGET_NAMES:
            field_map[f["name"]] = f["id"]
            print(f"  {f['name']:<22} -> {f['id']}")

    if not field_map:
        print(f"ERROR: No target fields resolved. Check network access to {BASE_URL}.", file=sys.stderr)
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(field_map, fh, indent=2)

    print(f"\nField map written to {OUT_FILE}")
    print(f"Resolved {len(field_map)} of {len(TARGET_NAMES)} target fields.")


if __name__ == "__main__":
    main()
