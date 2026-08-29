"""
Source vs Target comparison tool.
Supports CSV/Excel file pairs and Postgres (RDS) table pairs, driven by YAML config.
Uses Pandas (+ RDS), datacompy for the diff engine,
and tabulate for live progress display.

All file I/O (source/target files and generated reports) is now performed
directly against the local filesystem.
Step - `source` / `target` in the YAML config are simply local file paths,
and `output.report_dir` (or the global `output.directory` fallback) is a
local directory that reports/CSV files are written into directly.
"""

import os
import time
from pathlib import Path
import re
from decimal import Decimal
from typing import Optional
import yaml
import polars as pl
import pandas as pd
import datacompy
from tabulate import tabulate
from sqlalchemy import create_engine
import logging

logger = logging.getLogger("data_validator")

FIX_MIN_ROWS_TO_REPORT = 1
FIX_SAMPLE_COUNT = 3

def _classify_fix(raw: Optional[str], canonical: Optional[str]) -> Optional[str]:
    """Labels what kind of change _canonical_string made, compared to the raw value."""
    if raw == canonical:
        return None
    if canonical is None or raw is None:
        return "Null / Empty String Unified"
    if raw.strip() == canonical:
        return "Whitespace / Newline Trimmed"
    return "Numeric Formatting Normalized"

def compute_fix_summary(original_df: pl.DataFrame, normalized_df: pl.DataFrame) -> dict:
    """
    Compares raw vs normalized values column-by-column and builds a summary
    of which fixes fired, how many rows each affected, and a few samples.
    Returns: {column: {fix_category: {"count": int, "samples": [(before, after), ...]}}}
    """
    summary = {}
    for col in original_df.columns:
        if col not in normalized_df.columns:
            continue
        orig_vals = original_df[col].to_list()
        norm_vals = normalized_df[col].to_list()

        col_summary = {}
        for o, n in zip(orig_vals, norm_vals):
            raw_o = _raw_serialize(o)
            category = _classify_fix(raw_o, n)
            if category is None:
                continue
            entry = col_summary.setdefault(category, {"count": 0, "samples": []})
            entry["count"] += 1
            if len(entry["samples"]) < FIX_SAMPLE_COUNT:
                entry["samples"].append((raw_o, n))

        if col_summary:
            summary[col] = col_summary

    return summary

def _canonical_string(val) -> Optional[str]:
    """
    Converts a raw cell value into one canonical string, implementing
    four fixes identified from real source-vs-target data:

      1. Whitespace/newline trimming.
      2. NULL vs empty string unification (both -> None).
      3. Trailing ".0"-only stripping (e.g. "9088.0" -> "9088"), when the
         ENTIRE fractional part is zero.
      4. Leading zero stripping on the INTEGER part of a decimal value
         only (e.g. "00000000.01" -> "0.01"), while the fractional part
         is preserved EXACTLY as written (e.g. "52.525250" stays
         "52.525250" - only its integer part, "52", is checked/stripped,
         and since it has no leading zeros, nothing changes).

         Safe because a value with a decimal point is always a genuine
         number, never a zero-padded identifier (those are always pure
         integers, e.g. "007", which is handled separately below and
         never touched).

    Scientific notation (e.g. "1.3911952E7") is handled by the existing
    whole-number collapse only; leading-zero stripping is not applied
    there, since no such case has been observed in real data.
    """
    if val is None:
        return None

    if isinstance(val, int):
        return str(val)

    if isinstance(val, (float, Decimal)):
        f = float(val)
        if f == int(f):
            return str(int(f))
        return str(val)

    if hasattr(val, "isoformat"):
        return val.isoformat()

    if isinstance(val, str):
        # Fix 1: whitespace/newline trimming
        cleaned = val.strip()

        # Fix 2: empty string -> None
        if cleaned == "":
            return None

        # Fix 3 & 4: plain decimal (no exponent) - collapse ".0"-only,
        # OR strip leading zeros on the integer part while preserving
        # the fractional part exactly.
        plain_decimal = re.fullmatch(r"(-?)(\d*)\.(\d+)", cleaned)
        if plain_decimal:
            sign, int_part, frac_part = plain_decimal.groups()
            try:
                f = float(cleaned)
                if f == int(f):
                    return str(int(f))  # Fix 3: whole number, e.g. "9088.0" -> "9088"
            except ValueError:
                return cleaned
            # Fix 4: genuine fraction - strip leading zeros from integer
            # part only, keep fraction untouched
            stripped_int = int_part.lstrip("0") or "0"
            return f"{sign}{stripped_int}.{frac_part}"

        # Scientific notation - unchanged from before (whole-number
        # collapse only; leading-zero stripping not applied here)
        if re.fullmatch(r"-?\d*\.\d+[eE][+-]?\d+", cleaned) or re.fullmatch(r"-?\d+[eE][+-]?\d+", cleaned):
            try:
                f = float(cleaned)
                if f == int(f):
                    return str(int(f))
            except ValueError:
                pass
            return cleaned

        return cleaned

    return str(val)

def normalize_dataframe_universal(df: pl.DataFrame) -> pl.DataFrame:
    """
    Applies _canonical_string() to every cell of every column, automatically,
    for all validation types (CSV, Excel, RDS). No YAML configuration needed.
    """
    exprs = [
        pl.col(c).map_elements(_canonical_string, return_dtype=pl.Utf8).alias(c)
        for c in df.columns
    ]
    return df.with_columns(exprs)

def _raw_serialize(val) -> Optional[str]:
    """
    Minimal type-to-string conversion with NO formatting fixes applied.
    Used as the 'true raw' baseline for Excel reads and for fix-reporting.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, (float, Decimal)):
        return str(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, str):
        return val
    return str(val)

def get_matched_rows(comp):
    """
    Returns only the rows that joined successfully AND matched on every
    compared column. If ALL columns are join keys (no columns left to
    compare), every successfully-joined row is a full match by
    definition, so the entire intersect is returned.
    """
    intersect = comp.intersect_rows
    match_cols = [c for c in intersect.columns if c.endswith("_match")]
    if not match_cols:
        # No non-join columns were compared - every joined row is a
        # trivial full match (it matched on 100% of columns to join at all)
        return intersect
    all_match_mask = intersect[match_cols].all(axis=1)
    return intersect[all_match_mask]

def find_duplicate_keys(df: pl.DataFrame, on_cols: list) -> tuple:
    """
    Returns (duplicates_df, column_resolution_info).

    duplicates_df: one row per join-key combination appearing more than
    once, with a `duplicate_count` column - sorted worst-offender first.

    column_resolution_info: a dict describing exactly how each configured
    on_cols entry was matched against this dataframe's actual columns
    (case-insensitively, matching datacompy's own cast_column_names_lower
    default) - so it's fully transparent which on_cols entries were used,
    and which (if any) could not be found at all.
    """
    lower_to_actual = {c.lower(): c for c in df.columns}
    resolved = []
    used_for_groupby = []
    missing = []

    for col in on_cols:
        actual = lower_to_actual.get(col.lower())
        resolved.append((col, actual))
        if actual is not None:
            used_for_groupby.append(actual)
        else:
            missing.append(col)

    column_resolution_info = {
        "on_cols": on_cols,
        "df_columns": df.columns,
        "resolved": resolved,
        "missing": missing,
        "used_for_groupby": used_for_groupby,
    }

    if missing:
        logger.warning(
            f"find_duplicate_keys: on_cols entries not found in dataframe "
            f"(case-insensitive match): {missing}"
        )

    if not used_for_groupby:
        return df.head(0), column_resolution_info

    dup_keys = (
        df.group_by(used_for_groupby)
        .agg(pl.len().alias("duplicate_count"))
        .filter(pl.col("duplicate_count") > 1)
        .sort("duplicate_count", descending=True)
    )
    return dup_keys, column_resolution_info

# ============================================================
# Config loading
# ============================================================
def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)  # PyYAML resolves << anchors/aliases natively

# ============================================================
# Readers
# ============================================================
def read_csv_raw(path: str, delimiter: str) -> pl.DataFrame:
    """
    infer_schema_length=0 forces Polars to treat every column as Utf8 (string),
    i.e. no numeric/date auto-inference -> true 'raw' comparison.
    `path` is a local filesystem path.
    """
    return pl.read_csv(
        path,
        separator=delimiter,
        infer_schema_length=0,
        null_values=[""],
    )

def _read_xlsx_openpyxl(path: str, sheet_name=0) -> pl.DataFrame:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[sheet_name]] if isinstance(sheet_name, int) else wb[sheet_name]

    rows = ws.iter_rows(values_only=True)
    header_row = next(rows)
    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(header_row)]

    data = {h: [] for h in headers}
    for row in rows:
        for h, val in zip(headers, row):
            data[h].append(_raw_serialize(val))
    wb.close()
    return pl.DataFrame(data)

def _read_xls_xlrd(path: str, sheet_name=0) -> pl.DataFrame:
    import xlrd
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(sheet_name) if isinstance(sheet_name, int) else book.sheet_by_name(sheet_name)
    headers = [str(sheet.cell_value(0, c)) for c in range(sheet.ncols)]

    data = {h: [] for h in headers}
    for r in range(1, sheet.nrows):
        for c, h in enumerate(headers):
            data[h].append(_raw_serialize(sheet.cell_value(r, c)))
    return pl.DataFrame(data)

def read_excel_raw(path: str, sheet_name=0) -> pl.DataFrame:
    """`path` is a local filesystem path."""
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx_openpyxl(path, sheet_name)
    elif ext == ".xls":
        return _read_xls_xlrd(path, sheet_name)
    raise ValueError(f"Unsupported excel extension: {ext}")

def read_rds(query: str, connection_string: str, schema: str) -> pl.DataFrame:
    """
    Reads from Postgres/RDS via SQLAlchemy + pandas, then converts to Polars.
    """
    query = query.replace("{{SCHEMA}}", schema)
    engine = create_engine(connection_string)
    df_pd = pd.read_sql(query, engine)
    return pl.from_pandas(df_pd)

# ============================================================
# Field mapping helper
# ============================================================
def align_target_columns(target_df: pl.DataFrame, field_mapping: dict) -> pl.DataFrame:
    """
    field_mapping: {source_field: target_field}
    Renames target columns to their source-side names so downstream
    join/compare logic can operate on a single consistent naming scheme.
    """
    if not field_mapping:
        return target_df
    rename_map = {tgt: src for src, tgt in field_mapping.items()}
    existing = {k: v for k, v in rename_map.items() if k in target_df.columns}
    return target_df.rename(existing) if existing else target_df

# ============================================================
# Output settings resolution
# ============================================================
def resolve_output_settings(name: str, val_cfg: dict, global_output_cfg: dict) -> dict:
    """
    Merge per-validation `output:` block with global fallback defaults.
    If the validation defines its own output block, it takes priority field-by-field.
    `report_dir` is a local directory path that reports/CSVs get written into.
    """
    global_dir = global_output_cfg.get("directory", "data_validation")
    global_save_mismatches = global_output_cfg.get("save_mismatches", True)

    val_output = val_cfg.get("output", {})

    return {
        "report_dir": val_output.get("report_dir", global_dir),
        "report_name": val_output.get("report_name", name),
        "save_text": val_output.get("save_text", True),
        "save_html": val_output.get("save_html", False),
        "save_mismatches_csv": val_output.get("save_mismatches_csv", global_save_mismatches),
        "save_matched_csv": val_output.get("save_matched_csv", False),
        "check_duplicate_on_source": val_output.get("check_duplicate_on_source", False),
        "check_duplicate_on_target": val_output.get("check_duplicate_on_target", False),
    }

# ============================================================
# HTML report generation
# ============================================================
def build_html_report(
    name: str,
    text_report: str,
    summary: dict,
    mismatch_df,
    matched_row_count: int,
    vtype: str,
    source_target_info: dict,
    report_dir: str,
    base_name: str,
    out_settings: dict,
    apply_fix: bool,
    source_fix_summary: dict,
    target_fix_summary: dict,
    source_duplicates_df,
    target_duplicates_df,
    apply_fix_src_dup: bool,
    apply_fix_tgt_dup: bool,
    source_dup_col_info: Optional[dict],
    target_dup_col_info: Optional[dict],
) -> str:
    from datetime import datetime

    # Determine actual row count FIRST - .to_html() always produces a non-empty
    # string (headers + empty body) even for a 0-row DataFrame, so checking
    # len(mismatch_html) > 0 later would always be True. Check row count instead.
    mismatch_row_count = mismatch_df.height if isinstance(mismatch_df, pl.DataFrame) else len(mismatch_df)

    if mismatch_row_count > 0:
        if isinstance(mismatch_df, pl.DataFrame):
            mismatch_html = mismatch_df.to_pandas().head(500).to_html(
                index=False, border=0, classes="mismatch-table", escape=True
            )
        else:
            mismatch_html = mismatch_df.head(500).to_html(
                index=False, border=0, classes="mismatch-table", escape=True
            )
    else:
        mismatch_html = None

    is_match = summary["status"] == "MATCH"
    status_bg = "#16a34a" if is_match else "#dc2626"
    status_text = "MATCH" if is_match else "MISMATCH"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def stat_card(label, value, warn_if_nonzero=False):
        """Renders a single KPI card. If warn_if_nonzero, colors the value red when >0, green when 0."""
        try:
            numeric_value = int(value)
        except (ValueError, TypeError):
            numeric_value = None

        value_color = "#0f172a"
        if warn_if_nonzero and numeric_value is not None:
            value_color = "#dc2626" if numeric_value > 0 else "#16a34a"

        return f"""
        <div class="stat-card">
          <div class="stat-value" style="color:{value_color};">{value}</div>
          <div class="stat-label">{label}</div>
        </div>
        """

    cards_html = "".join([
        stat_card("Source Rows", summary["source_rows"]),
        stat_card("Target Rows", summary["target_rows"]),
        stat_card("Matched Rows", summary["matched_rows"]),
        stat_card("Mismatched Rows", summary["mismatched_rows"], warn_if_nonzero=True),
        stat_card("Only in Source", summary["only_in_source"], warn_if_nonzero=True),
        stat_card("Only in Target", summary["only_in_target"], warn_if_nonzero=True),
        stat_card("Duration (s)", summary["duration_sec"]),
    ])

    # ------------------------------------------------------------------
    # Source & Target card - content differs by validation type
    # ------------------------------------------------------------------
    def path_chip(label, value):
        return f"""
        <div class="path-block">
          <div class="path-label">{label}</div>
          <div class="path-chip">{value}</div>
        </div>
        """

    if vtype in ("csv", "excel"):
        source_path = os.path.abspath(source_target_info["source"])
        target_path = os.path.abspath(source_target_info["target"])
        source_col = path_chip("SOURCE FILE", source_path)
        target_col = path_chip("TARGET FILE", target_path)
    elif vtype == "rds":
        db_name = source_target_info.get("db_name", "N/A")
        schema_name = source_target_info.get("schema_name", "N/A")
        source_table = source_target_info.get("source_table", "N/A")
        target_table = source_target_info.get("target_table", "N/A")
        source_col = (
            path_chip("SOURCE DATABASE", db_name) +
            path_chip("SOURCE TABLE", f"{schema_name}.{source_table}")
        )
        target_col = (
            path_chip("TARGET DATABASE", db_name) +
            path_chip("TARGET TABLE", f"{schema_name}.{target_table}")
        )
    else:
        source_col = path_chip("SOURCE", "N/A")
        target_col = path_chip("TARGET", "N/A")

    # ------------------------------------------------------------------
    # Generated Output Files card - only lists files that were ACTUALLY
    # created: respects both the save_* flag AND whether there was data
    # to write (matches the exact logic used in run_validation()).
    # ------------------------------------------------------------------
    output_files = []
    report_base_path = os.path.abspath(report_dir)

    if out_settings.get("save_text"):
        output_files.append(os.path.join(report_base_path, f"{base_name}.txt"))

    if out_settings.get("save_html"):
        output_files.append(os.path.join(report_base_path, f"{base_name}.html"))

    if out_settings.get("save_mismatches_csv"):
        if mismatch_row_count > 0:
            output_files.append(os.path.join(report_base_path, f"{base_name}_mismatches.csv"))
        if summary["only_in_source"] > 0:
            output_files.append(os.path.join(report_base_path, f"{base_name}_only_in_source.csv"))
        if summary["only_in_target"] > 0:
            output_files.append(os.path.join(report_base_path, f"{base_name}_only_in_target.csv"))

    if out_settings.get("save_matched_csv") and matched_row_count > 0:
        output_files.append(os.path.join(report_base_path, f"{base_name}_matched.csv"))

    check_dup_source = out_settings.get("check_duplicate_on_source", False)
    check_dup_target = out_settings.get("check_duplicate_on_target", False)

    if check_dup_source and source_duplicates_df is not None and source_duplicates_df.height > 0:
        output_files.append(os.path.join(report_base_path, f"{base_name}_duplicates_source.csv"))

    if check_dup_target and target_duplicates_df is not None and target_duplicates_df.height > 0:
        output_files.append(os.path.join(report_base_path, f"{base_name}_duplicates_target.csv"))

    output_files_html = "".join([
        f'<div class="file-item">{f}</div>' for f in output_files
    ]) or '<div class="file-item file-item-empty">No output files generated</div>'

    def _visualize_whitespace(s: Optional[str]) -> str:
        """Makes invisible whitespace characters visible in HTML output."""
        if s is None:
            return "&empty;"  # visible symbol for None/null
        s = s.replace("\r\n", "&crarr;")   # CRLF -> visible symbol
        s = s.replace("\n", "&crarr;")     # LF -> visible symbol
        s = s.replace("\r", "&crarr;")     # CR -> visible symbol
        s = s.replace("\t", "&rarr;|")     # tab -> visible symbol
        # Wrap in a span that highlights leading/trailing spaces
        leading = len(s) - len(s.lstrip(" "))
        trailing = len(s) - len(s.rstrip(" "))
        if leading or trailing:
            core = s.strip(" ")
            return (
                ('<span class="ws-marker">&middot;</span>' * leading) +
                core +
                ('<span class="ws-marker">&middot;</span>' * trailing)
            )
        return s

    def render_fix_table(fix_summary: dict, side_label: str) -> str:
        rows = []
        for col, categories in fix_summary.items():
            for category, data in categories.items():
                if data["count"] < FIX_MIN_ROWS_TO_REPORT:
                    continue
                samples_html = "<br>".join(
                    f'"{_visualize_whitespace(b)}" &rarr; "{_visualize_whitespace(a)}"'
                    for b, a in data["samples"]
                )
                rows.append(f"""
                <tr>
                  <td>{side_label}</td>
                  <td>{col}</td>
                  <td>{category}</td>
                  <td>{data['count']}</td>
                  <td class="fix-samples">{samples_html}</td>
                </tr>
                """)
        return "".join(rows)

    if not apply_fix:
        fixes_section = '<div class="no-mismatch" style="color:#0f172a;background:#f1f5f9;border-color:#e2e8f0;">Normalization disabled (apply_fix_to_df: false) &mdash; raw values compared exactly as stored.</div>'
    else:
        fix_rows_html = render_fix_table(source_fix_summary, "Source") + render_fix_table(target_fix_summary, "Target")
        if fix_rows_html:
            fixes_section = f"""
            <div class="table-scroll">
              <table class="mismatch-table">
                <thead>
                  <tr><th>Side</th><th>Field</th><th>Fix Type</th><th>Rows Affected</th><th>Example (before &rarr; after)</th></tr>
                </thead>
                <tbody>{fix_rows_html}</tbody>
              </table>
            </div>
            """
        else:
            fixes_section = '<div class="no-mismatch">No fields had 100+ rows affected by normalization.</div>'

    def render_duplicates_block(dup_df, side_label, csv_path):
        if dup_df is None or dup_df.height == 0:
            return f'<div class="no-mismatch">No duplicate {side_label} records found on join key.</div>'
        total = dup_df.height
        preview_html = dup_df.head(10).to_pandas().to_html(
            index=False, border=0, classes="mismatch-table", escape=True
        )
        note = ""
        if total > 10:
            note = f'<p style="margin-top:10px;font-size:12.5px;color:#64748b;">Showing 10 of {total} duplicate rows. Full list: <span class="path-chip" style="display:inline-block;">{csv_path}</span></p>'
        return f'<div class="table-scroll">{preview_html}</div>{note}'

    def fix_badge(applied: bool) -> str:
        if applied:
            return '<span class="fix-badge fix-badge-on">Fix Applied</span>'
        return '<span class="fix-badge fix-badge-off">Raw Data</span>'

    def render_dup_column_warning(col_info: Optional[dict], side_label: str) -> str:
        """
        Renders a warning box explaining exactly which on_cols entries
        could not be matched to an actual column in this dataframe, and
        which columns WERE used for the duplicate grouping - so it's
        never ambiguous why a duplicate count looks unexpected.
        Renders nothing if every on_cols entry resolved successfully.
        """
        if col_info is None or not col_info["missing"]:
            return ""
        resolved_rows = "".join(
            f"<tr><td>{on_col}</td><td>{actual if actual else '<em>Not Found</em>'}</td></tr>"
            for on_col, actual in col_info["resolved"]
        )
        missing_list = ", ".join(f"<code>{m}</code>" for m in col_info["missing"])
        return f"""
        <div class="dup-warning">
          <strong>&#9888; Column Resolution Warning ({side_label}):</strong>
          The following configured <code>on_cols</code> entries could not be matched
          to any column in the {side_label} data (checked case-insensitively) and were
          <strong>excluded</strong> from duplicate detection: {missing_list}.
          <details style="margin-top:8px;">
            <summary style="cursor:pointer;color:#92400e;">Show full column resolution</summary>
            <table class="mismatch-table" style="margin-top:8px;">
              <thead><tr><th>Configured (on_cols)</th><th>Matched Dataframe Column</th></tr></thead>
              <tbody>{resolved_rows}</tbody>
            </table>
          </details>
        </div>
        """

    duplicates_blocks = []

    if check_dup_source:
        src_count = source_duplicates_df.height if source_duplicates_df is not None else 0
        src_csv_path = os.path.join(report_base_path, f"{base_name}_duplicates_source.csv")
        duplicates_blocks.append(f"""
        <div class="card" style="margin-bottom:14px;">
          <div class="path-label" style="margin-bottom:10px;">
            SOURCE DUPLICATES ({src_count} rows) {fix_badge(apply_fix_src_dup)}
          </div>
          {render_dup_column_warning(source_dup_col_info, "source")}
          {render_duplicates_block(source_duplicates_df, "source", src_csv_path)}
        </div>
        """)

    if check_dup_target:
        tgt_count = target_duplicates_df.height if target_duplicates_df is not None else 0
        tgt_csv_path = os.path.join(report_base_path, f"{base_name}_duplicates_target.csv")
        duplicates_blocks.append(f"""
        <div class="card">
          <div class="path-label" style="margin-bottom:10px;">
            TARGET DUPLICATES ({tgt_count} rows) {fix_badge(apply_fix_tgt_dup)}
          </div>
          {render_dup_column_warning(target_dup_col_info, "target")}
          {render_duplicates_block(target_duplicates_df, "target", tgt_csv_path)}
        </div>
        """)

    show_duplicates_section = check_dup_source or check_dup_target
    duplicates_html_block = "".join(duplicates_blocks)

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Data Validator Report - {name}</title>
<style>
  * {{ box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0;
    padding: 0;
    background-color: #f8fafc;
    color: #0f172a;
  }}

  .header {{
    background-color: #0f172a;
    color: #f8fafc;
    padding: 28px 40px;
  }}

  .header .program {{
    font-size: 13px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #93c5fd;
    font-weight: 600;
    margin-bottom: 4px;
  }}

  .header h1 {{
    font-size: 24px;
    margin: 0 0 6px 0;
    font-weight: 700;
  }}

  .header .meta {{
    font-size: 13px;
    color: #cbd5e1;
  }}

  .status-pill {{
    display: inline-block;
    padding: 6px 16px;
    border-radius: 999px;
    background-color: {status_bg};
    color: #ffffff;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 0.5px;
    margin-top: 12px;
  }}

  .container {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 32px 40px 60px 40px;
  }}

  .section-title {{
    font-size: 15px;
    font-weight: 700;
    color: #334155;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 36px 0 14px 0;
  }}

  .stats-grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
  }}

  .stat-card {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px 22px;
    min-width: 140px;
    flex: 1 1 140px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}

  .stat-value {{
    font-size: 26px;
    font-weight: 700;
    line-height: 1.2;
  }}

  .stat-label {{
    font-size: 12px;
    color: #64748b;
    margin-top: 4px;
    font-weight: 500;
  }}

  .card {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}

  .source-target-grid {{
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }}

  .source-target-col {{
    flex: 1 1 300px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}

  .path-block {{
    margin-bottom: 14px;
  }}

  .path-block:last-child {{
    margin-bottom: 0;
  }}

  .path-label {{
    font-size: 11px;
    font-weight: 700;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }}

  .path-chip {{
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 12.5px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 8px 12px;
    color: #1e293b;
    word-break: break-all;
    line-height: 1.5;
  }}

  .table-scroll {{
    overflow-x: auto;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
  }}

  table.mismatch-table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 12.5px;
    white-space: nowrap;
  }}

  table.mismatch-table thead th {{
    background-color: #f1f5f9;
    color: #334155;
    text-align: left;
    padding: 10px 14px;
    border-bottom: 2px solid #e2e8f0;
    position: sticky;
    top: 0;
  }}

  table.mismatch-table tbody td {{
    padding: 8px 14px;
    border-bottom: 1px solid #f1f5f9;
    color: #1e293b;
  }}

  table.mismatch-table tbody tr:nth-child(even) {{
    background-color: #f8fafc;
  }}

  table.mismatch-table tbody tr:hover {{
    background-color: #eff6ff;
  }}

  .no-mismatch {{
    padding: 24px;
    text-align: center;
    color: #16a34a;
    font-weight: 600;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-radius: 8px;
  }}

  .files-list {{
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}

  .file-item {{
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 12.5px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 10px 14px;
    color: #1e293b;
    word-break: break-all;
  }}

  .file-item-empty {{
    color: #94a3b8;
    font-style: italic;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}

  .fix-samples {{
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 11.5px;
    color: #475569;
    line-height: 1.6;
  }}

  .ws-marker {{
    color: #f97316;
    font-weight: 700;
  }}

  .fix-badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.3px;
    text-transform: uppercase;
    margin-left: 8px;
    vertical-align: middle;
  }}

  .fix-badge-on {{
    background-color: #dbeafe;
    color: #1d4ed8;
  }}

  .fix-badge-off {{
    background-color: #f1f5f9;
    color: #64748b;
  }}

  .dup-warning {{
    background: #fffbeb;
    border: 1px solid #fde68a;
    color: #92400e;
    border-radius: 8px;
    padding: 14px 16px;
    font-size: 12.5px;
    margin-bottom: 14px;
    line-height: 1.6;
  }}

  .dup-warning code {{
    background: #fef3c7;
    padding: 1px 5px;
    border-radius: 4px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }}

  pre.raw-report {{
    background: #0f172a;
    color: #e2e8f0;
    padding: 20px;
    border-radius: 10px;
    overflow-x: auto;
    font-size: 12px;
    line-height: 1.6;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }}

  .footer {{
    text-align: center;
    color: #94a3b8;
    font-size: 12px;
    margin-top: 40px;
  }}
</style>
</head>
<body>

  <div class="header">
    <div class="program">Data Validator</div>
    <h1>{name}</h1>
    <div class="meta">Generated on {generated_at}</div>
    <div class="status-pill">{status_text}</div>
  </div>

  <div class="container">

    <div class="section-title">Source &amp; Target</div>
    <div class="source-target-grid">
      <div class="source-target-col">{source_col}</div>
      <div class="source-target-col">{target_col}</div>
    </div>

    <div class="section-title">Summary</div>
    <div class="stats-grid">
      {cards_html}
    </div>

    <div class="section-title">Mismatch Preview (first 500 rows)</div>
    <div class="card">
      {f'<div class="table-scroll">{mismatch_html}</div>' if mismatch_html else '<div class="no-mismatch">No mismatches found</div>'}
    </div>

    <div class="section-title">Data Fixes Applied</div>
    <div class="card">
      {fixes_section}
    </div>

    {"" if not show_duplicates_section else f'<div class="section-title">Duplicate Records (on join key)</div>{duplicates_html_block}'}

    <div class="section-title">Full DataComPy Report</div>
    <pre class="raw-report">{text_report}</pre>

    <div class="section-title">Generated Output Files</div>
    <div class="card">
      <div class="files-list">
        {output_files_html}
      </div>
    </div>

    <div class="footer">Data Validator &middot; Automated Report</div>

  </div>

</body>
</html>
"""

# ============================================================
# Core comparison
# ============================================================
def write_df(df, path: Path):
    """Writes a dataframe (polars or pandas) straight to a local CSV path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(df, pl.DataFrame):
        df.write_csv(path)
    else:
        df.to_csv(path, index=False)

def run_comparison(source_df, target_df, on, name, abs_tol, rel_tol, ignore_extra_cols=True):
    """
    Runs datacompy comparison using the pandas-based Compare engine.
    """
    comp = datacompy.Compare(
        df1=source_df.to_pandas(),
        df2=target_df.to_pandas(),
        join_columns=on,
        abs_tol=abs_tol, #(float, optional) absolute tolerance
        rel_tol=rel_tol, #(float, optional) relative tolerance
        df1_name="source",
        df2_name="target"
    )

    return {
        "comp": comp,
        "is_match": comp.matches(ignore_extra_columns=ignore_extra_cols),
        "mismatch_df": comp.all_mismatch(),
        "matched_df": get_matched_rows(comp),
        "only_source": comp.df1_unq_rows,
        "only_target": comp.df2_unq_rows,
        "intersect_count": len(comp.intersect_rows),
        "text_report": comp.report(),
    }

def run_validation(name: str, cfg: dict, global_output_cfg: dict, db_uri=None) -> dict:
    """
    Expected cfg keys (local-filesystem version):
      type: "csv" | "excel" | "rds"
      on_cols: [...]
      absolute_tolerance, relative_tolerance
      ignore_extra_columns (optional, default True)
      field_mapping (optional)
      apply_fix_to_df / apply_fix_to_src_df_on_duplicate / apply_fix_to_tgt_df_on_duplicate (optional)
      output: {...} (optional, merged with global_output_cfg)

      For type == "csv" / "excel":
        source, target: local file paths
        sep (csv only, default ",")
        source_sheet, target_sheet (excel only, default 0)

      For type == "rds":
        source_query, target_query, schema_name, db_name (optional, for reporting)
    """
    start = time.time()
    vtype = cfg["type"]
    on = cfg["on_cols"]
    abs_tol, rel_tol = cfg["absolute_tolerance"], cfg["relative_tolerance"]
    ignore_extra_cols = cfg.get("ignore_extra_columns", True)
    mapping = cfg.get("field_mapping", {})
    out_settings = resolve_output_settings(name, cfg, global_output_cfg)
    report_dir = out_settings["report_dir"]
    # Ensure the local output directory exists
    os.makedirs(report_dir, exist_ok=True)

    # ---- Load data directly from local paths ----
    if vtype == "csv":
        source_df = read_csv_raw(cfg["source"], cfg.get("sep", ","))
        target_df = read_csv_raw(cfg["target"], cfg.get("sep", ","))
    elif vtype == "excel":
        source_df = read_excel_raw(cfg["source"], cfg.get("source_sheet", 0))
        target_df = read_excel_raw(cfg["target"], cfg.get("target_sheet", 0))
    elif vtype == "rds":
        source_df = read_rds(cfg["source_query"], db_uri, cfg["schema_name"])
        target_df = read_rds(cfg["target_query"], db_uri, cfg["schema_name"])
    else:
        raise ValueError(f"Unknown type '{vtype}' for validation '{name}'")

    target_df = align_target_columns(target_df, mapping)

    apply_fix = cfg.get("apply_fix_to_df", True)
    apply_fix_src_dup = cfg.get("apply_fix_to_src_df_on_duplicate", True)
    apply_fix_tgt_dup = cfg.get("apply_fix_to_tgt_df_on_duplicate", True)

    source_raw_df = source_df
    target_raw_df = target_df

    source_fix_summary, target_fix_summary = {}, {}

    if apply_fix:
        source_original = source_df.clone()
        target_original = target_df.clone()

        source_df = normalize_dataframe_universal(source_df)
        target_df = normalize_dataframe_universal(target_df)

        source_fix_summary = compute_fix_summary(source_original, source_df)
        target_fix_summary = compute_fix_summary(target_original, target_df)
    # else: apply_fix is False - dataframes stay exactly as read (raw comparison)

    # ---- Duplicate detection (independent of the main comparison/fix flag) ----
    check_dup_source = out_settings.get("check_duplicate_on_source", False)
    check_dup_target = out_settings.get("check_duplicate_on_target", False)

    source_duplicates_df = pl.DataFrame()
    target_duplicates_df = pl.DataFrame()
    source_dup_col_info = None
    target_dup_col_info = None

    if check_dup_source:
        if apply_fix_src_dup:
            src_for_dup = source_df if apply_fix else normalize_dataframe_universal(source_raw_df)
        else:
            src_for_dup = source_raw_df
        source_duplicates_df, source_dup_col_info = find_duplicate_keys(src_for_dup, on)

    if check_dup_target:
        if apply_fix_tgt_dup:
            tgt_for_dup = target_df if apply_fix else normalize_dataframe_universal(target_raw_df)
        else:
            tgt_for_dup = target_raw_df
        target_duplicates_df, target_dup_col_info = find_duplicate_keys(tgt_for_dup, on)

    # ---- Run datacompy ----
    result = run_comparison(source_df, target_df, on, name, abs_tol, rel_tol, ignore_extra_cols)
    comp            = result["comp"]
    is_match        = result["is_match"]
    mismatch_df     = result["mismatch_df"]
    matched_df      = result["matched_df"]
    only_source     = result["only_source"]
    only_target     = result["only_target"]
    intersect_count = result["intersect_count"]
    text_report     = result["text_report"]

    mismatch_count = len(mismatch_df)
    duration = round(time.time() - start, 2)

    summary = {
        "name": name,
        "status": "MATCH" if is_match else "MISMATCH",
        "source_rows": source_df.height,
        "target_rows": target_df.height,
        "matched_rows": intersect_count - mismatch_count,
        "mismatched_rows": mismatch_count,
        "only_in_source": len(only_source),
        "only_in_target": len(only_target),
        "duration_sec": duration,
        "error": None,
    }

    # ---- Write outputs directly to the local report_dir ----
    base_name = out_settings["report_name"]
    report_dir_path = Path(report_dir)
    report_dir_path.mkdir(parents=True, exist_ok=True)

    if out_settings["save_text"]:
        txt_report_path = report_dir_path / f"{base_name}.txt"
        with open(txt_report_path, "w") as f:
            f.write(text_report)

    if out_settings["save_html"]:
        if vtype in ("csv", "excel"):
            source_target_info = {"source": cfg["source"], "target": cfg["target"]}
        elif vtype == "rds":
            source_target_info = {
                "db_name": cfg.get("db_name"),
                "schema_name": cfg.get("schema_name"),
                "source_table": cfg.get("source_table", "N/A"),
                "target_table": cfg.get("target_table", "N/A"),
            }
        else:
            source_target_info = {}

        html_report = build_html_report(
            name, text_report, summary, mismatch_df,
            len(matched_df),
            vtype, source_target_info,
            report_dir, base_name, out_settings,
            apply_fix, source_fix_summary, target_fix_summary,
            source_duplicates_df, target_duplicates_df,
            apply_fix_src_dup, apply_fix_tgt_dup,
            source_dup_col_info, target_dup_col_info,
        )

        html_report_path = report_dir_path / f"{base_name}.html"
        with open(html_report_path, "w") as f:
            f.write(html_report)

    if out_settings["save_mismatches_csv"]:
        if len(mismatch_df) > 0:
            write_df(mismatch_df, report_dir_path / f"{base_name}_mismatches.csv")
        if len(only_source) > 0:
            write_df(only_source, report_dir_path / f"{base_name}_only_in_source.csv")
        if len(only_target) > 0:
            write_df(only_target, report_dir_path / f"{base_name}_only_in_target.csv")

    if out_settings.get("save_matched_csv") and len(matched_df) > 0:
        write_df(matched_df, report_dir_path / f"{base_name}_matched.csv")

    if check_dup_source and source_duplicates_df.height > 0:
        write_df(source_duplicates_df, report_dir_path / f"{base_name}_duplicates_source.csv")

    if check_dup_target and target_duplicates_df.height > 0:
        write_df(target_duplicates_df, report_dir_path / f"{base_name}_duplicates_target.csv")

    summary["report_dir"] = os.path.abspath(report_dir)
    return summary

# ============================================================
# Progress display (tabulate)
# ============================================================
def render_progress(statuses: list, clear=False):
    if clear:
        os.system("cls" if os.name == "nt" else "clear")
    headers = ["#", "Validation", "Type", "Status", "Duration (s)"]
    rows = [
        [i + 1, s["name"], s.get("type", ""), s["state"], s.get("duration_sec", "-")]
        for i, s in enumerate(statuses)
    ]
    table_str = tabulate(rows, headers=headers, tablefmt="grid")
    logger.info("Source vs Target Comparison - Progress\n\n" + table_str + "\n")

def render_summary(results: list):
    headers = [
        "Validation", "Status", "Src Rows", "Tgt Rows",
        "Matched", "Mismatched", "Only Src", "Only Tgt", "Time (s)"
    ]
    rows = []
    for r in results:
        if r.get("error"):
            rows.append([r["name"], "ERROR", "-", "-", "-", "-", "-", "-", r["duration_sec"]])
        else:
            rows.append([
                r["name"], r["status"], r["source_rows"], r["target_rows"],
                r["matched_rows"], r["mismatched_rows"],
                r["only_in_source"], r["only_in_target"], r["duration_sec"],
            ])
    table_str = tabulate(rows, headers=headers, tablefmt="grid")
    logger.info("\nFinal Summary\n\n" + table_str)