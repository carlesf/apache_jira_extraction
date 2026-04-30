"""
04_reconstruct_features.py
----------------------------------------------------------------------------
Reads raw per-issue JSON files from extract_out/raw/<PROJECT>/ and produces:
  - extract_out/clean/<PROJECT>.jsonl         (one issue per line, final schema)
  - extract_out/clean/<PROJECT>.summary.csv   (population rates for sanity checks)

Output schema is oriented toward task-decomposition fine-tuning:
  - Hierarchy  : parent_key (Epic or parent Story), subtasks
  - Decomp     : dependency_links (blocks / depends-on relationships)
  - Context    : title, description, components, labels, fix_versions
  - Metadata   : key, project, type, created_at, resolved_at, status, priority

Core rule: for time-varying fields (priority, ...), the value at creation is
taken from the oldest changelog entry for that field. If no history entry
exists the current value IS the creation value.

Usage:
  python 04_reconstruct_features.py --project SPARK
----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, timezone


def _parse_dt(s: str) -> datetime:
    s = s.replace("+0000", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def get_value_at_creation(issue: dict, field_name: str, current_value) -> object:
    """Return the field value at issue creation by finding the oldest changelog entry."""
    oldest_created: datetime | None = None
    oldest_from = None

    changelog = issue.get("changelog", {})
    for history in changelog.get("histories", []):
        for item in history.get("items", []):
            if item.get("field") == field_name or item.get("fieldId") == field_name:
                created = _parse_dt(history["created"])
                if oldest_created is None or created < oldest_created:
                    oldest_created = created
                    oldest_from = item.get("fromString")

    if oldest_created is None:
        return current_value
    return oldest_from


def get_link_added_at(issue: dict, target_key: str) -> str | None:
    """Scan changelog for the timestamp when a link to target_key was added."""
    changelog = issue.get("changelog", {})
    for history in changelog.get("histories", []):
        for item in history.get("items", []):
            if item.get("field") == "Link":
                combined = f"{item.get('toString', '')} {item.get('fromString', '')}"
                if target_key in combined:
                    return history["created"]
    return None


def process_issue(issue: dict, epic_link_field: str | None) -> dict:
    f = issue.get("fields", {})

    # Priority at creation.
    cur_priority = f.get("priority", {}).get("name") if f.get("priority") else None
    priority_at_creation = get_value_at_creation(issue, "priority", cur_priority)

    # Parent: subtask parent or Epic Link custom field.
    parent_key = None
    if f.get("parent"):
        parent_key = f["parent"].get("key")
    elif epic_link_field:
        parent_key = f.get(epic_link_field)

    # Issue links (blocks / depends-on / etc.).
    links: list[dict] = []
    for link in f.get("issuelinks", []):
        if link.get("outwardIssue"):
            target = link["outwardIssue"]["key"]
            direction = "outward"
        elif link.get("inwardIssue"):
            target = link["inwardIssue"]["key"]
            direction = "inward"
        else:
            continue
        added_at = get_link_added_at(issue, target)
        links.append({
            "type":      link.get("type", {}).get("name"),
            "direction": direction,
            "target":    target,
            "added_at":  added_at,
        })

    subtasks     = [st["key"] for st in f.get("subtasks", [])]
    components   = [c["name"] for c in f.get("components", [])]
    labels       = list(f.get("labels", []))
    fix_versions = [v["name"] for v in f.get("fixVersions", [])]

    return {
        "key":                  issue.get("key"),
        "project":              f.get("project", {}).get("key"),
        "type":                 f.get("issuetype", {}).get("name") if f.get("issuetype") else None,
        "status":               f.get("status", {}).get("name") if f.get("status") else None,
        "resolution":           f.get("resolution", {}).get("name") if f.get("resolution") else None,
        "created_at":           f.get("created"),
        "resolved_at":          f.get("resolutiondate"),
        "title":                f.get("summary"),
        "description":          f.get("description"),
        "priority_at_creation": priority_at_creation,
        "components":           components,
        "labels":               labels,
        "fix_versions":         fix_versions,
        "original_estimate_sec": f.get("timeoriginalestimate"),
        "time_spent_sec":       f.get("timespent"),
        "parent_key":           parent_key,
        "subtasks":             subtasks,
        "dependency_links":     links,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct creation-time features from raw Jira JSON.")
    parser.add_argument("--project", required=True, help="Jira project key, e.g. SPARK")
    args = parser.parse_args()

    in_dir       = f"./extract_out/raw/{args.project}"
    clean_dir    = "./extract_out/clean"
    out_jsonl    = os.path.join(clean_dir, f"{args.project}.jsonl")
    out_summary  = os.path.join(clean_dir, f"{args.project}.summary.csv")
    field_map_path = "./extract_out/field_map.json"

    if not os.path.isdir(in_dir):
        print(f"ERROR: Raw directory not found: {in_dir}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(field_map_path):
        print("ERROR: Field map not found. Run 01_discover_custom_fields.py first.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(clean_dir, exist_ok=True)

    with open(field_map_path, encoding="utf-8") as fh:
        field_map = json.load(fh)

    epic_link_field = field_map.get("Epic Link")

    raw_files = sorted(glob.glob(os.path.join(in_dir, "*.json")))
    print(f"Processing {len(raw_files)} issues from {in_dir} ...")

    stats = {
        "total":                0,
        "has_description":      0,
        "has_components":       0,
        "has_labels":           0,
        "has_fix_versions":     0,
        "has_original_estimate": 0,
        "has_time_spent":       0,
        "has_parent":           0,
        "has_subtasks":         0,
        "has_links":            0,
        "has_resolution":       0,
    }

    with open(out_jsonl, "w", encoding="utf-8") as out_fh:
        for i, filepath in enumerate(raw_files, start=1):
            with open(filepath, encoding="utf-8") as fh:
                issue = json.load(fh)

            record = process_issue(issue, epic_link_field)

            stats["total"] += 1
            if record["description"]:
                stats["has_description"] += 1
            if record["components"]:
                stats["has_components"] += 1
            if record["labels"]:
                stats["has_labels"] += 1
            if record["fix_versions"]:
                stats["has_fix_versions"] += 1
            if record["original_estimate_sec"] is not None:
                stats["has_original_estimate"] += 1
            if record["time_spent_sec"] is not None:
                stats["has_time_spent"] += 1
            if record["parent_key"]:
                stats["has_parent"] += 1
            if record["subtasks"]:
                stats["has_subtasks"] += 1
            if record["dependency_links"]:
                stats["has_links"] += 1
            if record["resolution"]:
                stats["has_resolution"] += 1

            out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")

            if i % 500 == 0:
                print(f"  processed {i}/{len(raw_files)}")

    # Summary CSV.
    total = float(stats["total"]) or 1.0
    summary_rows = [
        {"Field": "total_issues",      "Count": stats["total"],            "Rate": 1.0},
        {"Field": "has_description",   "Count": stats["has_description"],  "Rate": round(stats["has_description"]  / total, 3)},
        {"Field": "has_components",    "Count": stats["has_components"],   "Rate": round(stats["has_components"]   / total, 3)},
        {"Field": "has_labels",        "Count": stats["has_labels"],       "Rate": round(stats["has_labels"]       / total, 3)},
        {"Field": "has_fix_versions",      "Count": stats["has_fix_versions"],      "Rate": round(stats["has_fix_versions"]      / total, 3)},
        {"Field": "has_original_estimate", "Count": stats["has_original_estimate"], "Rate": round(stats["has_original_estimate"] / total, 3)},
        {"Field": "has_time_spent",        "Count": stats["has_time_spent"],        "Rate": round(stats["has_time_spent"]        / total, 3)},
        {"Field": "has_parent",            "Count": stats["has_parent"],            "Rate": round(stats["has_parent"]            / total, 3)},
        {"Field": "has_subtasks",      "Count": stats["has_subtasks"],     "Rate": round(stats["has_subtasks"]     / total, 3)},
        {"Field": "has_links",         "Count": stats["has_links"],        "Rate": round(stats["has_links"]        / total, 3)},
        {"Field": "has_resolution",    "Count": stats["has_resolution"],   "Rate": round(stats["has_resolution"]   / total, 3)},
    ]

    with open(out_summary, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Field", "Count", "Rate"])
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"\nClean dataset : {out_jsonl}")
    print(f"Summary       : {out_summary}\n")

    col_w = max(len(r["Field"]) for r in summary_rows)
    print(f"{'Field':<{col_w}}  {'Count':>8}  {'Rate':>6}")
    print("-" * (col_w + 18))
    for row in summary_rows:
        print(f"{row['Field']:<{col_w}}  {row['Count']:>8,}  {row['Rate']:>6.3f}")


if __name__ == "__main__":
    main()
