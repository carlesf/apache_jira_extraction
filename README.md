# Apache Jira Extraction Pipeline for TaskGenAgent Fine-Tuning

## Goal

Extract a clean, leakage-free dataset from `https://issues.apache.org/jira` suitable
for fine-tuning the TaskGenAgent. Each record must contain:

- **Inputs** observable at task creation (title, description, type, components,
  labels, parent link)
- **Targets** that represent planning decisions (priority at creation,
  story points at planning, original effort estimate, dependency links)
- **No post-resolution fields** (no `resolved_at`, `is_resolved`, `status`,
  `duration_hours` computed from end state)

The pipeline uses only the public Jira REST API v2. No authentication is needed
for reads, but rate limiting is required.

---

## Pipeline Overview

| Step | Script | Input | Output | Typical Runtime |
|------|--------|-------|--------|-----------------|
| 1 | `01_discover_custom_fields.py` | none | `field_map.json` | < 5 s |
| 2 | `02_measure_project_coverage.py` | candidate project list | `project_coverage.csv` | 2–5 min |
| 3 | `03_extract_issues.py` | one project key | `raw/<PROJECT>/<KEY>.json` (one file per issue) | 1–6 h per project |
| 4 | `04_reconstruct_features.py` | raw per-project dir | `clean/<PROJECT>.jsonl` + `summary.csv` | 5–15 min per project |

Directory layout after running all four:

```
extract_out/
  field_map.json
  project_coverage.csv
  raw/
    SPARK/
      SPARK-10001.json
      SPARK-10002.json
      ...
      .checkpoint.txt
  clean/
    SPARK.jsonl
    SPARK.summary.csv
```

---

## Prerequisites

- Python 3.11+
- Install dependencies: `pip install -r requirements.txt`
- ~2 GB free disk per project (raw JSON is verbose)
- Stable internet; the extraction is resumable but benefits from uninterrupted runs

---

## Step 1. Discover Custom Field IDs

Jira custom fields like *Story Points* and *Epic Link* are stored under
project-instance-specific IDs such as `customfield_12310243`. These IDs differ
per Jira installation. Step 1 resolves the human-readable names to their numeric
IDs on the Apache instance.

```bash
python 01_discover_custom_fields.py
```

Output: `extract_out/field_map.json`, a JSON object mapping names to field IDs.
Inspect it. You should see entries for *Story Points*, *Epic Link*, *Epic Name*,
and *Sprint*. If any are missing, that field does not exist on the Apache
instance (for example, *Original Estimate* is a built-in field `timeoriginalestimate`
and will not appear in the customfield map — that is expected).

---

## Step 2. Measure Project Coverage

The candidate projects are Apache projects with sustained Agile practice. Before
committing to a full extraction, check how often the planning fields are
populated in each project in the target time window.

```bash
python 02_measure_project_coverage.py
```

This queries ~15 projects with five diagnostic counts each. It does not
download any issues; it uses `maxResults=0` to retrieve only the total count.

Output: `extract_out/project_coverage.csv`, sortable by `SP_Rate` (story points
coverage) and `Link_Rate` (share of issues with Blocks/Depends links).

**Selection criteria:** pick three to five projects where `SP_Rate >= 0.30`
and `Link_Rate >= 0.10`. If no project clears the story-points bar, reduce the
threshold or drop story points as a target — do not substitute `duration_hours`
as happened in the previous pipeline.

---

## Step 3. Full Issue Extraction

For each selected project, run the main extraction. This is the long step.

```bash
python 03_extract_issues.py --project SPARK --from-date 2018-01-01 --to-date 2024-01-01
python 03_extract_issues.py --project FLINK --from-date 2018-01-01 --to-date 2024-01-01
python 03_extract_issues.py --project BEAM  --from-date 2018-01-01 --to-date 2024-01-01
```

The script works in two passes:

1. **Enumerate**: paginated JQL search (`fields=key` only) to collect all
   matching issue keys. Cheap.
2. **Fetch**: per-issue `GET /issue/{key}?expand=changelog`, with a follow-up
   paginated call to `/issue/{key}/changelog` for issues with > 100 history
   entries.

Each issue is saved as one JSON file under `raw/<PROJECT>/`. A checkpoint file
`raw/<PROJECT>/.checkpoint.txt` records extracted keys — if the script is
interrupted, rerun the same command and it will resume.

**Rate limiting:** a 400 ms delay between calls is hard-coded. The Apache
instance is generous but can return HTTP 429 under load; the script retries
with exponential backoff.

**Expected volume:** a mid-size project (e.g., FLINK with ~30,000 issues in the
window) produces ~1 GB of raw JSON and runs for several hours. Start with a
smaller window or a smaller project to validate the pipeline end-to-end.

---

## Step 4. Reconstruct Creation-Time Features

The raw JSON contains current field values plus the full changelog. Step 4
walks the changelog to recover the values that were in place at issue creation.

```bash
python 04_reconstruct_features.py --project SPARK
```

The script produces two files per project:

- `clean/<PROJECT>.jsonl` — one issue per line, in the final schema
- `clean/<PROJECT>.summary.csv` — population rates for each field (sanity check)

Review the summary. If the `priority_at_creation` reconstruction recovered a
value in < 98% of issues, there is a changelog parsing bug — report back.

---

## Output Schema (per line in `<PROJECT>.jsonl`)

```json
{
  "key": "SPARK-30001",
  "project": "SPARK",
  "created_at": "2020-05-12T14:23:01.000+0000",
  "type": "Bug",
  "title": "NPE when reading Parquet with corrupted footer",
  "description": "Steps to reproduce ...",
  "components": ["SQL", "Input/Output"],
  "labels": ["correctness"],
  "parent_key": "SPARK-29000",
  "affects_versions": ["3.0.0"],
  "reporter_anon": "3f2a...e1",

  "priority_at_creation": "Major",
  "story_points_planning": 3.0,
  "original_estimate_sec": null,
  "dependency_links": [
    { "type": "Blocks",   "direction": "outward", "target": "SPARK-30050", "added_at": "2020-05-12T14:23:01.000+0000" },
    { "type": "Duplicate","direction": "inward",  "target": "SPARK-29700", "added_at": "2020-05-13T08:11:22.000+0000" }
  ],
  "subtasks": []
}
```

Note that `created_at` is kept for splitting and filtering downstream but should
be **excluded from the model input** at training time to prevent the temporal
leakage that affected the previous pipeline.

---

## Things to Pay Attention To

**Customfield IDs are not stable across Jira instances.** Never hardcode them.
Step 1 exists for this reason. If you extract from a different Jira later
(e.g., a Taiga migration target), re-run Step 1.

**The Jira `created` field is the authoritative creation timestamp**, not the
first changelog entry. Changelog entries record *changes*, so no entry exists
for fields that were set at creation and never modified. The reconstruction
rule in Step 4 handles this: if no change history exists for a field, the
current value is the creation value.

**Issue links are bidirectional and duplicated.** If SPARK-A "blocks" SPARK-B,
both issues list the link from their own perspective. When building the
dependency graph, deduplicate on `(min(source, target), max(source, target), type)`.

**Post-resolution links are documentation, not planning.** Some links are added
months after an issue resolves (e.g., "is duplicated by" when a later issue is
filed). For dependency prediction, filter links where `added_at` is after the
issue's first status transition to Resolved/Closed. Step 4 records `added_at`
but does not filter — that decision belongs downstream.

**Description fields can be very long.** Apache bugs with stack traces reach
50+ KB. Decide your truncation policy at preprocessing time, not extraction
time. Keep the raw text in the extract.

**The description uses old Jira wiki markup, not Markdown.** Do not apply
Markdown parsers; either keep as-is or strip markup with a Jira-specific
converter at preprocessing time.

**Changelog is paginated for long-lived issues.** An issue with 200+ history
entries returns only the first 100 in the expanded search response. Step 3
detects `changelog.total > changelog.maxResults` and follows up on the dedicated
changelog endpoint. Confirm this worked by spot-checking a few long-lived issue
files and counting `changelog.histories` length against `changelog.total`.

**`reporter` and `assignee` are personal data under GDPR.** Hash them (Step 4
does SHA-256) or drop them. Do not publish the raw field. If you plan to release
the dataset, check the ASF data policy.

**Rate limits are real.** The 400 ms delay is conservative. If you reduce it
below 200 ms, you will see HTTP 429. The exponential backoff in the script
will recover, but the overall throughput will drop.

**UTF-8 with BOM breaks JSON Lines readers.** The scripts write BOMless UTF-8
via the .NET `UTF8Encoding($false)` constructor. If you pipe output through
`Set-Content -Encoding UTF8` on Windows PowerShell 5.1, you will get a BOM.
Do not do that.

**Deleted issues and access-restricted issues are silently skipped.** The API
returns 404 for them. This is normal for Apache projects — some issues are
security-embargoed. The script logs these; expect a small number per project.

**The time window matters for coverage.** Story points adoption in the Apache
community is a post-2015 phenomenon. A 2004–2005 window (as in AXISCPP)
guarantees zero story-points coverage. The recommended window is 2018–2024
for recency plus enough volume.

---

## What Comes After Extraction

The clean JSONL per project is the extraction output. Three things happen next,
all outside this pipeline:

1. **Train/eval split.** Split by time (train on older issues, evaluate on
   newer) and by project (train on projects A, B, C; evaluate on project D).
   The two splits together measure temporal generalisation and cross-project
   generalisation. Do not use a random shuffle.

2. **Backlog snapshot construction for dependency prediction.** For each issue
   at creation time `t`, build a candidate set of existing tasks in the same
   project that were open at `t`. Heuristic: same component, created within
   the previous 180 days, not yet resolved. This candidate set is the input
   over which the model ranks dependencies, replacing the ill-posed free-text
   generation of issue keys used in the previous pipeline. Natural to
   implement in Python once the JSONL is in hand.

3. **Preprocessing and prompt construction.** Decide description truncation,
   description markup handling, and the final Alpaca/chat template. This
   replaces the current pipeline's leakage-exposed prompt template.
