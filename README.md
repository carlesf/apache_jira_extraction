# Apache Jira Extraction Pipeline — Task Decomposition Dataset

## Goal

Extract a clean dataset from `https://issues.apache.org/jira` suitable for
fine-tuning an LLM on **task decomposition**: given a requirement (Epic, Story,
New Feature, or Improvement), generate the set of concrete tasks that implement it.

Each training example consists of:

- **Input** — a requirement issue: title, description, components, labels,
  fix versions, priority, and issue type.
- **Output** — the list of child tasks derived from that requirement, each with
  the same fields plus their parent relationship provenance and dependency links.

### Scope

| Dimension | Choice |
|-----------|--------|
| Source | Apache Software Foundation Jira (`issues.apache.org/jira`) |
| Issue types (requirements) | `Story`, `New Feature`, `Improvement`, `Epic` |
| Issue types (tasks) | any type present in the clean index |
| Projects | Active Apache projects, selected after measuring coverage (Step 2) |
| Time window | 2022-01-01 to 2025-12-31 (inclusive) |
| API | Public Jira REST API v2 — no authentication required |

Bugs and Tasks are excluded from the requirement side. Story points and
time estimates are collected when present, but are usually sparse in Apache Jira.

---

## Pipeline Overview

| Step | Script | Input | Output | Typical runtime |
|------|--------|-------|--------|-----------------|
| 1 | `01_discover_custom_fields.py` | — | `extract_out/field_map.json` | < 5 s |
| 2 | `02_measure_project_coverage.py` | candidate project list | `extract_out/project_coverage.csv` | 2–5 min |
| 3 | `03_extract_issues.py` | one project key | `extract_out/raw/<PROJECT>/<KEY>.json` | 1–6 h per project |
| 4 | `04_reconstruct_features.py` | raw per-project dir | `extract_out/clean/<PROJECT>.jsonl` + `summary.csv` | 5–15 min per project |
| 5 | `05_build_finetuning_pairs.py` | clean JSONL for all projects | `extract_out/dataset/decomposition_pairs.jsonl` + `pairs_summary.csv` | < 5 min |

Directory layout after running all five steps:

```
extract_out/
  field_map.json
  project_coverage.csv
  raw/
    SPARK/
      SPARK-30001.json
      ...
      .checkpoint.txt
      .extraction.log
  clean/
    SPARK.jsonl
    SPARK.summary.csv
    ...
  dataset/
    decomposition_pairs.jsonl
    pairs_summary.csv
```

---

## Prerequisites

- Python 3.8.10+
- `pip install requests`
- ~1 GB free disk per project (raw JSON is verbose)
- Stable internet; Step 3 is resumable if interrupted

---

## Step 1 — Discover Custom Field IDs

Jira custom fields like *Epic Link* are stored under instance-specific IDs
such as `customfield_12311120`. These IDs differ per Jira installation and
cannot be hardcoded. Step 1 resolves the field names to their IDs on the
Apache instance.

```bash
python 01_discover_custom_fields.py
```

Output: `extract_out/field_map.json`. Inspect it; you should see entries for
`Epic Link` and `Epic Name`. `Fix Version/s` is a built-in field and may
not appear in the customfield map — that is expected.

---

## Step 2 — Measure Project Coverage

Rather than maintaining a hardcoded candidate list, the script discovers
every project on the Apache Jira instance automatically and works in two
phases.

```bash
python 02_measure_project_coverage.py               # defaults
python 02_measure_project_coverage.py --min-issues 50 --delay-ms 500
```

**Phase 1 — Discovery.** Fetches the full project list from
`/rest/api/2/project` (one API call), then issues one `maxResults=0` count
query per project to check how many requirement issues it has in the
2022–2025 window. Projects below `--min-issues` (default: 100) are dropped.
At 400 ms per call and ~400 Apache projects this takes roughly 3 minutes.

**Phase 2 — Coverage measurement.** For each project that survived Phase 1,
three more count queries are issued (still `maxResults=0`, no issues
downloaded):

| Metric | Meaning |
|--------|---------|
| `Total` | Requirement issues in the 2022–2025 window |
| `Desc_Rate` | Fraction with a non-empty description |
| `Comp_Rate` | Fraction tagged with at least one component |
| `Link_Rate` | Fraction with at least one issue link |

Output: `extract_out/project_coverage.csv`, sorted by `Desc_Rate` descending.

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--min-issues` | `100` | Minimum requirement issues to pass Phase 1 |
| `--delay-ms` | `400` | Inter-request delay in milliseconds |

**Selection rule of thumb:** `Desc_Rate >= 0.50 AND (Link_Rate >= 0.15 OR Comp_Rate >= 0.30)`.
Pick three to five projects that clear this bar.

---

## Step 3 — Full Issue Extraction

For each selected project, run the main extraction. This is the long step.

```bash
python 03_extract_issues.py --project SPARK
python 03_extract_issues.py --project FLINK
python 03_extract_issues.py --project KAFKA
```

The script runs in three stages:

**Pass 1 — Requirements.** A paginated JQL search collects all requirement-type
issues (`Story`, `New Feature`, `Improvement`, `Epic`) created in the target
window, then fetches each one in full (`GET /issue/{key}?expand=changelog`).

**Pass 2a — Subtasks.** After Pass 1, the script scans only the fetched Pass 1
requirement JSON files for subtask keys (from the `subtasks` field) and
fetches those child issues without any type filter.

**Pass 2b — Epic-linked children.** The script also detects fetched Epics and
searches for issues in the same project whose `Epic Link` points to those
Epics. This catches Task, Bug, Story, and other child issue types that may not
appear in Pass 1 and are not represented as native subtasks, improving
coverage for Epic → child decomposition pairs. Epic Link searches are batched
and sent with POST requests to avoid long Jira search URLs. The child
collection stages are one-hop coverage improvements, not recursive graph
expansion.

A single checkpoint file (`raw/<PROJECT>/.checkpoint.txt`) records every
fetched key across all passes. Rerunning the same command resumes exactly
where it left off.

**Rate limiting:** a 400 ms inter-request delay is built in. The Apache
instance enforces HTTP 429 under load; the script retries with exponential
backoff up to 5 attempts.

**Changelog pagination:** issues with > 100 history entries return only the
first 100 in the expanded search response. The script detects this and
follows up on the dedicated changelog endpoint automatically.

---

## Step 4 — Reconstruct Creation-Time Features

The raw JSON contains current field values plus the full changelog. Step 4
walks the changelog to recover field values that were in place at issue
creation time, and writes one clean record per issue.

```bash
python 04_reconstruct_features.py --project SPARK
python 04_reconstruct_features.py --project FLINK
python 04_reconstruct_features.py --project KAFKA
```

Output per project:

- `clean/<PROJECT>.jsonl` — one JSON object per line (schema below)
- `clean/<PROJECT>.summary.csv` — population rate for each field

Review the summary. `has_description` below 0.50 is a signal that the project
may not be worth including. `has_parent` below 0.20 means few parent-child
relationships exist, which limits how many pairs Step 5 can produce.

### Output schema (`clean/<PROJECT>.jsonl`)

```json
{
  "key":                  "SPARK-40001",
  "project":              "SPARK",
  "type":                 "Story",
  "status":               "Resolved",
  "resolution":           "Fixed",
  "created_at":           "2023-03-15T09:12:44.000+0000",
  "resolved_at":          "2023-04-02T16:55:01.000+0000",
  "title":                "Add support for ANSI interval type in Parquet reader",
  "description":          "Currently the Parquet reader does not handle ...",
  "priority_at_creation": "Major",
  "story_points":         3.0,
  "original_estimate_h":  4.0,
  "time_spent_h":         5.0,
  "components":           ["SQL", "Input/Output"],
  "labels":               ["correctness"],
  "fix_versions":         ["3.5.0"],
  "parent_key":           "SPARK-38000",
  "parent_relation":      "epic_link",
  "subtasks":              ["SPARK-40002", "SPARK-40003"],
  "dependency_links": [
    {
      "type":      "Blocks",
      "direction": "outward",
      "target":    "SPARK-40050",
      "added_at":  "2023-03-15T09:12:44.000+0000"
    }
  ]
}
```

`parent_relation` records how `parent_key` was reconstructed:

- `subtask_parent` — from Jira's native `fields.parent` relationship
- `epic_link` — from the instance-specific Epic Link custom field
- `null` — no parent relationship was found

---

## Step 5 — Build Fine-Tuning Pairs

Step 5 joins parent requirement issues with their child tasks across all
extracted projects and writes one training example per parent–children group.

```bash
# All projects found in extract_out/clean/
python 05_build_finetuning_pairs.py

# Specific projects
python 05_build_finetuning_pairs.py --projects SPARK FLINK KAFKA

# Stricter filters
python 05_build_finetuning_pairs.py --min-tasks 2 --resolved-only
python 05_build_finetuning_pairs.py --min-tasks 2 --resolved-only --children-completed-only

# Cleaner general set from richer Epic-linked extraction
python 05_build_finetuning_pairs.py --projects IGNITE --min-tasks 2 --max-tasks 12 --min-quality-score 3

# Exclude noisy child issue types
python 05_build_finetuning_pairs.py --projects IGNITE --min-tasks 2 --max-tasks 12 --min-quality-score 3 --exclude-child-types Bug Test

# Feature-to-task pairs only
python 05_build_finetuning_pairs.py --projects IGNITE --decomposition-level feature_to_task --min-tasks 2 --max-tasks 12 --min-quality-score 3
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--projects` | all in `clean/` | Project keys to include |
| `--min-tasks` | `1` | Minimum number of tasks a requirement must have |
| `--max-tasks` | off | Maximum number of children allowed after child-level filters |
| `--min-quality-score` | off | Minimum `quality_score` required to keep a pair; values like `2` or `3` are useful for cleaner datasets |
| `--decomposition-level` | off | Keep only `epic_to_feature` or `feature_to_task` pairs |
| `--exclude-child-types` | off | Remove children of the listed issue types before task-count filters; matching is case-insensitive |
| `--no-require-description` | off | Include requirements even without a description |
| `--resolved-only` | off | Only include parent requirements completed by status (`Resolved`, `Closed`, `Done`) or resolution (`Fixed`, `Done`) |
| `--children-completed-only` | off | Only keep completed children before applying `--min-tasks` |

Filters are applied in this practical order: parent type, required parent
description, parent completion for `--resolved-only`, child collection,
`--children-completed-only`, `--exclude-child-types`, `--min-tasks`,
`--max-tasks`, `--decomposition-level`, then `--min-quality-score`.
`quality_score` and `decomposition_level` are computed once for each surviving
candidate pair. The summary CSV includes skip counters for max-task,
low-quality, decomposition-level, and child-exclusion effects.

### How children are collected

Two sources are merged and deduplicated for each parent:

1. **Back-links** — all issues in the index whose `parent_key` equals the
   parent's key. This covers Epic → Story relationships via the Epic Link
   custom field and subtask relationships via native Jira parent fields.
2. **Subtask keys** — keys listed in the parent's `subtasks` field that also
   appear in the index.

Children absent from the index are silently excluded. The `pairs_summary.csv`
reports how many requirements were skipped because no in-index children were
found (`Skipped_No_Tasks`).

### Output schema (`dataset/decomposition_pairs.jsonl`)

```json
{
  "id":                  "SPARK-38000",
  "project":             "SPARK",
  "decomposition_level": "epic_to_feature",
  "quality_score":       4,
  "requirement": {
    "key":         "SPARK-38000",
    "type":        "Epic",
    "title":       "Unified interval type support",
    "description": "This epic tracks all work needed to ...",
    "components":  ["SQL"],
    "labels":      [],
    "fix_versions": ["3.5.0"],
    "priority":    "Major"
  },
  "tasks": [
    {
      "key":              "SPARK-40001",
      "type":             "Story",
      "title":            "Add support for ANSI interval type in Parquet reader",
      "description":      "Currently the Parquet reader does not handle ...",
      "components":           ["SQL", "Input/Output"],
      "labels":               ["correctness"],
      "fix_versions":         ["3.5.0"],
      "parent_relation":      "epic_link",
      "story_points":         3.0,
      "original_estimate_h":  4.0,
      "time_spent_h":         5.0,
      "dependency_links":     []
    },
    {
      "key":                  "SPARK-40005",
      "type":                 "Improvement",
      "title":                "Add interval type casting in Catalyst",
      "description":          "Catalyst currently throws on INTERVAL ...",
      "components":           ["SQL"],
      "labels":               [],
      "fix_versions":         ["3.5.0"],
      "parent_relation":      "epic_link",
      "story_points":         null,
      "original_estimate_h":  null,
      "time_spent_h":         null,
      "dependency_links":     []
    }
  ]
}
```

`decomposition_level` is a coarse label for the hierarchy represented by the
pair. Parents of type `Epic` are labelled `epic_to_feature`; all other
requirement parents are labelled `feature_to_task`.

`quality_score` is a small integer heuristic for filtering or stratifying
pairs. The score adds one point each for: parent description present, 2–12
children, at least half of children having descriptions, and comparable child
creation timestamps not preceding the parent creation timestamp. It subtracts
one point each for more than 20 children and for any child typed `Bug`.

---

## Things to Pay Attention To

**Customfield IDs are not stable across Jira instances.** Never hardcode them.
Step 1 resolves them at runtime. If you later extract from a different Jira
instance, re-run Step 1.

**The Jira `created` field is the authoritative creation timestamp**, not the
first changelog entry. Changelog entries record *changes*, so no entry exists
for fields set at creation and never modified. The reconstruction rule in
Step 4 handles this: if no change history exists for a field, the current
value is the creation value.

**Effort fields are sparsely populated.**
Apache projects rarely enforce time tracking or story points, so expect low
coverage on `story_points`, `original_estimate_h`, and `time_spent_h` (often
under 20%). `original_estimate_h` reflects the planned effort in hours and is
meaningful as a training target; `time_spent_h` is the total time logged and
is inherently post-hoc — treat it as a ground-truth label rather than a
planning input. All three are `null` when not set.

**Issue links are bidirectional and duplicated.** If SPARK-A "blocks" SPARK-B,
both issues record the link from their own perspective. When building a
dependency graph, deduplicate on `(min(source, target), max(source, target), type)`.

**Post-resolution links are documentation, not planning.** Some links are added
months after resolution. For tasks where ordering matters, filter links where
`added_at` is after the parent's `resolved_at`. Step 4 records both timestamps
but does not filter; that decision belongs downstream.

**Descriptions use Jira wiki markup, not Markdown.** Do not apply Markdown
parsers. Either keep as-is or strip markup with a Jira-specific converter at
preprocessing time.

**Descriptions can be very long.** Issues with stack traces or design docs
can reach 50 KB. Decide on a truncation policy at preprocessing time, not
extraction time.

**Changelog is paginated for long-lived issues.** An issue with > 100 history
entries returns only the first 100 in the expanded search response. Step 3
detects `changelog.total > changelog.maxResults` and follows up on the
dedicated changelog endpoint.

**Deleted and access-restricted issues return 404.** These are silently
skipped and logged. Expect a small number per project (security-embargoed
issues in Apache are uncommon but exist).

**Rate limits are real.** The 400 ms delay is conservative. Reducing it
below 200 ms triggers HTTP 429. The exponential backoff will recover, but
overall throughput drops.

**UTF-8 without BOM.** The scripts write BOMless UTF-8. Avoid piping output
through `Set-Content -Encoding UTF8` on Windows PowerShell 5.1, which adds a
BOM that breaks JSON Lines readers.

---

## What Comes After

The `decomposition_pairs.jsonl` is the extraction output. Three things happen
next, all outside this pipeline:

1. **Train / eval split.** Split by time (train on 2022–2023, evaluate on
   2024–2025) and optionally by project (held-out project as zero-shot test).
   Do not use a random shuffle — temporal ordering matters for leakage
   prevention.

2. **Prompt construction.** Format each pair into an instruction template,
   e.g., Alpaca-style or a chat template. The `requirement` object is the
   input; the `tasks` array is the target output. Decide how to represent the
   task list (numbered prose, JSON, structured YAML) based on the model and
   evaluation protocol.

3. **Component-aware filtering.** The `components` field on both requirement
   and tasks enables filtering or stratification by system area (e.g., evaluate
   separately on SQL-only pairs vs. I/O pairs) to measure component-level
   generalisation.
