"""
02_measure_project_coverage.py
----------------------------------------------------------------------------
For each candidate project, compute:
  - Total issues in the time window
  - Fraction with Story Points populated  (SP_Rate)
  - Fraction with Original Estimate populated (Est_Rate)
  - Fraction with Blocks/Depends links (Link_Rate)

Uses maxResults=0 for every query: no issues are downloaded, only totals.
Output: extract_out/project_coverage.csv

Selection rule of thumb: SP_Rate >= 0.30 AND Link_Rate >= 0.10
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

CANDIDATES = [
    "SPARK", "KAFKA", "FLINK", "BEAM", "AIRFLOW",
    "CASSANDRA", "ARROW", "PULSAR", "HIVE", "HADOOP",
    "HDFS", "HBASE", "LUCENE", "SOLR", "CALCITE",
]

FROM_DATE = "2018-01-01"
TO_DATE = "2024-01-01"
TYPE_FILTER = 'issuetype in (Bug, Story, Task, "New Feature", Improvement, Epic)'


def get_count(jql: str) -> int:
    encoded = quote(jql)
    url = f"{BASE_URL}/rest/api/2/search?jql={encoded}&maxResults=0"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return int(data.get("total", -1))
    except Exception:
        return -1


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Measuring coverage for {len(CANDIDATES)} projects in window {FROM_DATE} .. {TO_DATE}\n")

    rows: list[dict] = []
    for project in CANDIDATES:
        base = (
            f'project = {project} AND created >= "{FROM_DATE}" AND created < "{TO_DATE}"'
            f" AND {TYPE_FILTER}"
        )

        total = get_count(base)
        time.sleep(DELAY_S)
        sp = get_count(f'{base} AND "Story Points" is not EMPTY')
        time.sleep(DELAY_S)
        est = get_count(f"{base} AND timeoriginalestimate is not EMPTY")
        time.sleep(DELAY_S)
        links = get_count(
            f'{base} AND issueLinkType in (Blocks, "is blocked by", Depends, "depends on")'
        )
        time.sleep(DELAY_S)

        sp_rate = round(sp / total, 3) if total > 0 else 0.0
        est_rate = round(est / total, 3) if total > 0 else 0.0
        link_rate = round(links / total, 3) if total > 0 else 0.0

        rows.append({
            "Project": project,
            "Total": total,
            "SP_Populated": sp,
            "SP_Rate": sp_rate,
            "Est_Populated": est,
            "Est_Rate": est_rate,
            "Linked": links,
            "Link_Rate": link_rate,
        })

        print(
            f"  {project:<10} total={total:>7,}  "
            f"sp={sp:>6,} ({sp_rate:>6.1%})  "
            f"est={est:>6,} ({est_rate:>6.1%})  "
            f"links={links:>6,} ({link_rate:>6.1%})"
        )

    rows.sort(key=lambda r: r["SP_Rate"], reverse=True)

    fieldnames = ["Project", "Total", "SP_Populated", "SP_Rate", "Est_Populated", "Est_Rate", "Linked", "Link_Rate"]
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCoverage saved to {OUT_FILE}")
    print("Selection rule of thumb: SP_Rate >= 0.30 AND Link_Rate >= 0.10")


if __name__ == "__main__":
    main()
