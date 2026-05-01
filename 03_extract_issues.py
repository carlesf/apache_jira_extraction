"""
03_extract_issues.py
----------------------------------------------------------------------------
Extraction in three stages:

  Pass 1 — Requirements
    Enumerates and fetches all requirement-type issues (Story, New Feature,
    Improvement, Epic) for the project in the target time window.

  Pass 2a — Subtasks
    Scans every fetched requirement for subtask keys (from the `subtasks`
    field) and fetches those child issues regardless of their type.

  Pass 2b — Epic-linked children
    Finds issues in the same project whose Epic Link points to a fetched Epic
    from Pass 1, and fetches those child issues regardless of their type.

The single checkpoint file covers all stages, so the script is fully
resumable: rerunning the same command skips already-fetched keys.

Features:
  - Resumable via .checkpoint.txt
  - Exponential backoff on HTTP 429/5xx
  - One JSON file per issue (easy to inspect, easy to resume)

Usage:
  python 03_extract_issues.py --project SPARK
  python 03_extract_issues.py --project SPARK --from-date 2022-01-01 --to-date 2026-01-01
----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Dict, List, Optional, Set
from urllib.parse import quote

import requests

BASE_URL = "https://issues.apache.org/jira"
# Requirement types only for Pass 1; Pass 2 fetches children with no type filter
TYPE_FILTER = 'issuetype in (Story, "New Feature", Improvement, Epic)'
MAX_RETRIES = 5
EPIC_LINK_BATCH_SIZE = 25


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


def get_jira_json(url: str, logger: logging.Logger, delay_s: float) -> Optional[dict]:
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


def post_jira_json(url: str, payload: Dict, logger: logging.Logger, delay_s: float) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=60)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 404:
                logger.warning("404 (resource unavailable or deleted): %s", url)
                return None

            if resp.status_code in (429, 502, 503, 504):
                wait = min(60, 2 ** attempt)
                logger.warning("HTTP %d on attempt %d. Waiting %ds.", resp.status_code, attempt, wait)
                time.sleep(wait)
                continue

            logger.error("HTTP %d (not retrying): %s", resp.status_code, url)
            logger.debug("POST payload: %s", json.dumps(payload, ensure_ascii=False))
            return None

        except Exception as exc:
            wait = min(60, 2 ** attempt)
            logger.warning("Exception on attempt %d: %s. Waiting %ds.", attempt, exc, wait)
            time.sleep(wait)

    logger.error("Max retries exceeded: %s", url)
    return None


def enumerate_requirement_keys(project: str, from_date: str, to_date: str,
                               batch_size: int, delay_s: float,
                               logger: logging.Logger) -> List[str]:
    jql = (
        f'project = {project} AND created >= "{from_date}" AND created < "{to_date}"'
        f" AND {TYPE_FILTER} ORDER BY created ASC"
    )
    encoded = quote(jql)

    all_keys: List[str] = []
    start_at = 0
    total = -1

    logger.info("Pass 1: enumerating requirements in project=%s window=[%s, %s)",
                project, from_date, to_date)

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
            logger.info("Total requirement issues: %d", total)

        start_at += batch_size
        logger.info("Enumerated %d/%d", min(start_at, total), total)
        time.sleep(delay_s)

        if start_at >= total:
            break

    logger.info("Pass 1 enumeration complete: %d keys.", len(all_keys))
    return all_keys


def collect_subtask_keys_from_requirements(out_dir: str, requirement_keys: List[str],
                                           logger: logging.Logger) -> Set[str]:
    """
    Scan fetched Pass 1 requirement JSONs and collect direct subtask keys.
    This intentionally does not scan child issues from previous runs.
    """
    child_keys: Set[str] = set()
    inspected_requirements = 0

    for key in requirement_keys:
        path = os.path.join(out_dir, f"{key}.json")
        if not os.path.isfile(path):
            continue

        try:
            with open(path, encoding="utf-8") as fh:
                issue = json.load(fh)
        except Exception as exc:
            logger.warning("Could not inspect fetched requirement %s for subtasks: %s", key, exc)
            continue

        inspected_requirements += 1
        for subtask in issue.get("fields", {}).get("subtasks", []):
            subtask_key = subtask.get("key")
            if subtask_key:
                child_keys.add(subtask_key)

    logger.info("Pass 2a: inspected %d fetched Pass 1 requirement files.", inspected_requirements)
    return child_keys


def collect_fetched_epic_keys(out_dir: str, requirement_keys: List[str],
                              logger: logging.Logger) -> Set[str]:
    """
    Return fetched Pass 1 requirement keys whose issue type is Epic.
    Missing files are expected after interrupted runs or unavailable issues.
    """
    epic_keys: Set[str] = set()
    fetched_requirements = 0

    for key in requirement_keys:
        path = os.path.join(out_dir, f"{key}.json")
        if not os.path.isfile(path):
            continue

        try:
            with open(path, encoding="utf-8") as fh:
                issue = json.load(fh)
        except Exception as exc:
            logger.warning("Could not inspect fetched requirement %s: %s", key, exc)
            continue

        fetched_requirements += 1
        issue_type = issue.get("fields", {}).get("issuetype", {}).get("name")
        if issue_type == "Epic":
            epic_keys.add(key)

    logger.info("Fetched requirement issues available locally: %d", fetched_requirements)
    logger.info("Fetched Epics available for Epic Link discovery: %d", len(epic_keys))
    return epic_keys


def _batches(items: List[str], batch_size: int) -> List[List[str]]:
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def search_epic_link_child_keys(project: str, epic_keys: Set[str],
                                search_batch_size: int, epic_link_batch_size: int,
                                delay_s: float,
                                logger: logging.Logger) -> Set[str]:
    """
    Search children whose Epic Link points to fetched Epics.
    Uses POST and small Epic-key batches to avoid long GET URLs.
    """
    child_keys: Set[str] = set()
    search_url = f"{BASE_URL}/rest/api/2/search"
    sorted_epics = sorted(epic_keys)

    if not sorted_epics:
        logger.info("Pass 2b: no fetched Epics found; skipping Epic Link discovery.")
        return child_keys

    logger.info(
        "Pass 2b: searching Epic-linked children for %d Epics in batches of %d.",
        len(sorted_epics), epic_link_batch_size,
    )

    for batch_num, epic_batch in enumerate(_batches(sorted_epics, epic_link_batch_size), start=1):
        epic_list = ", ".join(epic_batch)
        jql = f'project = {project} AND "Epic Link" in ({epic_list}) ORDER BY created ASC'
        start_at = 0
        total = -1

        while True:
            payload = {
                "jql": jql,
                "fields": ["key"],
                "maxResults": search_batch_size,
                "startAt": start_at,
            }
            page = post_jira_json(search_url, payload, logger, delay_s)
            if page is None:
                sample = ", ".join(epic_batch[:3])
                suffix = "..." if len(epic_batch) > 3 else ""
                logger.error(
                    "Pass 2b batch %d failed at startAt=%d for %d Epic keys (%s%s); "
                    "skipping the rest of this batch and continuing.",
                    batch_num, start_at, len(epic_batch), sample, suffix,
                )
                break

            for issue in page.get("issues", []):
                key = issue.get("key")
                if key:
                    child_keys.add(key)

            if total < 0:
                total = int(page.get("total", 0))
                logger.info(
                    "Pass 2b batch %d: %d Epic-linked issues matched.",
                    batch_num, total,
                )

            start_at += search_batch_size
            time.sleep(delay_s)

            if start_at >= total:
                break

    logger.info("Pass 2b: discovered %d unique Epic-linked child keys.", len(child_keys))
    return child_keys


def fetch_issue(key: str, out_dir: str, delay_s: float, logger: logging.Logger) -> bool:
    url = f"{BASE_URL}/rest/api/2/issue/{key}?expand=changelog"
    issue = get_jira_json(url, logger, delay_s)
    if issue is None:
        return False

    # Paginate changelog if truncated.
    changelog = issue.get("changelog", {})
    if changelog.get("total", 0) > changelog.get("maxResults", 0):
        histories: List = list(changelog.get("histories", []))
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


def run_pass(keys: List[str], label: str, out_dir: str, chk_file: str,
             done: Set[str], delay_s: float, logger: logging.Logger) -> int:
    fetched = skipped = failed = 0

    for key in keys:
        if key in done:
            skipped += 1
            continue

        ok = fetch_issue(key, out_dir, delay_s, logger)
        if not ok:
            failed += 1
        else:
            done.add(key)
            with open(chk_file, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")
            fetched += 1

        if fetched % 50 == 0 and fetched > 0:
            logger.info(
                "%s: fetched %d/%d (skipped=%d, failed=%d)",
                label, fetched, len(keys), skipped, failed,
            )

        time.sleep(delay_s)

    logger.info("%s complete. Fetched=%d Skipped=%d Failed=%d", label, fetched, skipped, failed)
    return fetched


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Jira requirement issues and their child tasks."
    )
    parser.add_argument("--project", required=True, help="Jira project key, e.g. SPARK")
    parser.add_argument("--from-date", default="2022-01-01")
    parser.add_argument("--to-date", default="2026-01-01",
                        help="Exclusive upper bound (default covers through end of 2025)")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--epic-link-batch-size", type=int, default=EPIC_LINK_BATCH_SIZE,
        help=f"Number of Epic keys per Epic Link search batch (default: {EPIC_LINK_BATCH_SIZE})",
    )
    parser.add_argument("--delay-ms", type=int, default=400)
    args = parser.parse_args()

    if args.epic_link_batch_size < 1:
        parser.error("--epic-link-batch-size must be >= 1")

    delay_s  = args.delay_ms / 1000.0
    out_dir  = f"./extract_out/raw/{args.project}"
    chk_file = os.path.join(out_dir, ".checkpoint.txt")
    log_file = os.path.join(out_dir, ".extraction.log")

    os.makedirs(out_dir, exist_ok=True)
    logger = setup_logging(log_file)

    # Load checkpoint (shared across all passes).
    done: Set[str] = set()
    if os.path.exists(chk_file):
        with open(chk_file, encoding="utf-8") as fh:
            for line in fh:
                key = line.strip()
                if key:
                    done.add(key)
        logger.info("Resuming: %d issues already extracted.", len(done))

    # Pass 1: requirements.
    req_keys = enumerate_requirement_keys(
        args.project, args.from_date, args.to_date,
        args.batch_size, delay_s, logger,
    )
    run_pass(req_keys, "Pass 1 (requirements)", out_dir, chk_file, done, delay_s, logger)

    # Pass 2a: subtasks referenced by the fetched requirements.
    logger.info("Pass 2a: collecting subtask keys from fetched requirements ...")
    child_keys = collect_subtask_keys_from_requirements(out_dir, req_keys, logger)
    new_child_keys = sorted(child_keys - done)
    logger.info("Found %d subtask child keys total, %d not yet fetched.",
                len(child_keys), len(new_child_keys))

    if new_child_keys:
        run_pass(new_child_keys, "Pass 2a (subtasks)", out_dir, chk_file, done, delay_s, logger)
    else:
        logger.info("Pass 2a: nothing to fetch.")

    # Pass 2b: children linked to fetched Epics via Epic Link.
    epic_keys = collect_fetched_epic_keys(out_dir, req_keys, logger)
    epic_child_keys = search_epic_link_child_keys(
        args.project, epic_keys, args.batch_size, args.epic_link_batch_size,
        delay_s, logger,
    )
    new_epic_child_keys = sorted(epic_child_keys - done)
    logger.info(
        "Pass 2b: found %d Epic-linked child keys total, %d not yet fetched.",
        len(epic_child_keys), len(new_epic_child_keys),
    )

    if new_epic_child_keys:
        fetched_epic_children = run_pass(
            new_epic_child_keys,
            "Pass 2b (Epic-linked children)",
            out_dir,
            chk_file,
            done,
            delay_s,
            logger,
        )
        logger.info("Pass 2b: newly fetched Epic-linked children: %d", fetched_epic_children)
    else:
        logger.info("Pass 2b: no new Epic-linked children to fetch.")
        logger.info("Pass 2b: newly fetched Epic-linked children: 0")

    logger.info("All done. Raw files in: %s", out_dir)


if __name__ == "__main__":
    main()
