"""
02_measure_project_coverage.py
----------------------------------------------------------------------------
Systematically discovers all projects on the Apache Jira instance, filters
them to those with sufficient activity in the 2022-2025 window, then
measures decomposition-relevant coverage metrics for each survivor.

Two-phase approach:
  Phase 1 — Discovery
    Fetches every project from /rest/api/2/project and counts its requirement
    issues in the target window. Projects below --min-issues are dropped.

  Phase 2 — Coverage measurement (surviving projects only)
    For each project, computes:
      Total     : requirement issues in the window
      Desc_Rate : fraction with a non-empty description
      Comp_Rate : fraction tagged with at least one component
      Link_Rate : fraction with at least one issue link

Uses maxResults=0 for every search query: no issues are downloaded.
Output: extract_out/project_coverage.csv

Selection rule of thumb:
  Desc_Rate >= 0.50  AND  (Link_Rate >= 0.15  OR  Comp_Rate >= 0.30)

Usage:
  python 02_measure_project_coverage.py               # defaults
  python 02_measure_project_coverage.py --min-issues 50 --delay-ms 500
----------------------------------------------------------------------------
"""

import argparse
import atexit
import csv
import os
import time
from urllib.parse import quote

from jira_extract import config
from jira_extract.client import JiraClient

OUT_FILE = os.path.join(config.OUT_DIR, "project_coverage.csv")

client = JiraClient()
atexit.register(client.close)


def get_count(jql: str, delay_s: float) -> int:
    encoded = quote(jql)
    path = f"rest/api/2/search?jql={encoded}&maxResults=0"
    try:
        resp = client.get(path)
        if resp is None:
            return -1
        return int(resp.get("total", -1))
    except Exception:
        return -1
    finally:
        time.sleep(delay_s)


def fetch_all_project_keys() -> list[str]:
    """Return every project key available on the Jira instance."""
    resp = client.get("rest/api/2/project")
    if not resp:
        raise RuntimeError("Failed to fetch project list from JIRA.")
    return [p["key"] for p in resp]


def discover_active_projects(all_keys: list[str], min_issues: int,
                              delay_s: float) -> list[tuple[str, int]]:
    """
    Filter project keys to those with at least min_issues requirement issues
    in the target window. Returns list of (key, total) sorted by total desc.
    """
    active: list[tuple[str, int]] = []
    n = len(all_keys)
    for i, key in enumerate(all_keys, start=1):
        jql = (
            f'project = "{key}"'
            f' AND created >= "{config.FROM_DATE}" AND created < "{config.TO_DATE}"'
            f" AND {config.TYPE_FILTER}"
        )
        total = get_count(jql, delay_s)
        if total >= min_issues:
            active.append((key, total))
            marker = " <--"
        else:
            marker = ""
        if i % 20 == 0 or total >= min_issues:
            print(f"  [{i:>3}/{n}] {key:<20} {total if total >= 0 else 'error':>6}{marker}")

    active.sort(key=lambda x: x[1], reverse=True)
    return active


def measure_coverage(project: str, total: int, delay_s: float) -> dict:
    base = (
        f'project = "{project}"'
        f' AND created >= "{config.FROM_DATE}" AND created < "{config.TO_DATE}"'
        f" AND {config.TYPE_FILTER}"
    )

    desc   = get_count(f"{base} AND description is not EMPTY", delay_s)
    comp   = get_count(f"{base} AND component is not EMPTY",   delay_s)
    linked = get_count(f"{base} AND issuelinks is not EMPTY",  delay_s)

    desc_rate = round(desc   / total, 3) if total > 0 else 0.0
    comp_rate = round(comp   / total, 3) if total > 0 else 0.0
    link_rate = round(linked / total, 3) if total > 0 else 0.0

    return {
        "Project":   project,
        "Total":     total,
        "Desc":      desc,
        "Desc_Rate": desc_rate,
        "Comp":      comp,
        "Comp_Rate": comp_rate,
        "Linked":    linked,
        "Link_Rate": link_rate,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover and measure Apache Jira projects for task-decomposition coverage."
    )
    parser.add_argument(
        "--min-issues", type=int, default=100,
        help="Minimum requirement issues in the window to include a project (default: 100)."
    )
    parser.add_argument(
        "--delay-ms", type=int, default=400,
        help="Delay between API calls in milliseconds (default: 400)."
    )
    args = parser.parse_args()
    delay_s = args.delay_ms / 1000.0

    os.makedirs(config.OUT_DIR, exist_ok=True)

    # Phase 1: discover all projects and filter by activity.
    print(f"Fetching project list from {config.BASE_URL} ...")
    all_keys = fetch_all_project_keys()
    print(f"Found {len(all_keys)} projects. Filtering to those with >= {args.min_issues} "
          f"requirement issues in {config.FROM_DATE} .. {config.TO_DATE} ...\n")

    active = discover_active_projects(all_keys, args.min_issues, delay_s)
    print(f"\n{len(active)} projects passed the activity filter.\n")

    if not active:
        print("No projects found. Try lowering --min-issues.")
        return

    # Phase 2: full coverage measurement.
    print(f"{'Project':<20} {'Total':>7}  {'Desc':>6}  {'Comp':>6}  {'Link':>6}")
    print("-" * 56)

    rows: list[dict] = []
    for project, total in active:
        row = measure_coverage(project, total, delay_s)
        rows.append(row)
        print(
            f"  {project:<18} {total:>7,}  "
            f"{row['Desc_Rate']:>6.1%}  "
            f"{row['Comp_Rate']:>6.1%}  "
            f"{row['Link_Rate']:>6.1%}"
        )

    # Sort by Desc_Rate then Link_Rate descending.
    rows.sort(key=lambda r: (r["Desc_Rate"], r["Link_Rate"]), reverse=True)

    fieldnames = ["Project", "Total", "Desc", "Desc_Rate", "Comp", "Comp_Rate", "Linked", "Link_Rate"]
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCoverage saved to {OUT_FILE}")
    print("Selection rule: Desc_Rate >= 0.50  AND  (Link_Rate >= 0.15  OR  Comp_Rate >= 0.30)")


if __name__ == "__main__":
    main()

