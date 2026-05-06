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
    "decomposition_level": "epic_to_feature",
    "quality_score": 4,
    "requirement": {
      "key", "type", "title", "description",
      "components", "labels", "fix_versions", "priority"
    },
    "tasks": [
      {
        "key", "type", "title", "description",
        "components", "labels", "fix_versions", "parent_relation",
        "dependency_links"
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

  # Require at least 2 tasks for each completed parent requirement
  python 05_build_finetuning_pairs.py --min-tasks 2 --resolved-only

  # Also require each retained child task to be completed
  python 05_build_finetuning_pairs.py --min-tasks 2 --children-completed-only

  # Cleaner set from richer Epic-link extraction
  python 05_build_finetuning_pairs.py --min-tasks 2 --max-tasks 12 --min-quality-score 3

  # Exclude noisy child issue types
  python 05_build_finetuning_pairs.py --min-tasks 2 --max-tasks 12 --min-quality-score 3 --exclude-child-types Bug Test

  # Feature-to-task pairs only
  python 05_build_finetuning_pairs.py --decomposition-level feature_to_task --min-tasks 2 --max-tasks 12 --min-quality-score 3

  # Include task status trajectories in the pair output
  python 05_build_finetuning_pairs.py --projects IGNITE --min-tasks 2 --include-status-history

  # Allow requirements without a description (not recommended)
  python 05_build_finetuning_pairs.py --no-require-description
----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set


CLEAN_DIR  = "./extract_out/clean"
OUT_DIR    = "./extract_out/dataset"
OUT_PAIRS  = os.path.join(OUT_DIR, "decomposition_pairs.jsonl")
OUT_SUMMARY = os.path.join(OUT_DIR, "pairs_summary.csv")

# Requirement-level issue types that can serve as parents.
REQUIREMENT_TYPES = {"Epic", "Story", "New Feature", "Improvement"}

# Conservative completion markers. Jira commonly stores workflow state in
# status, while resolution describes why work ended; --resolved-only uses both.
COMPLETED_STATUSES = {"resolved", "closed", "done"}
COMPLETED_RESOLUTIONS = {"fixed", "done"}
DECOMPOSITION_LEVELS = ("epic_to_feature", "feature_to_task")


def load_index(projects: List[str]) -> Dict[str, dict]:
    """Load clean JSONL files and return a key→record index."""
    index: Dict[str, dict] = {}
    for project in projects:
        path = os.path.join(CLEAN_DIR, f"{project}.jsonl")
        if not os.path.isfile(path):
            print(f"  WARNING: {path} not found - skipping.")
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
        print(f"  Loaded {count:>6,} issues <- {project}")
    return index


def build_child_index(index: Dict[str, dict]) -> Dict[str, Set[str]]:
    """Return parent_key → set of child keys, from back-links."""
    children: Dict[str, Set[str]] = defaultdict(set)
    for record in index.values():
        pk = record.get("parent_key")
        if pk:
            children[pk].add(record["key"])
    return dict(children)


def _has_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalised(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _normalised_set(values: List[str]) -> Set[str]:
    return {_normalised(value) for value in values if _normalised(value)}


def is_completed(record: dict) -> bool:
    """Return True when Jira status or resolution conservatively means done."""
    return (
        _normalised(record.get("status")) in COMPLETED_STATUSES
        or _normalised(record.get("resolution")) in COMPLETED_RESOLUTIONS
    )


def _parse_jira_timestamp(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except ValueError:
        return None


def decomposition_level(parent: dict) -> str:
    return "epic_to_feature" if parent.get("type") == "Epic" else "feature_to_task"


def child_type_is_excluded(child: dict, excluded_types: Set[str]) -> bool:
    return _normalised(child.get("type")) in excluded_types


def quality_score(parent: dict, children: List[dict]) -> int:
    """
    Small interpretable heuristic for pair filtering/stratification:
      +1 parent has description
      +1 child count is 2..12
      +1 at least half of children have descriptions
      +1 comparable child creation times are not before the parent creation time
      -1 child count is >20
      -1 any child is a Bug
    """
    score = 0

    if _has_text(parent.get("description")):
        score += 1

    child_count = len(children)
    if 2 <= child_count <= 12:
        score += 1
    if child_count > 20:
        score -= 1

    described_children = sum(1 for child in children if _has_text(child.get("description")))
    if child_count and described_children * 2 >= child_count:
        score += 1

    parent_created = _parse_jira_timestamp(parent.get("created_at"))
    comparable_child_dates = []
    if parent_created is not None:
        for child in children:
            child_created = _parse_jira_timestamp(child.get("created_at"))
            if child_created is not None:
                comparable_child_dates.append(child_created)
    if comparable_child_dates and all(child_created >= parent_created for child_created in comparable_child_dates):
        score += 1

    if any(_normalised(child.get("type")) == "bug" for child in children):
        score -= 1

    return score


def collect_children(parent: dict, child_index: Dict[str, Set[str]],
                     index: Dict[str, dict]) -> List[dict]:
    """
    Merge children from two sources and return only those present in the index.
    Source 1: back-link index (parent_key == parent.key)
    Source 2: parent.subtasks list
    """
    child_keys: Set[str] = set()
    child_keys.update(child_index.get(parent["key"], set()))
    child_keys.update(k for k in parent.get("subtasks", []) if k in index)
    # Exclude the parent itself (defensive check)
    child_keys.discard(parent["key"])
    return [index[k] for k in sorted(child_keys) if k in index]


def task_record(child: dict, include_status_history: bool = False) -> dict:
    record = {
        "key":                   child["key"],
        "type":                  child["type"],
        "title":                 child["title"],
        "description":           child["description"],
        "components":            child["components"],
        "labels":                child["labels"],
        "fix_versions":          child["fix_versions"],
        "parent_relation":       child.get("parent_relation"),
        "story_points":         child.get("story_points"),
        "original_estimate_h":  child.get("original_estimate_h"),
        "time_spent_h":         child.get("time_spent_h"),
        "dependency_links":      child["dependency_links"],
    }
    if include_status_history:
        record["initial_status"] = child.get("initial_status")
        record["status_history"] = child.get("status_history", [])
    return record


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


def initial_stats() -> dict:
    return {
        "requirements": 0,
        "pairs_written": 0,
        "skipped_no_desc": 0,
        "skipped_no_tasks": 0,
        "skipped_unresolved": 0,
        "skipped_max_tasks": 0,
        "skipped_low_quality": 0,
        "skipped_decomposition_level": 0,
        "skipped_after_child_exclusion": 0,
        "total_tasks": 0,
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
        "--max-tasks", type=int,
        help="Maximum number of tasks a requirement may have after child filters."
    )
    parser.add_argument(
        "--min-quality-score", type=int,
        help="Minimum quality_score to keep a pair; common cleaner values are 2 or 3."
    )
    parser.add_argument(
        "--decomposition-level", choices=DECOMPOSITION_LEVELS,
        help="Only keep pairs at this decomposition level."
    )
    parser.add_argument(
        "--exclude-child-types", nargs="+", default=[],
        help="Exclude child issue types before task-count filters; matching is case-insensitive."
    )
    parser.add_argument(
        "--no-require-description", dest="require_description",
        action="store_false", default=True,
        help="Include requirements even if their description is empty."
    )
    parser.add_argument(
        "--resolved-only", action="store_true", default=False,
        help="Only include pairs where the parent requirement has a completed status or resolution."
    )
    parser.add_argument(
        "--children-completed-only", action="store_true", default=False,
        help="Only keep completed child tasks before applying --min-tasks."
    )
    parser.add_argument(
        "--include-status-history", action="store_true", default=False,
        help="Include initial_status and status_history for each output task."
    )
    args = parser.parse_args()

    if args.max_tasks is not None and args.max_tasks < 1:
        parser.error("--max-tasks must be >= 1")

    excluded_child_types = _normalised_set(args.exclude_child_types)

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
    stats: Dict[str, dict] = {}
    for proj in projects:
        stats[proj] = initial_stats()

    pairs_written = 0

    with open(OUT_PAIRS, "w", encoding="utf-8") as out_fh:
        for parent in index.values():
            if parent.get("type") not in REQUIREMENT_TYPES:
                continue

            proj = parent.get("project", "UNKNOWN")
            s = stats.get(proj, stats.setdefault(proj, initial_stats()))
            s["requirements"] += 1

            # Filtering order:
            # 1. parent description/completion, 2. child-level filters,
            # 3. task count limits, 4. decomposition level, 5. quality score.
            if args.require_description and not parent.get("description"):
                s["skipped_no_desc"] += 1
                continue

            if args.resolved_only and not is_completed(parent):
                s["skipped_unresolved"] += 1
                continue

            children = collect_children(parent, child_index, index)
            if args.children_completed_only:
                children = [child for child in children if is_completed(child)]

            children_before_type_exclusion = len(children)
            if excluded_child_types:
                children = [
                    child for child in children
                    if not child_type_is_excluded(child, excluded_child_types)
                ]

            if len(children) < args.min_tasks:
                if excluded_child_types and children_before_type_exclusion >= args.min_tasks:
                    s["skipped_after_child_exclusion"] += 1
                else:
                    s["skipped_no_tasks"] += 1
                continue

            if args.max_tasks is not None and len(children) > args.max_tasks:
                s["skipped_max_tasks"] += 1
                continue

            level = decomposition_level(parent)
            if args.decomposition_level and level != args.decomposition_level:
                s["skipped_decomposition_level"] += 1
                continue

            score = quality_score(parent, children)
            if args.min_quality_score is not None and score < args.min_quality_score:
                s["skipped_low_quality"] += 1
                continue

            pair = {
                "id":                  parent["key"],
                "project":             proj,
                "decomposition_level": level,
                "quality_score":       score,
                "requirement":         requirement_record(parent),
                "tasks":               [
                    task_record(c, include_status_history=args.include_status_history)
                    for c in children
                ],
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
            "Skipped_Max_Tasks": s["skipped_max_tasks"],
            "Skipped_Low_Quality": s["skipped_low_quality"],
            "Skipped_Decomposition_Level": s["skipped_decomposition_level"],
            "Skipped_After_Child_Exclusion": s["skipped_after_child_exclusion"],
            "Avg_Tasks":         avg_tasks,
            "Total_Tasks":       s["total_tasks"],
        })

    fieldnames = [
        "Project", "Requirements", "Pairs_Written", "Pair_Rate",
        "Skipped_No_Desc", "Skipped_No_Tasks", "Skipped_Unresolved",
        "Skipped_Max_Tasks", "Skipped_Low_Quality",
        "Skipped_Decomposition_Level", "Skipped_After_Child_Exclusion",
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
    print("      Script 03 fetches subtasks and Epic-linked children, so both")
    print("      Epic->child and Story->Sub-task hierarchies are supported.")


if __name__ == "__main__":
    main()
