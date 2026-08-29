# Data Validation Framework

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![datacompy](https://img.shields.io/badge/powered%20by-datacompy-informational.svg)](https://github.com/capitalone/datacompy)
[![License](https://img.shields.io/badge/config-YAML--driven-lightgrey.svg)](#)

A **configuration-driven data validation tool** for comparing **source** vs **target** datasets across **CSV**, **Excel**, and **PostgreSQL/RDS** — purpose-built for reliable data migration validation.

Everything — what to compare, how to normalize it, and what to report — is defined declaratively in a single YAML file. Point it at a validation key and it:

1. Loads both sides of the comparison
2. Normalizes and compares them with [datacompy](https://github.com/capitalone/datacompy)
3. Detects duplicate join keys
4. Writes back detailed **TXT**, **HTML**, and **CSV** reports

No code changes required to add a new comparison.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Basic Example](#1-configure-validations)
  - [Supported Validation Types](#supported-validation-types)
  - [Output Options](#per-validation-output-options)
  - [RDS Secrets](#2-configure-rds-secrets-for-type-rds-validations-only)
- [Usage](#usage)
- [Project Structure](#project-structure)
- [How It Works](#how-it-works)
- [Key Config Concepts](#key-config-concepts)
- [Automatic Data Normalization](#automatic-data-normalization)
- [Duplicate Join-Key Detection](#duplicate-join-key-detection)
- [Extra Column Handling](#extra-column-handling)
- [Output Files](#output-files)
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [Design Notes](#design-notes)

---

## Features

| Category | Capability |
|---|---|
| **Sources** | CSV, Excel (`.xlsx`/`.xlsm`/`.xls`), PostgreSQL/RDS |
| **Data integrity** | Raw string reads — no automatic numeric/date type inference that could mask real differences (e.g. `007` → `7`, or reformatted dates) |
| **Schema flexibility** | Field name mapping — source and target can use different column names |
| **Reporting** | Per-validation control over `.txt`, `.html`, and mismatch CSV output |
| **Configuration** | Pure YAML — add new validations without touching code |
| **Batch-friendly** | Multiple validations per config file, run individually via `--validation` |
| **Observability** | Live progress table (via `tabulate`) + full per-run log file |
| **Normalization** | Auto-fixes trailing whitespace, NULL vs `""`, and `.0`-suffix numeric drift before comparing |
| **Tolerance** | Configurable `absolute_tolerance` / `relative_tolerance` for numeric comparisons |
| **Matched-row export** | Opt-in export of every fully-matched row for spot-checking or reconciliation |
| **Duplicate detection** | Opt-in, per-side detection of non-unique join keys — a common root cause of unreliable comparisons |
| **HTML reports** | Stakeholder-ready — status badge, KPI cards, source/target details, mismatch preview, and a "Data Fixes Applied" section |

---

## Requirements

- Python 3.9+
- Key dependencies (see `requirements.txt`, extend as needed for your environment):

  | Package | Purpose |
  |---|---|
  | `polars` | Raw, type-safe data reads |
  | `pandas` | Comparison engine backing (via datacompy) |
  | `datacompy` | Core row/column comparison logic |
  | `sqlalchemy` + `psycopg2-binary` | RDS/Postgres connectivity (`rds` validations) |
  | `pyyaml` | Config parsing |
  | `tabulate` | Live progress table |
  | `openpyxl` / `xlrd` | Excel read support (`.xlsx`/`.xlsm` / `.xls`) |
  | `python-dotenv` | `.env` secret loading |

## Installation

### 🐧 Linux & 🍏 macOS (Bash / Zsh)
```bash
git clone https://github.com/giteshdkolte/data-validation-framework.git
cd data-validation-framework
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 🪟 Windows (CMD / Powershell)
```cmd
git clone https://github.com/giteshdkolte/data-validation-framework.git
cd data-validation-framework
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configuration

The config file lives at `config/configuration.yaml`.

### 1. Configure validations

Common settings live under `default_settings` and are pulled into each validation via the YAML anchor `<<: *defaults` — override only what differs per validation.

```yaml
default_settings: &defaults
  db_name: validation_db
  schema_name: validation_schema
  absolute_tolerance: 0
  relative_tolerance: 0
  apply_fix_to_df: True
  ignore_extra_columns: True
  apply_fix_to_src_df_on_duplicate: True
  apply_fix_to_tgt_df_on_duplicate: True

output:
  directory: "data_validation"
  save_mismatches: true

compare:
  validation-1:
    <<: *defaults
    type: csv
    sep: ","
    ignore_extra_columns: False
    source: "../data_files/csv_case1_source.csv"
    target: "../data_files/csv_case1_target.csv"
    on_cols: ["record_id"]
    output:
      report_dir: "data_validation/validation-1"
      report_name: "compare_report"
      save_text: true
      save_html: true
      save_mismatches_csv: true
      save_matched_csv: true
      check_duplicate_on_source: true
      check_duplicate_on_target: true
    field_mapping:               # rename target columns to match source
      customer_name: cust_name   # source.cust_name <-> target.customer_name
      email: email_address
```

### Supported Validation Types

| Type    | Required keys                                                                 | Optional keys |
|---------|--------------------------------------------------------------------------------|---|
| `csv`   | `source`, `target`, `on_cols`                                                  | `sep` (default `,`) |
| `excel` | `source`, `target`, `on_cols`                                                  | `source_sheet` / `target_sheet` (index or name, default `0`) |
| `rds`   | `source_query`, `target_query`, `schema_name`, `on_cols`                       | `source_table` / `target_table` (report labeling only); `{{SCHEMA}}` in queries is substituted with `schema_name` |

### Per-Validation Output Options

| Key                         | Description                                                    |
|------------------------------|------------------------------------------------------------------|
| `report_dir`                | Directory the reports for this validation are written to        |
| `report_name`               | Base filename for generated reports                              |
| `save_text`                 | Write the plain-text `datacompy` report                          |
| `save_html`                 | Write the styled HTML report                                     |
| `save_mismatches_csv`       | Write mismatch / source-only / target-only CSVs                  |
| `save_matched_csv`          | Write a CSV of fully matched rows                                 |
| `check_duplicate_on_source` | Check for duplicate `on_cols` keys in the source                 |
| `check_duplicate_on_target` | Check for duplicate `on_cols` keys in the target                 |

### 2. Configure RDS secrets (for `type: rds` validations only)

Create a `.env` file in the project root (git-ignored):

```
pg_connection_string=postgresql://user:password@host:5432
```

`main.py` combines this with each validation's `db_name` to build the full connection URI.

---

## Usage

Run a specific validation by its key from `config/configuration.yaml`:

```bash
python main.py --validation validation-1
# or, with the short flag:
python main.py -v validation-1
```

The console shows a live progress table as the validation runs, followed by a summary table:

```
Source vs Target Comparison - Progress

+----+----------------+--------+-----------+----------------+
|  # | Validation     | Type   | Status    | Duration (s)   |
+====+================+========+===========+================+
|  1 | validation-1   | csv    | MATCH     | 1.24           |
+----+----------------+--------+-----------+----------------+
```

Reports are written under the configured `report_dir` (default: `data_validation/<name>/`):

```
data_validation/validation-1/
├── compare_report.txt
├── compare_report.html
├── compare_report_mismatches.csv
├── compare_report_matched.csv
├── compare_report_only_in_source.csv
├── compare_report_only_in_target.csv
├── compare_report_duplicates_source.csv
└── compare_report_duplicates_target.csv
```

Logs for each run are written to `logs/data_validation_<timestamp>.log`.

---

## Project Structure

```
📦data-validation-framework
 ┣ 📂config
 ┃ ┗ 📜configuration.yaml                           # Declarative validation definitions
 ┣ 📂logs
 ┃ ┣ 📜data_validation_20260829_215714.log
 ┣ 📂utils
 ┃ ┣ 📜compare_tool.py                              # Core comparison, normalization, and reporting engine
 ┃ ┣ 📜dot_env_secrets.py
 ┃ ┗ 📜__init__.py
 ┣ 📜.env                                           # .env loader for RDS credentials
 ┣ 📜.gitignore
 ┣ 📜main.py                                        # CLI entry point
 ┣ 📜README.md
 ┗ 📜requirements.txt
```

---

## How It Works

1. **Load config** — parses YAML and resolves anchors.
2. **Fetch credentials** — pulls the Postgres connection string from `.env` (for `rds` validations).
3. **Read raw data** — CSV and Excel are read as strings via Polars/Openpyxl to prevent type coercion.
4. **Normalize data** — applies automatic fixes (see [Automatic Data Normalization](#automatic-data-normalization)) unless `apply_fix_to_df: false`.
5. **Align columns** — renames target columns to match source names via `field_mapping`.
6. **Compare** — runs `datacompy.Compare()` on common columns.
7. **Check for duplicate join keys** *(optional)* — flags any `on_cols` combination appearing more than once, if `check_duplicate_on_source` / `check_duplicate_on_target` is enabled.
8. **Generate & upload** — reports are created, uploaded to `report_dir`, and cleaned up locally.

---

## Key Config Concepts

| Key | Applies to | Notes |
|---|---|---|
| `type` | all | `csv`, `excel`, or `rds` |
| `on_cols` | all | Join key column(s), referenced using source-side names |
| `field_mapping` | all | `{sourcefield: targetfield}` — only needed when names differ |
| `source_query` / `target_query` | rds | SQL statements; `{{SCHEMA}}` is substituted with `schema_name` |
| `apply_fix_to_df` | all | `true` (default) applies automatic normalization before comparing; `false` compares raw values exactly as read |
| `absolute_tolerance` / `relative_tolerance` | all | Numeric comparison tolerance passed to datacompy; default `0` (exact match) |
| `source_table` / `target_table` | rds | Display-only labels in the HTML report's "Source & Target" section — does not affect the query |
| `ignore_extra_columns` | all | `true` (default) — an extra/missing column doesn't by itself force `MISMATCH`, as long as all *shared* columns match. Set `false` for strict schema-equality checking |
| `save_matched_csv` | all | `false` (default, opt-in) — exports `<report_name>_matched.csv` with every fully-matched row. Off by default since matched rows are typically the majority of data |
| `check_duplicate_on_source` / `check_duplicate_on_target` | all | `false` (default, opt-in) — detects non-unique `on_cols` values on that side, independently controlled |
| `apply_fix_to_src_df_on_duplicate` / `apply_fix_to_tgt_df_on_duplicate` | all | `true` (default) — controls whether duplicate detection runs against normalized or raw data, independent of `apply_fix_to_df` |

---

## Automatic Data Normalization

Before comparison, every validation automatically applies these fixes to both source and target data (unless `apply_fix_to_df: false`):

1. **Whitespace/newline trimming** — strips leading/trailing `\r`, `\n`, and spaces (e.g. `"GmbH\n"` → `"GmbH"`)
2. **NULL vs empty-string unification** — both `NULL` and `""` are treated as the same value
3. **Trailing `.0` stripping** — only when the entire fractional part is zero (e.g. `"9088.0"` → `"9088"`). Values with a genuine fraction (e.g. `"52.525250"`) are left untouched
4. **Leading zero stripping on decimals** — only on the integer part of a value that already has a decimal point (e.g. `"00000000.01"` → `"0.01"`). Pure integers (e.g. `"007"`) are never touched, preserving zero-padded identifiers

These fixes only resolve previously-incomparable-but-actually-equal values — they never introduce new matches between genuinely different values. Set `apply_fix_to_df: false` to disable this and compare raw, unmodified values (useful for reviewing truly raw differences with business stakeholders).

---

## Duplicate Join-Key Detection

A join key (`on_cols`) that isn't actually unique within source or target undermines the reliability of the whole comparison — rows can silently fail to join 1:1, inflating "only in source"/"only in target" counts in ways that look like data-migration issues but are really a join-key design problem.

Enable detection independently per side, under a validation's `output:` block:

```yaml
output:
  check_duplicate_on_source: true
  check_duplicate_on_target: true
```

When enabled, the tool groups each side by `on_cols` and flags any combination appearing more than once, reporting:

- The duplicated key values
- `duplicate_count` — how many times each combination repeats

- **HTML report:** the first 10 duplicate key groups (sorted worst-offender-first) appear under a "Duplicate Records (on join key)" section.
- **CSV output:** the full list is written to `<report_name>_duplicates_source.csv` / `<report_name>_duplicates_target.csv` if any duplicates are found.

By default, duplicate checking uses the same normalized data as the main comparison. Override this independently per side with `apply_fix_to_src_df_on_duplicate` / `apply_fix_to_tgt_df_on_duplicate` (both default `true`) — useful, for example, to check duplicates on normalized data even when `apply_fix_to_df: false` is set for the main raw comparison.

> **Note:** Duplicate detection is diagnostic, not corrective — it reports which join-key values aren't unique but never deduplicates data or alters the comparison itself. A non-unique join key is a design issue worth resolving (e.g. finding a better key, or accepting the fan-out) rather than something this tool silently works around.

---

## Extra Column Handling

By default, this tool treats a data migration as successful if all *shared* columns match perfectly across all rows — even if one side has an extra column the other doesn't (e.g. a system-generated `id` field added during migration).

| Setting | Behavior |
|---|---|
| `ignore_extra_columns: true` (default) | Status is `MATCH` as long as all common columns/rows match. Extra/missing columns are still reported but don't affect pass/fail status |
| `ignore_extra_columns: false` | Status is `MISMATCH` if source and target don't have the exact same column set — even if every shared column matches perfectly |

---

## Output Files

Each run produces, per validation:

| File | Contents |
|---|---|
| `<report_name>.txt` | Full datacompy text report |
| `<report_name>.html` | Styled HTML version with mismatch preview |
| `<report_name>_only_in_source.csv` | Rows present in source but not target |
| `<report_name>_only_in_target.csv` | Rows present in target but not source |
| `<report_name>_mismatches.csv` | Rows that joined successfully but had at least one differing column value |
| `<report_name>_matched.csv` | Rows that joined and matched on every compared column (opt-in — see `save_matched_csv`) |
| `<report_name>_duplicates_source.csv` | Join-key values appearing more than once in source, with a count per key (opt-in — see `check_duplicate_on_source`) |
| `<report_name>_duplicates_target.csv` | Join-key values appearing more than once in target, with a count per key (opt-in — see `check_duplicate_on_target`) |

> Each CSV is only generated if it has data to write — e.g. `_mismatches.csv` is skipped if every joined row matched perfectly, `_matched.csv` is skipped if there are no full-row matches, and duplicate CSVs are skipped if no duplicate join-key values are found. This is expected, not an error.

All files are uploaded to `{report_dir}/`.

---

## Logging

Every run creates a timestamped log file: `./logs/data_validation_YYYYMMDD_HHMMSS.log`

- **Console:** clean progress and summary tables.
- **Log file:** complete detail, including library-level logs.

> Both the console and log file receive the same progress/summary output (routed through the logger, not `print()`), so the full run history — including the `tabulate` tables — is always preserved in the log file for later review.

---

## Troubleshooting

| Symptom | Likely Cause |
|---|---|
| `KeyError: 'on'` | Used unquoted `on:` in YAML. Use `on_cols:` instead |
| High "only in" counts | Join key (`on_cols`) formatting mismatch — check raw string formats |
| `KeyError` in config | Check YAML nesting level or anchor merging |
| `cannot return empty fold because the number of output rows is unknown` | Known bug in datacompy's `PolarsCompare` (0.15.0) when there are 0 mismatches — resolved, since this tool exclusively uses the pandas-based `Compare` engine |
| Very high "only in source"/"only in target" counts | Usually a join-key formatting mismatch (e.g. numeric/whitespace drift). Check if `apply_fix_to_df` is enabled; review the HTML report's "Data Fixes Applied" section |
| HTML shows "No mismatches found" but counts show unmatched rows | Expected — "mismatches" refers only to rows that joined but differ; unmatched rows are reported separately as "only in source/target" |
| Status shows `MISMATCH` but report shows 0 mismatched rows and 0 unequal values | Source/target have a different column set (e.g. target has an extra `id` column). Check "Number of columns in ... but not in ..." in the report. Set `ignore_extra_columns: true` (default) if this is expected |
| `"Any duplicates on match values: Yes"` in the report | The join key (`on_cols`) isn't unique in source or target. Enable `check_duplicate_on_source`/`check_duplicate_on_target` to see exactly which values repeat and how many times |

---

## Design Notes

- **Tolerance:** `absolute_tolerance`/`relative_tolerance` default to `0` (exact match) and are configurable per validation. Since most data is compared as normalized strings by design, tolerance is primarily useful when you explicitly want to allow small numeric rounding differences.
- **Data fixes are conservative by design:** normalization only resolves *formatting* differences (whitespace, `.0` suffixes, null/empty). It never alters genuinely different values. Boolean text/dtype differences (e.g. native `bool` vs text `"true"`/`"false"`) are intentionally left for the SQL query itself to resolve (e.g. via an explicit `::text` cast), not handled in Python.
- **`ignore_extra_columns` defaults to `true`**, prioritizing data-value correctness over strict schema equality — appropriate for migration validation, where the target commonly gains system-generated columns (e.g. `id`) that source never had. Override to `false` per validation if strict schema matching is required.
- **Duplicate detection is a diagnostic, not a fix:** it reports which join-key values aren't unique — it doesn't deduplicate data or alter the comparison. A non-unique join key is a design issue worth resolving rather than something this tool silently works around.