"""
03_extract_issues.py
----------------------------------------------------------------------------
Main extraction. Two passes:
  1. Enumerate all matching issue keys via paginated JQL search.
  2. For each key, fetch full issue + changelog, handling changelog pagination
     for issues with > 100 history entries.

Features:
  - Resumable via .checkpoint.txt
  - Exponential backoff on HTTP 429/5xx
  - One JSON file per issue (easy to inspect, easy to resume)

Usage:
  python 03_extract_issues.py --project SPARK --from-date 2018-01-01 --to-date 2024-01-01
----------------------------------------------------------------------------
"""

import argparse
import json
import logging
import os
import time
from urllib.parse import quote

import requests

BASE_URL = "https://issues.apache.org/jira"
TYPE_FILTER = 'issuetype in (Bug, Story, Task, "New Feature", Improvement, Epic)'
MAX_RETRIES = 5


def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("extract")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def get_jira_json(url: str, logger: logging.Logger, delay_s: float) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 404:
                logger.warning("404 (issue unavailable or deleted): %s", url)
                return None

            if resp.status_code in (429, 502, 503, 504):
                wait = min(60, 2 ** attempt)
                logger.warning("HTTP %d on attempt %d. Waiting %ds.", resp.status_code, attempt, wait)
                time.sleep(wait)
                continue

            logger.error("HTTP %d (not retrying): %s", resp.status_code, url)
            return None

        except Exception as exc:
            wait = min(60, 2 ** attempt)
            logger.warning("Exception on attempt %d: %s. Waiting %ds.", attempt, exc, wait)
            time.sleep(wait)

    logger.error("Max retries exceeded: %s", url)
    return None


def enumerate_keys(project: str, from_date: str, to_date: str,
                   batch_size: int, delay_s: float, logger: logging.Logger) -> list[str]:
    jql = (
        f'project = {project} AND created >= "{from_date}" AND created < "{to_date}"'
        f" AND {TYPE_FILTER} ORDER BY created ASC"
    )
    encoded = quote(jql)

    all_keys: list[str] = []
    start_at = 0
    total = -1

    logger.info("Enumerating issues in project=%s window=[%s .. %s]", project, from_date, to_date)

    while True:
        url = (
            f"{BASE_URL}/rest/api/2/search"
            f"?jql={encoded}&fields=key&maxResults={batch_size}&startAt={start_at}"
        )
        page = get_jira_json(url, logger, delay_s)
        if page is None:
            break

        for issue in page.get("issues", []):
            all_keys.append(issue["key"])

        if total < 0:
            total = int(page.get("total", 0))
            logger.info("Total matching issues: %d", total)

        start_at += batch_size
        logger.info("Enumerated %d/%d", min(start_at, total), total)
        time.sleep(delay_s)

        if start_at >= total:
            break

    logger.info("Enumeration complete: %d keys collected.", len(all_keys))
    return all_keys


def fetch_issue(key: str, out_dir: str, delay_s: float, logger: logging.Logger) -> bool:
    url = f"{BASE_URL}/rest/api/2/issue/{key}?expand=changelog"
    issue = get_jira_json(url, logger, delay_s)
    if issue is None:
        return False

    # Paginate changelog if truncated.
    changelog = issue.get("changelog", {})
    if changelog.get("total", 0) > changelog.get("maxResults", 0):
        histories: list = list(changelog.get("histories", []))
        ch_start = changelog.get("maxResults", 100)

        while ch_start < changelog["total"]:
            ch_url = f"{BASE_URL}/rest/api/2/issue/{key}/changelog?startAt={ch_start}"
            ch_page = get_jira_json(ch_url, logger, delay_s)
            if ch_page is None:
                break
            histories.extend(ch_page.get("values", []))
            ch_start += int(ch_page.get("maxResults", len(ch_page.get("values", [])) or 1))
            time.sleep(delay_s)

        issue["changelog"]["histories"] = histories

    out_path = os.path.join(out_dir, f"{key}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(issue, fh)

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Jira issues for a project.")
    parser.add_argument("--project", required=True, help="Jira project key, e.g. SPARK")
    parser.add_argument("--from-date", default="2018-01-01")
    parser.add_argument("--to-date", default="2024-01-01")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--delay-ms", type=int, default=400)
    args = parser.parse_args()

    delay_s = args.delay_ms / 1000.0
    out_dir = f"./extract_out/raw/{args.project}"
    chk_file = os.path.join(out_dir, ".checkpoint.txt")
    log_file = os.path.join(out_dir, ".extraction.log")

    os.makedirs(out_dir, exist_ok=True)
    logger = setup_logging(log_file)

    # Load checkpoint.
    done: set[str] = set()
    if os.path.exists(chk_file):
        with open(chk_file, encoding="utf-8") as fh:
            for line in fh:
                key = line.strip()
                if key:
                    done.add(key)
        logger.info("Resuming: %d issues already extracted.", len(done))

    all_keys = enumerate_keys(
        args.project, args.from_date, args.to_date,
        args.batch_size, delay_s, logger,
    )

    fetched = skipped = failed = 0

    for key in all_keys:
        if key in done:
            skipped += 1
            continue

        ok = fetch_issue(key, out_dir, delay_s, logger)
        if not ok:
            failed += 1
        else:
            with open(chk_file, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")
            fetched += 1

        if fetched % 50 == 0 and fetched > 0:
            logger.info(
                "Fetched %d / %d (skipped=%d, failed=%d)",
                fetched, len(all_keys), skipped, failed,
            )

        time.sleep(delay_s)

    logger.info("Extraction complete. Fetched=%d Skipped=%d Failed=%d", fetched, skipped, failed)
    logger.info("Raw files in: %s", out_dir)


if __name__ == "__main__":
    main()
