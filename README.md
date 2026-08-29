# Data Validation Framework

A configuration-driven data validation tool for comparing **source** vs **target** datasets across **CSV**, **Excel**, and **PostgreSQL/RDS** — built for reliable data migration validation.

Everything (what to compare, how to normalize it, and what to report) is defined declaratively in a single YAML file. Point it at a validation key and it loads both sides, normalizes and compares them with [datacompy](https://github.com/capitalone/datacompy), detects duplicate keys, and writes back detailed TXT, HTML, and CSV reports — no code changes required to add a new comparison.

## Features

- **Multiple source types** — compare CSV ↔ CSV, Excel ↔ Excel, or Postgres/RDS table ↔ table, all through the same engine.
- **Config-driven validations** — define any number of named validations (`validation-1`, `validation-2`, ...) in `config/configuration.yaml`, each with its own source, target, and output settings. YAML anchors (`<<: *defaults`) let you share common settings and override only what differs.
- **Raw data preservation** — data is read as raw strings (no silent numeric/date type coercion) so the "before" state is always available for reporting, while a normalized copy is used for the actual comparison.
- **Automatic data normalization ("fixes")** — before comparing, values are canonicalized to avoid noisy false-positive mismatches:
  - Whitespace/newline trimming
  - NULL vs. empty-string unification
  - Trailing `.0` collapsed on whole numbers (e.g. `9088.0` → `9088`)
  - Leading zeros stripped from the integer part of decimals, fractional part left untouched (e.g. `00000000.01` → `0.01`)

  Every fix applied is tallied per column with before/after samples, so you can see exactly what was normalized.
- **Column mapping** — use `field_mapping` to align differently-named columns between source and target before comparing.
- **Configurable tolerances** — set `absolute_tolerance` / `relative_tolerance` to allow acceptable numeric drift instead of flagging it as a mismatch.
- **Duplicate key detection** — optionally check the source and/or target for duplicate join keys (`on_cols`) independent of the main comparison, with a per-column resolution report (case-insensitive matching).
- **Detailed reporting** — for each validation, generate:
  - A plain-text `datacompy` report (`.txt`)
  - A styled, self-contained HTML report (`.html`) with summary KPI cards, mismatch tables, fix summaries, and duplicate-key tables
  - CSV exports for mismatches, source-only rows, target-only rows, matched rows, and duplicate keys
- **Live progress + summary tables** — a `tabulate`-rendered progress table updates as each validation runs, followed by a final summary table (rows compared, matched, mismatched, duration, etc.).
- **Secrets via `.env`** — RDS connection strings are loaded from a local `.env` file and never stored in the YAML config.

## How it works

1. `main.py` loads `config/configuration.yaml` and (optionally) filters down to a single validation via `--validation`.
2. For each validation, `utils/compare_tool.py`:
   - Reads the source and target (CSV, Excel, or a SQL query against Postgres/RDS) as raw Polars DataFrames.
   - Applies `field_mapping` to align target column names to the source side.
   - Normalizes both DataFrames (unless `apply_fix_to_df: false`) and records what changed.
   - Optionally checks for duplicate join keys on either side.
   - Runs the comparison with `datacompy.Compare`, using the configured join columns and tolerances.
   - Writes the requested reports to `report_dir`.
3. Progress and a final summary are rendered to the console/log as the run proceeds.

## Requirements

- Python 3.9+
- Key dependencies (see `requirements.txt`, add as needed for your environment):
  - `polars`
  - `pandas`
  - `datacompy`
  - `sqlalchemy` (+ a Postgres driver, e.g. `psycopg2-binary`, for `rds` validations)
  - `pyyaml`
  - `tabulate`
  - `openpyxl` (`.xlsx`/`.xlsm`) and/or `xlrd` (`.xls`)
  - `python-dotenv`

Install with:

```bash
git clone https://github.com/giteshdkolte/data-validation-framework.git
cd data-validation-framework
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Setup

### 1. Configure validations

Edit `config/configuration.yaml`. Common settings live under `default_settings` and are pulled into each validation with the YAML anchor `<<: *defaults`; override only what's different per validation.

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

Supported validation `type` values:

| Type    | Required keys                                                                 |
|---------|--------------------------------------------------------------------------------|
| `csv`   | `source`, `target`, `on_cols` (optional: `sep`, default `,`)                   |
| `excel` | `source`, `target`, `on_cols` (optional: `source_sheet` / `target_sheet`, index or name, default `0`) |
| `rds`   | `source_query`, `target_query`, `schema_name`, `on_cols` (optional: `source_table` / `target_table` for report labeling; `{{SCHEMA}}` in queries is replaced with `schema_name`) |

Per-validation `output` options:

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

Create a `.env` file in the project root (this is git-ignored):

```
pg_connection_string=postgresql://user:password@host:5432
```

`main.py` combines this with each validation's `db_name` to build the full connection URI.

## Usage

Run a specific validation by its key from `config/configuration.yaml`:

```bash
python main.py --validation validation-1
```

or with the short flag:

```bash
python main.py -v validation-1
```

The console shows a live progress table as the validation runs, followed by a summary table, e.g.:

```
Source vs Target Comparison - Progress

+----+----------------+--------+-----------+----------------+
|  # | Validation     | Type   | Status    | Duration (s)   |
+====+================+========+===========+================+
|  1 | validation-1   | csv    | MATCH     | 1.24           |
+----+----------------+--------+-----------+----------------+
```

Reports are written under the configured `report_dir` (default: `data_validation/<name>/`), e.g.:

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

## How It Works

At a high level, the framework follows this flow:

```text
                 ┌─────────────────────┐
                 │ configuration.yaml  │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │ Select Validation   │
                 │ --validation KEY    │
                 └──────────┬──────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
          CSV Files      Excel Files   PostgreSQL/RDS
              │             │             │
              └─────────────┼─────────────┘
                            ▼
                 ┌─────────────────────┐
                 │ Raw Data Loading    │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │ Column Alignment    │
                 │ + Field Mapping     │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │ Data Normalization  │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │ Duplicate Detection │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │ Source vs Target    │
                 │ Comparison           │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │ TXT / HTML / CSV    │
                 │ Validation Reports  │
                 └─────────────────────┘
```

## Project structure

```
.
├── main.py                     # CLI entry point
├── config/
│   └── configuration.yaml      # Declarative validation definitions
├── utils/
│   ├── compare_tool.py         # Core comparison, normalization, and reporting engine
│   └── dot_env_secrets.py      # .env loader for RDS credentials
├── requirements.txt
└── .gitignore
```

## Notes

- `.env`, virtual environments, `__pycache__/`, and `logs/` are excluded via `.gitignore` — never commit real credentials.
- Data files referenced in the sample config (`../data_files/...`) are not included in this repository; point `source` / `target` at your own CSV, Excel, or database data.
