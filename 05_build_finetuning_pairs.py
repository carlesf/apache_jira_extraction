"""
05_build_finetuning_pairs.py
----------------------------------------------------------------------------
Assembles (requirement, tasks) pairs from the clean per-project JSONL files
produced by 04_reconstruct_features.py.

A "pair" is a parent requirement issue (Epic, Story, New Feature, Improvement)
together with the child issues that implement it. Two sources are used to
identify children:

  1. Back-links: any issue whose parent_key == parent.key
  2. Subtask keys: keys listed in parent.subtasks that also appear in the index

Both sources are merged and deduplicated. Children that were not extracted
(e.g., native Sub-task issue types) are silently excluded — only children
present in the clean index contribute to a pair.

Output
------
  extract_out/dataset/decomposition_pairs.jsonl  — one pair per line
  extract_out/dataset/pairs_summary.csv          — per-project statistics

Output schema (one JSON object per line):
  {
    "id":          "SPARK-12345",       # parent key
    "project":     "SPARK",
    "requirement": {
      "key", "type", "title", "description",
      "components", "labels", "fix_versions", "priority"
    },
    "tasks": [
      {
        "key", "type", "title", "description",
        "components", "labels", "fix_versions", "dependency_links"
      },
      ...
    ]
  }

Usage
-----
  # All projects found in extract_out/clean/
  python 05_build_finetuning_pairs.py

  # Specific projects only
  python 05_build_finetuning_pairs.py --projects SPARK FLINK KAFKA

  # Require at least 2 resolved tasks per requirement
  python 05_build_finetuning_pairs.py --min-tasks 2 --resolved-only

  # Allow requirements without a description (not recommended)
  python 05_build_finetuning_pairs.py --no-require-description
----------------------------------------------------------------------------
"""

import argparse
import csv
import glob
import json
import os
from collections import defaultdict


CLEAN_DIR  = "./extract_out/clean"
OUT_DIR    = "./extract_out/dataset"
OUT_PAIRS  = os.path.join(OUT_DIR, "decomposition_pairs.jsonl")
OUT_SUMMARY = os.path.join(OUT_DIR, "pairs_summary.csv")

# Requirement-level issue types that can serve as parents.
REQUIREMENT_TYPES = {"Epic", "Story", "New Feature", "Improvement"}

# Resolution values that indicate completed work.
RESOLVED_STATUSES = {"Resolved", "Closed", "Done"}


def load_index(projects: list[str]) -> dict[str, dict]:
    """Load clean JSONL files and return a key→record index."""
    index: dict[str, dict] = {}
    for project in projects:
        path = os.path.join(CLEAN_DIR, f"{project}.jsonl")
        if not os.path.isfile(path):
            print(f"  WARNING: {path} not found — skipping.")
            continue
        count = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                index[record["key"]] = record
                count += 1
        print(f"  Loaded {count:>6,} issues  ← {project}")
    return index


def build_child_index(index: dict[str, dict]) -> dict[str, set[str]]:
    """Return parent_key → set of child keys, from back-links."""
    children: dict[str, set[str]] = defaultdict(set)
    for record in index.values():
        pk = record.get("parent_key")
        if pk:
            children[pk].add(record["key"])
    return dict(children)


def collect_children(parent: dict, child_index: dict[str, set[str]],
                     index: dict[str, dict]) -> list[dict]:
    """
    Merge children from two sources and return only those present in the index.
    Source 1: back-link index (parent_key == parent.key)
    Source 2: parent.subtasks list
    """
    child_keys: set[str] = set()
    child_keys.update(child_index.get(parent["key"], set()))
    child_keys.update(k for k in parent.get("subtasks", []) if k in index)
    # Exclude the parent itself (defensive check)
    child_keys.discard(parent["key"])
    return [index[k] for k in sorted(child_keys) if k in index]


def task_record(child: dict) -> dict:
    return {
        "key":              child["key"],
        "type":             child["type"],
        "title":            child["title"],
        "description":      child["description"],
        "components":       child["components"],
        "labels":           child["labels"],
        "fix_versions":     child["fix_versions"],
        "dependency_links": child["dependency_links"],
    }


def requirement_record(parent: dict) -> dict:
    return {
        "key":         parent["key"],
        "type":        parent["type"],
        "title":       parent["title"],
        "description": parent["description"],
        "components":  parent["components"],
        "labels":      parent["labels"],
        "fix_versions": parent["fix_versions"],
        "priority":    parent["priority_at_creation"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build (requirement, tasks) fine-tuning pairs from clean Jira JSONL."
    )
    parser.add_argument(
        "--projects", nargs="+",
        help="Project keys to include. Defaults to all *.jsonl files in extract_out/clean/."
    )
    parser.add_argument(
        "--min-tasks", type=int, default=1,
        help="Minimum number of tasks a requirement must have to be included (default: 1)."
    )
    parser.add_argument(
        "--no-require-description", dest="require_description",
        action="store_false", default=True,
        help="Include requirements even if their description is empty."
    )
    parser.add_argument(
        "--resolved-only", action="store_true", default=False,
        help="Only include pairs where the parent requirement is resolved/closed."
    )
    args = parser.parse_args()

    # Discover projects.
    if args.projects:
        projects = args.projects
    else:
        projects = sorted(
            os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(CLEAN_DIR, "*.jsonl"))
            if not p.endswith(".summary.csv")
        )

    if not projects:
        print(f"No .jsonl files found in {CLEAN_DIR}. Run 04_reconstruct_features.py first.")
        return

    print(f"Loading {len(projects)} project(s): {', '.join(projects)}\n")
    index = load_index(projects)
    print(f"\nTotal issues in index: {len(index):,}")

    child_index = build_child_index(index)

    os.makedirs(OUT_DIR, exist_ok=True)

    # Per-project stats.
    stats: dict[str, dict] = {}
    for proj in projects:
        stats[proj] = {
            "requirements":    0,
            "pairs_written":   0,
            "skipped_no_desc": 0,
            "skipped_no_tasks": 0,
            "skipped_unresolved": 0,
            "total_tasks":     0,
        }

    pairs_written = 0

    with open(OUT_PAIRS, "w", encoding="utf-8") as out_fh:
        for parent in index.values():
            if parent.get("type") not in REQUIREMENT_TYPES:
                continue

            proj = parent.get("project", "UNKNOWN")
            s = stats.get(proj, stats.setdefault(proj, {
                "requirements": 0, "pairs_written": 0,
                "skipped_no_desc": 0, "skipped_no_tasks": 0,
                "skipped_unresolved": 0, "total_tasks": 0,
            }))
            s["requirements"] += 1

            if args.require_description and not parent.get("description"):
                s["skipped_no_desc"] += 1
                continue

            if args.resolved_only and parent.get("resolution") not in RESOLVED_STATUSES:
                s["skipped_unresolved"] += 1
                continue

            children = collect_children(parent, child_index, index)

            if len(children) < args.min_tasks:
                s["skipped_no_tasks"] += 1
                continue

            pair = {
                "id":          parent["key"],
                "project":     proj,
                "requirement": requirement_record(parent),
                "tasks":       [task_record(c) for c in children],
            }
            out_fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
            s["pairs_written"] += 1
            s["total_tasks"]   += len(children)
            pairs_written += 1

    # Summary CSV.
    summary_rows = []
    for proj, s in sorted(stats.items()):
        pw = s["pairs_written"]
        req = s["requirements"] or 1
        avg_tasks = round(s["total_tasks"] / pw, 2) if pw > 0 else 0.0
        summary_rows.append({
            "Project":           proj,
            "Requirements":      s["requirements"],
            "Pairs_Written":     pw,
            "Pair_Rate":         round(pw / req, 3),
            "Skipped_No_Desc":   s["skipped_no_desc"],
            "Skipped_No_Tasks":  s["skipped_no_tasks"],
            "Skipped_Unresolved": s["skipped_unresolved"],
            "Avg_Tasks":         avg_tasks,
            "Total_Tasks":       s["total_tasks"],
        })

    fieldnames = [
        "Project", "Requirements", "Pairs_Written", "Pair_Rate",
        "Skipped_No_Desc", "Skipped_No_Tasks", "Skipped_Unresolved",
        "Avg_Tasks", "Total_Tasks",
    ]
    with open(OUT_SUMMARY, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    # Console report.
    print(f"\n{'Project':<12} {'Reqs':>7} {'Pairs':>7} {'Rate':>6} {'AvgTasks':>9} {'TotalTasks':>11}")
    print("-" * 58)
    total_reqs = total_pairs = total_tasks = 0
    for row in summary_rows:
        print(
            f"  {row['Project']:<10} {row['Requirements']:>7,} {row['Pairs_Written']:>7,}"
            f" {row['Pair_Rate']:>6.1%} {row['Avg_Tasks']:>9.1f} {row['Total_Tasks']:>11,}"
        )
        total_reqs  += row["Requirements"]
        total_pairs += row["Pairs_Written"]
        total_tasks += row["Total_Tasks"]

    print("-" * 58)
    avg = round(total_tasks / total_pairs, 2) if total_pairs > 0 else 0.0
    print(f"  {'TOTAL':<10} {total_reqs:>7,} {total_pairs:>7,} {'':>6} {avg:>9.1f} {total_tasks:>11,}")
    print(f"\nDataset : {OUT_PAIRS}")
    print(f"Summary : {OUT_SUMMARY}")

    print("\nNOTE: Only children present in the clean index contribute to pairs.")
    print("      Native Sub-task issues are not extracted by script 03.")
    print("      Epic→Story pairs are fully supported; Story→Sub-task pairs")
    print("      require re-running script 03 with Sub-task added to TYPE_FILTER.")


if __name__ == "__main__":
    main()
