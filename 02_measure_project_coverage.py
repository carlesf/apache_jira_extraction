"""
02_measure_project_coverage.py
----------------------------------------------------------------------------
For each candidate Apache project, compute coverage metrics relevant to
training a task-decomposition model (requirement -> tasks):

  - Total      : requirement-type issues in the 2022-2025 window
  - Desc_Rate  : fraction with a non-empty description
  - Comp_Rate  : fraction tagged with at least one component
  - Link_Rate  : fraction with at least one issue link (proxy for decomposition)

Uses maxResults=0 for every query: no issues are downloaded, only totals.
Output: extract_out/project_coverage.csv

Selection rule of thumb:
  Desc_Rate >= 0.50  AND  (Link_Rate >= 0.15  OR  Comp_Rate >= 0.30)
----------------------------------------------------------------------------
"""

import csv
import os
import time
from urllib.parse import quote

import requests

BASE_URL = "https://issues.apache.org/jira"
OUT_DIR = "./extract_out"
OUT_FILE = os.path.join(OUT_DIR, "project_coverage.csv")
DELAY_S = 0.4

# Active Apache projects 2022-2025
CANDIDATES = [
    # Core big-data processing
    "SPARK", "KAFKA", "FLINK", "BEAM", "AIRFLOW",
    # Storage / table formats
    "CASSANDRA", "HBASE", "HUDI", "ICEBERG", "PARQUET",
    # Search / analytical query
    "LUCENE", "SOLR", "CALCITE", "DRUID",
    # Messaging / integration
    "PULSAR", "CAMEL", "NIFI",
    # In-memory / columnar compute
    "ARROW",
    # Security / governance
    "RANGER",
]

FROM_DATE = "2022-01-01"
TO_DATE = "2026-01-01"   # exclusive upper bound, covers through end of 2025

# Requirement-type issues only; excludes Bug and Task
TYPE_FILTER = 'issuetype in (Story, "New Feature", Improvement, Epic)'


def get_count(jql: str) -> int:
    encoded = quote(jql)
    url = f"{BASE_URL}/rest/api/2/search?jql={encoded}&maxResults=0"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return int(resp.json().get("total", -1))
    except Exception:
        return -1


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Measuring coverage for {len(CANDIDATES)} projects | window {FROM_DATE} .. {TO_DATE}\n")
    print(f"{'Project':<12} {'Total':>7}  {'Desc':>6}  {'Comp':>6}  {'Link':>6}")
    print("-" * 48)

    rows: list[dict] = []
    for project in CANDIDATES:
        base = (
            f'project = {project}'
            f' AND created >= "{FROM_DATE}" AND created < "{TO_DATE}"'
            f" AND {TYPE_FILTER}"
        )

        total = get_count(base)
        time.sleep(DELAY_S)
        desc = get_count(f"{base} AND description is not EMPTY")
        time.sleep(DELAY_S)
        comp = get_count(f"{base} AND component is not EMPTY")
        time.sleep(DELAY_S)
        linked = get_count(f"{base} AND issuelinks is not EMPTY")
        time.sleep(DELAY_S)

        desc_rate = round(desc / total, 3) if total > 0 else 0.0
        comp_rate = round(comp / total, 3) if total > 0 else 0.0
        link_rate = round(linked / total, 3) if total > 0 else 0.0

        rows.append({
            "Project":    project,
            "Total":      total,
            "Desc":       desc,
            "Desc_Rate":  desc_rate,
            "Comp":       comp,
            "Comp_Rate":  comp_rate,
            "Linked":     linked,
            "Link_Rate":  link_rate,
        })

        print(
            f"  {project:<10} {total:>7,}  "
            f"{desc_rate:>6.1%}  "
            f"{comp_rate:>6.1%}  "
            f"{link_rate:>6.1%}"
        )

    # Sort by description rate descending (most informative first)
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
