#!/usr/bin/env python3
"""
Clean borough CSV files: apply per-borough header-modification rules.

uses the cleaning logic tested in the script: check_AMOD_headers_X4.py and imports the same logic to actually perform the cleaning

Two modes:
  --mode amod   (default) Write AMOD_<filename> beside each modified file.
                Only modified files produce output. Unmodified files left alone.
  --mode clean  Mirror the directory tree to <output_root>/london_boroughs/<Borough>/<file>.
                Every file is copied; modified files have the rule applied,
                unmodified files copied as-is.

Errors (unreadable file, matched no rule for a borough that has rules,
modification raised an exception, copy failed) are written to
clean_unmatched.log. A successful run produces an empty log.

Usage:
    python clean_borough_files_X8.py                          # AMOD_ test mode
    python clean_borough_files.py --mode clean             # mirror to clean/
    python clean_borough_files.py --source raw/london_boroughs --output clean
    python clean_borough_files_X14.py --borough Bromley         # single borough
    python clean_borough_files.py --borough Croydon Ealing  # multiple boroughs
    python clean_borough_files.py --borough Croydon --files april_2022  # combined

    python clean_borough_files_X18.py --borough Bromley Redbridge

    then

    python clean_borough_files_X21.py --mode clean  --borough Redbridge

"""
import argparse
import csv
import io
import os
import re
import shutil
import sys
from datetime import datetime

# Import header-detection logic from the companion script
from check_AMOD_header_X4 import (
    read_detected_header_row,
    _resolve_format,
    _detect_file_format,
    _try_load_xlsx,
    _iter_xlsx_rows,
    _fix_mojibake,
)

DEFAULT_SOURCE = "raw/london_boroughs"
DEFAULT_OUTPUT_ROOT = "clean"
LOG_NAME = "clean_unmatched.log"

# Supported data file extensions
DATA_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls")

# ---------------------------------------------------------------------------
# CSV helpers — preserve quoting so commas inside "1,234.56" are not delimiters
# ---------------------------------------------------------------------------
ENCODINGS = ("utf-8-sig", "utf-8", "latin-1", "cp1252")


def read_csv_rows(filepath):
    """Read a CSV file and return (rows, encoding_used).
    Sniffs the delimiter (comma, tab, semicolon, pipe) from the first 8KB.
    Uses csv.reader with default quoting so quoted commas are preserved."""
    last_err = None
    for enc in ENCODINGS:
        try:
            with open(filepath, "r", encoding=enc, newline="") as f:
                sample = f.read(8192)
                f.seek(0)
                # Sniff delimiter — try csv.Sniffer, fall back to tab detection
                delimiter = ","
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
                    delimiter = dialect.delimiter
                except csv.Error:
                    # Sniffer failed — check if tabs are more common than commas
                    if sample.count("\t") > sample.count(","):
                        delimiter = "\t"
                reader = csv.reader(f, delimiter=delimiter)
                rows = list(reader)
            return rows, enc
        except (UnicodeDecodeError, UnicodeError) as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not decode {filepath} with any of {ENCODINGS}: {last_err}")


def _read_xlsx_rows(filepath):
    """Read all rows from first non-underscore sheet of an xlsx/xlsm file.
    Returns list of lists of strings."""
    wb, close = _try_load_xlsx(filepath)
    if wb is None:
        raise RuntimeError(f"Could not open xlsx: {filepath}")
    try:
        target_sheets = [s for s in wb.sheetnames
                         if not s.startswith("_") and "LTR Template" not in s]
        if not target_sheets:
            target_sheets = wb.sheetnames
        ws = wb[target_sheets[0]]
        rows = []
        for vals in _iter_xlsx_rows(ws):
            rows.append([_fix_mojibake(v) for v in vals])
        return rows
    finally:
        try:
            close()
        except Exception:
            pass


def _read_xls_rows(filepath):
    """Read all rows from a legacy .xls (BIFF) file. Returns list of lists of strings."""
    try:
        import xlrd
    except ImportError:
        raise RuntimeError("xlrd not installed — cannot read .xls files")
    try:
        book = xlrd.open_workbook(filepath)
    except Exception as e:
        raise RuntimeError(f"Could not open xls: {filepath}: {e}")
    sheet = book.sheet_by_index(0)
    rows = []
    for rx in range(sheet.nrows):
        vals = [str(sheet.cell_value(rx, cx)).strip() for cx in range(sheet.ncols)]
        rows.append([_fix_mojibake(v) for v in vals])
    return rows


def read_data_rows(filepath):
    """Unified reader: detects actual format via magic bytes, reads any supported
    file type into a list of string rows. Returns (rows, 'utf-8').
    Raises RuntimeError for unreadable files or PDFs."""
    fmt = _resolve_format(filepath)
    if fmt == "pdf":
        raise RuntimeError("PDF file, not a spreadsheet")
    if fmt == "csv":
        return read_csv_rows(filepath)
    if fmt == "xlsx":
        return _read_xlsx_rows(filepath), "utf-8"
    if fmt == "xls":
        return _read_xls_rows(filepath), "utf-8"
    # Fallback: try as CSV
    return read_csv_rows(filepath)


def _normalise_cell(cell):
    """Normalise a single cell value to clean ASCII-safe equivalents.
    Applied to every cell on write so the clean directory has consistent encoding."""
    if not isinstance(cell, str):
        return cell
    return (cell
            .replace("\ufffd", "£")      # U+FFFD replacement char -> £ (was £ before bad decode)
            .replace("\u2018", "'")       # left single curly quote -> ASCII apostrophe
            .replace("\u2019", "'")       # right single curly quote -> ASCII apostrophe
            .replace("\u201c", '"')       # left double curly quote -> ASCII double quote
            .replace("\u201d", '"')       # right double curly quote -> ASCII double quote
            .replace("\u2013", "-")       # en-dash -> ASCII hyphen
            .replace("\u2014", "-")       # em-dash -> ASCII hyphen
            .replace("\u00a0", " ")       # non-breaking space -> plain space
            .replace("\u00ad", "")        # soft hyphen -> strip (invisible char)
            )


def _title_case_header(cell):
    """Title-case a header cell, preserving known abbreviations and
    PascalCase/camelCase names (e.g. OrganisationalUnit, IrrecoverableVATAmount).
    Only applied to the header row of output files."""
    if not isinstance(cell, str) or not cell.strip():
        return cell
    # Preserve PascalCase/camelCase: if any individual word (split on space
    # or underscore) has internal mixed case, leave the whole cell alone.
    # e.g. "OrganisationalUnit" has no separators and mixed case -> preserve.
    # e.g. "Non Recoverable Vat" splits into all-single-case words -> title-case.
    words = re.split(r'[\s_]+', cell)
    for w in words:
        if len(w) <= 1:
            continue
        if not w.isupper() and not w.islower() and not w.istitle():
            # Word has internal mixed case (PascalCase, camelCase, or acronym-embedded)
            return cell
    result = cell.title()
    # Restore known abbreviations that .title() lowered
    for abbr, wrong in (("ID", "Id"), ("VAT", "Vat"), ("URI", "Uri")):
        result = re.sub(rf'\b{wrong}\b', abbr, result)
    return result


def write_csv_rows(filepath, rows, encoding="utf-8-sig"):
    """Write rows to filepath as UTF-8 with BOM, quoting fields that contain
    commas/quotes/newlines.  Always writes utf-8-sig regardless of the
    encoding parameter (kept for call-site compatibility).
    The first row is title-cased as a header."""
    if not rows:
        return
    cleaned = [[_normalise_cell(cell) for cell in row] for row in rows]
    # Title-case the header row only
    cleaned[0] = [_title_case_header(cell) for cell in cleaned[0]]
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerows(cleaned)


def norm(s):
    """Normalise a cell for case-insensitive comparison: lowercase, collapse whitespace."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def row_matches(row, expected):
    """Case-insensitive exact match of two header rows (lists of cells)."""
    if len(row) != len(expected):
        return False
    return [norm(c) for c in row] == [norm(c) for c in expected]


# ---------------------------------------------------------------------------
# Borough rules
# ---------------------------------------------------------------------------
TOWER_HAMLETS_HEADER = [
    "Directorate", "Service", "Division", "Responsible Unit", "Expense Type",
    "Payment Date", "Trans No", "Net Amount", "Supplier Name",
]

# ---------------------------------------------------------------------------
# Tower Hamlets column-type detectors (for 5- and 6-field schema detection)
# ---------------------------------------------------------------------------
_TH_DATE_PATTERNS = [
    re.compile(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}'),   # DD/MM/YYYY, MM-DD-YY, etc.
    re.compile(r'\d{4}[/\-]\d{1,2}[/\-]\d{1,2}'),       # YYYY-MM-DD
]

_TH_FIELD_MATCHERS = {
    # field_name: regex to search in cell values (case-insensitive)
    "DIRECTORATE":          re.compile(r'RESOURCES|CHILDRENS.SERVICES', re.I),
    "MERCHANT NAME":        re.compile(r'AMAZON', re.I),
    "TRANS CAC DESC 1":     re.compile(r'COMMUNITY.EQUIP', re.I),
    "TRANS CAC DESC 2":     re.compile(r'EARLY.HELP', re.I),
}


def _th_is_date(value):
    """Return True if value looks like a date string."""
    v = str(value).strip()
    if not v:
        return False
    return any(p.search(v) for p in _TH_DATE_PATTERNS)


def _th_is_decimal(value):
    """Return True if value looks like a decimal number (including negative)."""
    v = str(value).strip().replace(",", "")
    if not v:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def _th_detect_column_order(rows, n_fields):
    """Scan data rows to determine which column is which field.
    Returns a list of field names in column order, or None if detection fails.
    Works for both 5-field and 6-field Tower Hamlets files.
    """
    all_fields_5 = ["TRANS DATE", "TRANS ORIGINAL NET AMT", "MERCHANT NAME",
                     "TRANS CAC DESC 1", "TRANS CAC DESC 2"]
    all_fields_6 = ["DIRECTORATE", "TRANS DATE", "TRANS ORIGINAL NET AMT",
                     "MERCHANT NAME", "TRANS CAC DESC 1", "TRANS CAC DESC 2"]
    all_fields = all_fields_6 if n_fields == 6 else all_fields_5

    # Tally evidence per column
    col_scores = {i: {f: 0 for f in all_fields} for i in range(n_fields)}

    sample_rows = [r for r in rows if len(r) >= n_fields and any(c.strip() for c in r)][:200]
    if not sample_rows:
        return None

    for row in sample_rows:
        for col_idx in range(n_fields):
            val = str(row[col_idx]).strip()
            if not val:
                continue
            if _th_is_date(val):
                col_scores[col_idx]["TRANS DATE"] += 1
            if _th_is_decimal(val):
                col_scores[col_idx]["TRANS ORIGINAL NET AMT"] += 1
            for field_name, regex in _TH_FIELD_MATCHERS.items():
                if field_name in all_fields and regex.search(val):
                    col_scores[col_idx][field_name] += 1

    # Greedy assignment: assign each field to the column with the highest score,
    # taking date and decimal first (most reliable detectors)
    assigned_cols = {}   # col_index -> field_name
    assigned_fields = set()
    priority_order = [f for f in ["TRANS DATE", "TRANS ORIGINAL NET AMT",
                                   "DIRECTORATE", "MERCHANT NAME",
                                   "TRANS CAC DESC 1", "TRANS CAC DESC 2"]
                      if f in all_fields]

    for field in priority_order:
        best_col = None
        best_score = 0
        for col_idx in range(n_fields):
            if col_idx in assigned_cols:
                continue
            score = col_scores[col_idx].get(field, 0)
            if score > best_score:
                best_score = score
                best_col = col_idx
        if best_col is not None and best_score > 0:
            assigned_cols[best_col] = field
            assigned_fields.add(field)

    # Any unassigned columns get remaining unassigned fields in order
    remaining_fields = [f for f in all_fields if f not in assigned_fields]
    remaining_cols = [i for i in range(n_fields) if i not in assigned_cols]
    for col_idx, field in zip(remaining_cols, remaining_fields):
        assigned_cols[col_idx] = field

    if len(assigned_cols) != n_fields:
        return None

    return [assigned_cols[i] for i in range(n_fields)]


SOUTHWARK_HEADER = [
    "date", "department", "beneficiary", "amount", "summary of purpose",
    "category", "Transaction number", "SAP vendor number",
]

GREENWICH_HEADER = [
    "Creditor_Name", "Invoice Line Amount", "Payment_Date",
    "Expenditure Category/Description", "LA Department",
]

# Ealing — list of (header_after_trim, n_cols_to_strip_from_left)
EALING_VARIANTS = [
    # 13-column headers (no leading "columns,," — Q4-ish files)
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Amended Supplier Name"], 0),
    # 15-column variant: ..., Relation, Amended Supplier Name, Cost Centre
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Relation", "Amended Supplier Name", "Cost Centre"], 2),
    # 16-column variant: ..., Supplier Name, Relation, Amended Supplier Name, Cost Centre
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Supplier Name", "Relation", "Amended Supplier Name", "Cost Centre"], 2),
    # 13-column variant ending in Supplier Name
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Supplier Name"], 2),
    # 13-column variant ending in Amended Supplier Name (with "columns" prefix)
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Amended Supplier Name"], 2),
    # 13-column variant ending in Name
    (["Body Name", "Organisation Code", "Service Label", "Service Code",
      "Organisation Unit", "Expenditure Category", "Expenditure Code",
      "Narrative", "Date", "Transaction No", "Net Amount", "Supplier ID",
      "Name"], 2),
]

SUTTON_OLD_HEADER = ["Account (T)", "Cat1 (T)", "Amount", "Text"]
SUTTON_NEW_HEADER = ["Account", "Cat1", "Amount", "Date", "Supplier", "narrative"]

HOUNSLOW_HEADER_WITH_BLANK = [
    "", "OrganisationalUnit", "ServiceCategoryLabel", "BeneficiaryName",
    "SupplierID", "PaymentDate", "TransactionNumber", "Amount",
    "IrrecoverableVATAmount", "Purpose", "CategoryInternalName",
]

HOUNSLOW_HEADER_CLEAN = [
    "OrganisationalUnit", "ServiceCategoryLabel", "BeneficiaryName",
    "SupplierID", "PaymentDate", "TransactionNumber", "Amount",
    "IrrecoverableVATAmount", "Purpose", "CategoryInternalName",
]

MERTON_HEADER_WITH_BLANK = [
    "", "Directorate", "Supplier Name", "Supplier Invoice No",
    "Payment Date", "Gross Invoice Value", "Vat Amount", "Vendor Id",
    "Purpose Of Expenditure", "Description", "Redact Flag", "Year", "Period",
]

MERTON_HEADER_CLEAN = [
    "Directorate", "Supplier Name", "Supplier Invoice No",
    "Payment Date", "Gross Invoice Value", "Vat Amount", "Vendor Id",
    "Purpose Of Expenditure", "Description", "Redact Flag", "Year", "Period",
]


# ---------------------------------------------------------------------------
# Per-borough cleaners
# Each returns (cleaned_rows_or_None, status_string)
#   cleaned_rows: the modified rows list, or None if no change/error
#   status: "modified", "unchanged", or "error: <message>"
# ---------------------------------------------------------------------------

def _th_effective_col_count(row):
    """Return the number of columns in a row after stripping trailing empty cells."""
    trimmed = list(row)
    while trimmed and not str(trimmed[-1]).strip():
        trimmed = trimmed[:-1]
    return len(trimmed)


def _th_detect_true_field_count(rows):
    """Check data rows (indices 5 and 10) to determine the true number of
    populated columns, guarding against trailing commas inflating len().
    Falls back to other non-empty rows if 5/10 are not available."""
    candidates = []
    for idx in (5, 10):
        if idx < len(rows) and any(str(c).strip() for c in rows[idx]):
            candidates.append(_th_effective_col_count(rows[idx]))
    # If we got at least one sample, use the maximum observed
    if candidates:
        return max(candidates)
    # Fallback: scan the first 20 non-empty data rows (skip row 0 which may be a header)
    for r in rows[1:21]:
        if any(str(c).strip() for c in r):
            candidates.append(_th_effective_col_count(r))
    return max(candidates) if candidates else None


def clean_tower_hamlets(rows):
    """Route by field count:
      5 fields  -> detect column order from data, prepend detected header
      6 fields  -> detect column order from data, prepend detected header
      9 fields  -> prepend 9-col header (if not already present)
      18 fields -> truncate every row to first 9 cols, then prepend 9-col header

    Uses data rows (rows 5 and 10) to determine the true column count,
    avoiding false 9-col detection caused by trailing commas.
    """
    if not rows:
        return None, "error: empty file"
    first = next((r for r in rows if any(c.strip() for c in r)), None)
    if first is None:
        return None, "error: no non-empty rows"

    n = len(first)

    # Guard against trailing commas: if the header row says 9 cols but data
    # rows only have 5 or 6 populated columns, use the data-derived count.
    if n not in (5, 6) and n != 18:
        true_n = _th_detect_true_field_count(rows)
        if true_n is not None and true_n in (5, 6) and n != true_n:
            n = true_n
            # Also trim trailing empty cells from every row to match
            rows = [r[:n] if len(r) > n else r for r in rows]
            first = next((r for r in rows if any(c.strip() for c in r)), first)

    if n in (5, 6):
        detected = _th_detect_column_order(rows, n)
        if detected is None:
            return None, f"error: could not detect {n}-field column order"
        # Check if header already present
        if row_matches(first, detected):
            return None, "unchanged"
        return [detected] + rows, "modified"

    if n == 9:
        if row_matches(first, TOWER_HAMLETS_HEADER):
            return None, "unchanged"
        return [TOWER_HAMLETS_HEADER] + rows, "modified"

    if n == 18:
        truncated = [r[:9] for r in rows]
        new_first = next((r for r in truncated if any(c.strip() for c in r)), None)
        if new_first is not None and row_matches(new_first, TOWER_HAMLETS_HEADER):
            return truncated, "modified"
        return [TOWER_HAMLETS_HEADER] + truncated, "modified"

    return None, f"error: unsupported field count {n} (expected 5, 6, 9, or 18)"


def clean_southwark(rows):
    """Two cases:
      (a) First row has 8 fields and first cell contains a backtick (`) —
          replace that first cell with 'Date'.
      (b) First row has 8 fields of data (no matching header) — prepend the
          full Southwark header.
      If first row already matches the expected header: unchanged.
    """
    if not rows:
        return None, "error: empty file"
    first = next((r for r in rows if any(c.strip() for c in r)), None)
    if first is None:
        return None, "error: no non-empty rows"
    if len(first) != 8:
        return None, f"error: expected 8 fields, got {len(first)}"
    if row_matches(first, SOUTHWARK_HEADER):
        return None, "unchanged"
    # Backtick-in-first-cell case: replace just that cell with 'Date'
    if "`" in first[0]:
        # Find the row in the original list to mutate it in place
        idx = rows.index(first)
        new_rows = [list(r) for r in rows]
        new_rows[idx][0] = "Date"
        return new_rows, "modified"
    # Otherwise prepend the full header
    return [SOUTHWARK_HEADER] + rows, "modified"


def clean_greenwich(rows):
    if not rows:
        return None, "error: empty file"
    first = next((r for r in rows if any(c.strip() for c in r)), None)
    if first is None:
        return None, "error: no non-empty rows"
    if len(first) != 5:
        return None, f"error: expected 5 fields, got {len(first)}"
    if row_matches(first, GREENWICH_HEADER):
        return None, "unchanged"
    new_rows = [GREENWICH_HEADER] + rows
    return new_rows, "modified"


def _ealing_strip_select_block(rows):
    """If first row contains 'SELECT' (case-insensitive in any cell), strip rows
    from the start through (and including) the row containing 'columns' in any
    of cells 1..3. Returns the trimmed rows, or rows unchanged if no SELECT."""
    if not rows:
        return rows
    first_has_select = any("select" in norm(c) for c in rows[0])
    if not first_has_select:
        return rows
    # Find the index of the row containing 'columns' in the first 3 cells
    cutoff = None
    for i, row in enumerate(rows):
        for c in row[:3]:
            if norm(c) == "columns":
                cutoff = i
                break
        if cutoff is not None:
            break
    if cutoff is None:
        # SELECT present but no 'columns' marker — leave alone, will fail to match
        return rows
    return rows[cutoff + 1:]


def _ealing_strip_query_block(rows):
    """If first non-empty row contains 'query' (case-insensitive, anywhere in any
    cell), delete that row and strip the first 2 columns from every remaining row.
    Returns (new_rows, triggered) where triggered is True if the rule fired."""
    if not rows:
        return rows, False
    # Find first non-empty row
    first_idx = next((i for i, r in enumerate(rows) if any(c.strip() for c in r)), None)
    if first_idx is None:
        return rows, False
    first = rows[first_idx]
    if not any("query" in norm(c) for c in first):
        return rows, False
    # Delete the query row, then strip first 2 cols from everything that follows
    remaining = rows[:first_idx] + rows[first_idx + 1:]
    stripped = [r[2:] for r in remaining]
    return stripped, True


def clean_ealing(rows):
    if not rows:
        return None, "error: empty file"

    # Rule: query row present -> delete it + strip first 2 cols from all remaining rows
    work, query_triggered = _ealing_strip_query_block(rows)
    if not work:
        return None, "error: no rows after query stripping"

    # Rule: SELECT in first row -> strip down through the 'columns' marker row
    if not query_triggered:
        work = _ealing_strip_select_block(work)
        if not work:
            return None, "error: no rows after SELECT/columns stripping"

    # First non-empty row is the header candidate
    header_idx = next((i for i, r in enumerate(work) if any(c.strip() for c in r)), None)
    if header_idx is None:
        return None, "error: no non-empty rows after stripping"
    candidate = work[header_idx]

    # If query pre-processing already stripped 2 cols, match variants with n_strip=0.
    # Otherwise use each variant's own n_strip value.
    for expected, n_strip in EALING_VARIANTS:
        effective_strip = 0 if query_triggered else n_strip
        trimmed = candidate[effective_strip:] if effective_strip else candidate[:]
        # Some variants have a trailing comma — drop trailing empty cells before comparing
        while trimmed and not trimmed[-1].strip():
            trimmed = trimmed[:-1]
        if row_matches(trimmed, expected):
            new_header = expected
            new_data = []
            for r in work[header_idx + 1:]:
                stripped_row = r[effective_strip:] if effective_strip else r[:]
                stripped_row = stripped_row[: len(expected)]
                if len(stripped_row) < len(expected):
                    stripped_row = stripped_row + [""] * (len(expected) - len(stripped_row))
                new_data.append(stripped_row)
            return [new_header] + new_data, "modified"

    return None, "error: no Ealing variant matched"


def clean_croydon(rows):
    """Find the header row (first two cells are 'Payment Date', 'Vendor Name')
    and remove all rows above it."""
    if not rows:
        return None, "error: empty file"
    target = ["payment date", "vendor name"]
    header_idx = None
    for i, row in enumerate(rows):
        if len(row) >= 2 and [norm(row[0]), norm(row[1])] == target:
            header_idx = i
            break
    if header_idx is None:
        return None, "error: header row (Payment Date, Vendor Name) not found"
    if header_idx == 0:
        return None, "unchanged"
    return rows[header_idx:], "modified"


def clean_sutton(rows):
    """If first row matches the old 4-column header (ignoring any trailing
    empty cells), replace the entire first row with the new 6-column header.
    Files whose first row doesn't match are left unchanged — no error."""
    if not rows:
        return None, "error: empty file"
    first = list(rows[0])
    # Drop trailing empty cells before comparing
    trimmed = first[:]
    while trimmed and not trimmed[-1].strip():
        trimmed = trimmed[:-1]
    if not row_matches(trimmed, SUTTON_OLD_HEADER):
        return None, "unchanged"
    new_rows = [SUTTON_NEW_HEADER] + rows[1:]
    return new_rows, "modified"


def clean_hounslow(rows):
    """If the header row has a blank first column followed by the known
    Hounslow fields, strip that first column from every row."""
    if not rows:
        return None, "error: empty file"
    # Find the header row
    for i, row in enumerate(rows):
        trimmed = [c.strip() for c in row]
        if len(trimmed) >= len(HOUNSLOW_HEADER_WITH_BLANK) and \
           row_matches(trimmed[:len(HOUNSLOW_HEADER_WITH_BLANK)], HOUNSLOW_HEADER_WITH_BLANK):
            new_rows = [HOUNSLOW_HEADER_CLEAN] + [r[1:] for r in rows[i + 1:]]
            return new_rows, "modified"
    return None, "unchanged"


def clean_merton(rows):
    """If the header row has a blank first column followed by the known
    Merton fields, strip that first column from every row."""
    if not rows:
        return None, "error: empty file"
    # Find the header row
    for i, row in enumerate(rows):
        trimmed = [c.strip() for c in row]
        if len(trimmed) >= len(MERTON_HEADER_WITH_BLANK) and \
           row_matches(trimmed[:len(MERTON_HEADER_WITH_BLANK)], MERTON_HEADER_WITH_BLANK):
            new_rows = [MERTON_HEADER_CLEAN] + [r[1:] for r in rows[i + 1:]]
            return new_rows, "modified"
    return None, "unchanged"


def clean_redbridge(rows):
    """Redbridge files have a 'Period' column containing YYYYMM values (e.g. 202307).
    This cleaner finds the Period column in the header row and converts every
    YYYYMM value to DD/MM/YYYY using the 1st of the month (e.g. 202307 → 01/07/2023).
    The header name 'Period' is left unchanged.

    Some source files format the period with a thousands separator (e.g. "202,502")
    or currency symbols — these are stripped before matching. The year and month are
    validated (year 2000-2099, month 01-12) to avoid converting stray numbers."""
    if not rows:
        return None, "error: empty file"

    # Find the header row and the index of the Period column
    header_idx = None
    period_col = None
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            if norm(cell) == "period":
                header_idx = i
                period_col = j
                break
        if header_idx is not None:
            break

    if header_idx is None or period_col is None:
        return None, "unchanged"

    new_rows = list(rows)  # shallow copy
    modified = False

    for i in range(header_idx + 1, len(new_rows)):
        row = new_rows[i]
        if period_col >= len(row):
            continue
        # Strip commas, currency symbols, and whitespace before matching
        val = str(row[period_col]).strip()
        val = val.replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip()
        if re.fullmatch(r'\d{6}', val):
            yyyy = int(val[:4])
            mm = int(val[4:6])
            # Validate year and month to avoid converting stray numbers
            if 2000 <= yyyy <= 2099 and 1 <= mm <= 12:
                new_row = list(row)
                new_row[period_col] = f"01/{mm:02d}/{yyyy:04d}"
                new_rows[i] = new_row
                modified = True

    if not modified:
        return None, "unchanged"
    return new_rows, "modified"


# ---------------------------------------------------------------------------
# Generic header-row cleaner: uses check_AMOD_header logic to find the header
# row and strips all rows above it. Applied to boroughs without specific rules.
# ---------------------------------------------------------------------------
def _find_header_row_index_csv(rows, filepath):
    """Use check_AMOD_header's marker-based detection to find the header row index
    within an already-loaded list of CSV rows. Returns the row index or None."""
    from check_AMOD_header_X4 import _HEADER_MARKERS, _is_header_row

    is_croydon = "croydon" in filepath.lower()
    max_scan = min(len(rows), 100)

    for i in range(max_scan):
        row = rows[i]
        stripped = [c.strip() if c is not None else "" for c in row]
        # Filter out cells longer than 200 chars (SQL blobs)
        short_cells = [c if len(c) <= 200 else "" for c in stripped]
        non_empty = [v for v in short_cells if v]
        if len(non_empty) < 3 or not _is_header_row(short_cells):
            continue
        # Croydon fill-ratio check
        if is_croydon:
            total_cells = len(stripped)
            if len(non_empty) < 3 or (total_cells > 0 and len(non_empty) / total_cells < 0.6):
                continue
        return i
    return None


def clean_generic_strip_preamble(rows, filepath=""):
    """Find the header row using marker-word detection and strip all rows above it.
    Used as a fallback for boroughs without specific cleaning rules."""
    if not rows:
        return None, "error: empty file"

    header_idx = _find_header_row_index_csv(rows, filepath)
    if header_idx is None:
        return None, "unchanged"  # no header detected, leave file as-is
    if header_idx == 0:
        return None, "unchanged"
    return rows[header_idx:], "modified"


def clean_wandsworth(rows):
    """Wandsworth files have 'Payment Amount' appearing twice in the header —
    the first occurrence is actually the Payment Date column.  Replace only the
    first 'Payment Amount' with 'Payment Date'.  If the header doesn't have
    the duplicate, leave the file unchanged."""
    if not rows:
        return None, "error: empty file"
    # Find the header row (first non-empty row)
    header_idx = None
    for i, row in enumerate(rows):
        if any(str(c).strip() for c in row):
            header_idx = i
            break
    if header_idx is None:
        return None, "error: no non-empty rows"

    header = rows[header_idx]
    # Find all indices where cell matches 'payment amount' (case-insensitive)
    pa_indices = [j for j, c in enumerate(header)
                  if norm(c) == "payment amount"]

    if len(pa_indices) < 2:
        return None, "unchanged"

    # Replace the first occurrence with 'Payment Date'
    new_rows = [list(r) for r in rows]
    new_rows[header_idx][pa_indices[0]] = "Payment Date"
    return new_rows, "modified"


# Borough name (case-insensitive) → cleaner function
BOROUGH_CLEANERS = {
    "tower hamlets": clean_tower_hamlets,
    "southwark":     clean_southwark,
    "greenwich":     clean_greenwich,
    "ealing":        clean_ealing,
    "croydon":       clean_croydon,
    "sutton":        clean_sutton,
    "hounslow":      clean_hounslow,
    "merton":        clean_merton,
    "redbridge":     clean_redbridge,
    "wandsworth":    clean_wandsworth,
}


# ---------------------------------------------------------------------------
# File-specific overrides: for files where the borough cleaner errors and
# generic strip also fails, we forcibly apply a known header.
# Each entry maps (borough_key, lowercase_filename) -> handler function.
# Handlers receive (rows) and return (new_rows, status) like borough cleaners.
# ---------------------------------------------------------------------------

# Ealing credit card files: header already in file, just accept it
_EALING_CC_HEADER_4COL = ["POSTING DATE", "TRANSACTION AMOUNT", "MERCHANT NAME", "DESCRIPTION"]
_EALING_CC_HEADER_4COL_V2 = ["Date", "Merchant Name", "Transaction Amount", "Description"]

_EALING_CC_4COL_FILES = {
    "april_2022_-_credit_card.xlsx",
    "april_2023_to_february_2024_credit_card_spend.xlsx",
    "august_2022_-_credit_card.xlsx",
    "december_2022_-_credit_card.xlsx",
    "february_2023_-_credit_card.xlsx",
    "january_2023_-_credit_card.xlsx",
    "july_2022_-_credit_card.xlsx",
    "june_2022_-_credit_card.xlsx",
    "march_2023_-_credit_card.xlsx",
    "may_2022_-_credit_card.xlsx",
    "november_2022_-_credit_card.xlsx",
    "october_2022_-_credit_card.xlsx",
    "september_2022_-_credit_card.xlsx",
}


def _override_ealing_cc_4col(rows):
    """Ealing credit card files with 4 columns (POSTING DATE header).
    The header already exists in the file — find it and strip any preamble above."""
    if not rows:
        return None, "error: empty file"
    target = [norm(c) for c in _EALING_CC_HEADER_4COL]
    for i, row in enumerate(rows):
        trimmed = [c for c in row if c.strip()]
        if len(trimmed) >= 4 and [norm(c) for c in trimmed[:4]] == target:
            return rows[i:], "modified"
    # Header not found in expected form — prepend it above all rows
    return [_EALING_CC_HEADER_4COL] + rows, "modified"


def _override_ealing_cc_4col_v2(rows):
    """Ealing credit card spend April-November 2024 (Date, Merchant Name, ...).
    The header already exists in the file — find it and strip any preamble above."""
    if not rows:
        return None, "error: empty file"
    target = [norm(c) for c in _EALING_CC_HEADER_4COL_V2]
    for i, row in enumerate(rows):
        trimmed = [c for c in row if c.strip()]
        if len(trimmed) >= 4 and [norm(c) for c in trimmed[:4]] == target:
            return rows[i:], "modified"
    return [_EALING_CC_HEADER_4COL_V2] + rows, "modified"


def _override_greenwich_use_existing(rows):
    """Greenwich files where the header already exists but the cleaner rejects
    because field count != 5.  Accept the existing first non-empty row as the header."""
    if not rows:
        return None, "error: empty file"
    first = next((r for r in rows if any(c.strip() for c in r)), None)
    if first is None:
        return None, "error: no non-empty rows"
    # The file already has its header — return rows as-is (mark modified so AMOD_ is written)
    first_idx = rows.index(first)
    return rows[first_idx:], "modified"


def _override_southwark_use_existing(rows):
    """Southwark file where the cleaner rejects because field count != 8.
    Accept the existing first non-empty row as the header."""
    if not rows:
        return None, "error: empty file"
    first = next((r for r in rows if any(c.strip() for c in r)), None)
    if first is None:
        return None, "error: no non-empty rows"
    first_idx = rows.index(first)
    return rows[first_idx:], "modified"


_TH_PCARD_HEADER = ["TRANS DATE", "TRANS ORIGINAL NET AMT", "TRANS CAC DESC 1",
                     "MERCHANT NAME", "TRANS CAC DESC 2"]


def _override_th_pcard_tab_split(rows):
    """Tower Hamlets PCARD file where all fields are in a single column separated
    by tabs.  Split the single field into multiple fields and prepend the header."""
    if not rows:
        return None, "error: empty file"
    new_rows = []
    for row in rows:
        # If row has only 1 field (or all content is in the first cell), split by tab
        if len(row) == 1 or (len(row) >= 1 and all(not c.strip() for c in row[1:])):
            cell = row[0] if row else ""
            split = cell.split("\t")
            new_rows.append([c.strip() for c in split])
        else:
            new_rows.append(row)
    # Check if the first non-empty row already looks like our header
    first = next((r for r in new_rows if any(c.strip() for c in r)), None)
    if first is not None and row_matches(first, _TH_PCARD_HEADER):
        return new_rows, "modified"
    return [_TH_PCARD_HEADER] + new_rows, "modified"


# Build the file-specific override lookup: (borough_key, lowercase_filename) -> handler
FILE_SPECIFIC_OVERRIDES = {}

# Ealing credit card 4-col files
for _fn in _EALING_CC_4COL_FILES:
    FILE_SPECIFIC_OVERRIDES[("ealing", _fn.lower())] = _override_ealing_cc_4col

# Ealing credit card v2 file
FILE_SPECIFIC_OVERRIDES[("ealing", "credit_card_spend_april_2024_-_november_2024.xlsx")] = _override_ealing_cc_4col_v2

# Greenwich files — use existing header
for _fn in [
    "Greater than £500 Qtr 2 Jul to Sep 25-26 Final for Publishing.xlsx",
    "Greater_than___500_Qtr_3_Oct_to_Dec_23_24_Final_for_Publishing.xlsx",
    "Payments_over_500_Qtr_3_Oct_to_Dec_24_25.xlsx",
]:
    FILE_SPECIFIC_OVERRIDES[("greenwich", _fn.lower())] = _override_greenwich_use_existing

# Southwark — use existing header
FILE_SPECIFIC_OVERRIDES[("southwark", "council spending january 2024.xlsx")] = _override_southwark_use_existing

# Tower Hamlets PCARD — tab-split + header
FILE_SPECIFIC_OVERRIDES[("tower hamlets", "pcard-aug-2025.xlsx")] = _override_th_pcard_tab_split


def borough_from_dirname(dirname):
    """Convert directory name like 'Tower_Hamlets' → 'tower hamlets' for lookup."""
    return dirname.replace("_", " ").strip().lower()


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, path):
        self.path = path
        self.entries = []

    def add(self, borough, filepath, message, detail=None):
        ts = datetime.now().isoformat(timespec="seconds")
        fname = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()
        try:
            fsize = os.path.getsize(filepath)
            if fsize > 1_048_576:
                size_str = f"{fsize / 1_048_576:.1f}MB"
            elif fsize > 1024:
                size_str = f"{fsize / 1024:.1f}KB"
            else:
                size_str = f"{fsize}B"
        except Exception:
            size_str = "?"
        detail_str = f"\t{detail}" if detail else ""
        self.entries.append(
            f"{ts}\t{borough}\t{fname}\t{ext}\t{size_str}\t{message}{detail_str}"
        )

    def write(self):
        if not self.entries:
            if os.path.exists(self.path):
                os.remove(self.path)
            return
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("timestamp\tborough\tfilename\text\tsize\terror\tdetail\n")
            for line in self.entries:
                f.write(line + "\n")

    def print_summary(self):
        """Print a grouped summary of all logged errors to stderr."""
        if not self.entries:
            return
        # Group by borough
        by_borough = {}
        for entry in self.entries:
            parts = entry.split("\t")
            borough = parts[1] if len(parts) > 1 else "unknown"
            by_borough.setdefault(borough, []).append(entry)
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"ERROR DETAILS ({len(self.entries)} total)", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        for borough in sorted(by_borough):
            entries = by_borough[borough]
            print(f"\n  [{borough}] — {len(entries)} error(s):", file=sys.stderr)
            for entry in entries:
                parts = entry.split("\t")
                # parts: ts, borough, filename, ext, size, error, [detail]
                fname = parts[2] if len(parts) > 2 else "?"
                ext = parts[3] if len(parts) > 3 else "?"
                size = parts[4] if len(parts) > 4 else "?"
                error = parts[5] if len(parts) > 5 else "?"
                detail = parts[6] if len(parts) > 6 else ""
                print(f"    {fname} ({ext}, {size})", file=sys.stderr)
                print(f"      Error: {error}", file=sys.stderr)
                if detail:
                    # Indent multi-line detail (e.g. tracebacks)
                    for dline in detail.split("\\n"):
                        print(f"        {dline}", file=sys.stderr)


def process_file(src_path, borough_dir_name, mode, output_root, source_root, logger):
    """Process a single file. Returns one of: 'modified', 'copied', 'unchanged', 'error', 'skipped'."""
    import traceback as _tb

    borough_key = borough_from_dirname(borough_dir_name)
    cleaner = BOROUGH_CLEANERS.get(borough_key)
    cleaner_name = cleaner.__name__ if cleaner else "generic_strip_preamble"

    ext = os.path.splitext(src_path)[1].lower()
    is_data_file = ext in DATA_EXTENSIONS or ext == ""
    fname = os.path.basename(src_path)

    if not is_data_file:
        return "skipped"

    # Detect actual format for logging
    try:
        actual_fmt = _resolve_format(src_path) or "unknown"
    except Exception:
        actual_fmt = "unknown"
    fmt_note = f" (actual format: {actual_fmt})" if actual_fmt != ext.lstrip(".") else ""

    # Derive the output filename.
    # Files with an extension: always .csv (existing behaviour).
    # Extensionless files: append the discovered format extension.
    def _csv_output_name(path):
        base, _ = os.path.splitext(path)
        return base + ".csv"

    # In AMOD mode: remove stale AMOD_ copies, then process
    if mode == "amod":
        for amod_ext in [ext, ".csv"]:
            stale_base = os.path.splitext(fname)[0] + amod_ext
            stale = os.path.join(os.path.dirname(src_path), "AMOD_" + stale_base)
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError as e:
                    logger.add(borough_key, src_path,
                               "could not remove stale AMOD_",
                               detail=str(e))

    # Note: cleaner may be None for boroughs without specific rules.
    # The two-step logic below always runs generic strip first, then
    # applies the borough cleaner if one exists.

    # Read the file (any format)
    rows = None
    encoding = "utf-8"
    try:
        rows, encoding = read_data_rows(src_path)
    except RuntimeError as e:
        msg = str(e)
        if "PDF" in msg:
            logger.add(borough_key, src_path,
                        f"PDF file, not a spreadsheet — skipped{fmt_note}")
            print(f"  [{'skipped':9s}] {src_path}  — PDF masquerading as {ext}", file=sys.stderr)
            return "skipped"
        logger.add(borough_key, src_path,
                    f"read failed ({actual_fmt}): {e}",
                    detail=_tb.format_exc().replace("\n", "\\n"))
        print(f"  [{'error':9s}] {src_path}", file=sys.stderr)
        print(f"             Read failed ({actual_fmt}{fmt_note}): {e}", file=sys.stderr)
        return "error"
    except Exception as e:
        logger.add(borough_key, src_path,
                    f"read failed ({actual_fmt}): {type(e).__name__}: {e}",
                    detail=_tb.format_exc().replace("\n", "\\n"))
        print(f"  [{'error':9s}] {src_path}", file=sys.stderr)
        print(f"             Read failed ({actual_fmt}{fmt_note}): {type(e).__name__}: {e}", file=sys.stderr)
        return "error"

    row_count = len(rows) if rows else 0
    col_count = len(rows[0]) if rows else 0
    first_row_preview = ""
    if rows:
        non_empty = [v for v in rows[0] if v.strip()][:4]
        first_row_preview = ", ".join(f"'{v[:30]}'" for v in non_empty)
        if len(non_empty) < len([v for v in rows[0] if v.strip()]):
            first_row_preview += ", ..."

    # ----- Step 1: Generic preamble stripping (uses check_AMOD_header logic) -----
    # Always run this first to find and strip rows above the header row.
    generic_result, generic_status = clean_generic_strip_preamble(rows, src_path)
    if generic_status == "modified":
        working_rows = generic_result
        preamble_stripped = True
    else:
        working_rows = rows
        preamble_stripped = False

    # ----- Step 2: Apply borough-specific cleaner (if any) -----
    if cleaner is not None:
        try:
            new_rows, status = cleaner(working_rows)
            # If cleaner says unchanged but generic strip already removed preamble,
            # use the generic-stripped result
            if status == "unchanged" and preamble_stripped:
                new_rows, status = generic_result, "modified"
        except Exception as e:
            # Borough cleaner crashed — fall back to generic result
            logger.add(borough_key, src_path,
                        f"cleaner '{cleaner_name}' raised: {type(e).__name__}: {e} — using generic strip",
                        detail=f"rows={row_count}, cols={col_count}, first_row=[{first_row_preview}]\\n{_tb.format_exc().replace(chr(10), '\\n')}")
            print(f"  [{'warning':9s}] {src_path}", file=sys.stderr)
            print(f"             Cleaner '{cleaner_name}' raised {type(e).__name__}: {e}", file=sys.stderr)
            print(f"             Falling back to generic header-row stripping", file=sys.stderr)
            new_rows = generic_result if preamble_stripped else None
            status = "modified" if preamble_stripped else "unchanged"
            # If generic strip didn't help, try file-specific override
            if not preamble_stripped:
                override_fn = FILE_SPECIFIC_OVERRIDES.get((borough_key, fname.lower()))
                if override_fn is not None:
                    try:
                        new_rows, status = override_fn(rows)
                        if status == "modified" and new_rows is not None:
                            print(f"             Applied file-specific override ({override_fn.__name__})", file=sys.stderr)
                    except Exception as e_ov:
                        print(f"             File-specific override also failed: {e_ov}", file=sys.stderr)
                        new_rows = None
                        status = "unchanged"

        if status.startswith("error"):
            # Borough cleaner returned an error — fall back to generic result
            logger.add(borough_key, src_path,
                        f"cleaner '{cleaner_name}': {status} — using generic strip",
                        detail=f"rows={row_count}, cols={col_count}, first_row=[{first_row_preview}]")
            print(f"  [{'warning':9s}] {src_path}", file=sys.stderr)
            print(f"             Cleaner '{cleaner_name}': {status}", file=sys.stderr)
            print(f"             File: {row_count} rows × {col_count} cols, format={actual_fmt}{fmt_note}", file=sys.stderr)
            print(f"             First row: [{first_row_preview}]", file=sys.stderr)
            if preamble_stripped:
                print(f"             Falling back to generic header-row stripping ({len(rows) - len(working_rows)} preamble rows removed)", file=sys.stderr)
                new_rows = generic_result
                status = "modified"
            else:
                # ----- Step 2b: Try file-specific override before giving up -----
                override_fn = FILE_SPECIFIC_OVERRIDES.get((borough_key, fname.lower()))
                if override_fn is not None:
                    try:
                        new_rows, status = override_fn(rows)
                    except Exception as e_ov:
                        logger.add(borough_key, src_path,
                                    f"file-specific override raised: {type(e_ov).__name__}: {e_ov}",
                                    detail=f"rows={row_count}, cols={col_count}")
                        print(f"             File-specific override also failed: {e_ov}", file=sys.stderr)
                        new_rows = None
                        status = "error: override failed"
                    if status == "modified" and new_rows is not None:
                        print(f"             Applied file-specific override ({override_fn.__name__})", file=sys.stderr)
                        # fall through to the normal write logic below
                    else:
                        print(f"             File-specific override did not help: {status}", file=sys.stderr)
                        if mode == "clean":
                            rel = os.path.relpath(src_path, source_root)
                            dst_path = _csv_output_name(os.path.join(output_root, rel))
                            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                            try:
                                write_csv_rows(dst_path, rows, encoding=encoding)
                            except Exception as e2:
                                logger.add(borough_key, src_path,
                                            f"fallback write failed: {e2}",
                                            detail=f"dst={dst_path}")
                                print(f"             Fallback write also failed: {e2}", file=sys.stderr)
                        return "error"
                else:
                    print(f"             No generic header detected either — writing original rows", file=sys.stderr)
                    if mode == "clean":
                        rel = os.path.relpath(src_path, source_root)
                        dst_path = _csv_output_name(os.path.join(output_root, rel))
                        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                        try:
                            write_csv_rows(dst_path, rows, encoding=encoding)
                        except Exception as e2:
                            logger.add(borough_key, src_path,
                                        f"fallback write failed: {e2}",
                                        detail=f"dst={dst_path}")
                            print(f"             Fallback write also failed: {e2}", file=sys.stderr)
                    return "error"
    else:
        # No borough-specific cleaner — use generic result directly
        new_rows = generic_result if preamble_stripped else None
        status = "modified" if preamble_stripped else "unchanged"

    if status == "unchanged":
        if mode == "amod":
            # Always produce an AMOD_ copy for every file
            amod_base = "AMOD_" + os.path.splitext(fname)[0] + ".csv"
            dst_path = os.path.join(os.path.dirname(src_path), amod_base)
            try:
                write_csv_rows(dst_path, rows, encoding=encoding)
                return "modified"
            except Exception as e:
                logger.add(borough_key, src_path,
                            f"write failed: {type(e).__name__}: {e}",
                            detail=f"dst={dst_path}, rows={row_count}")
                print(f"  [{'error':9s}] {src_path}", file=sys.stderr)
                print(f"             Write failed: {e}", file=sys.stderr)
                return "error"
        if mode == "clean":
            rel = os.path.relpath(src_path, source_root)
            dst_path = _csv_output_name(os.path.join(output_root, rel))
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            try:
                write_csv_rows(dst_path, rows, encoding=encoding)
                return "copied"
            except Exception as e:
                logger.add(borough_key, src_path,
                            f"write failed: {type(e).__name__}: {e}",
                            detail=f"dst={dst_path}, rows={row_count}")
                print(f"  [{'error':9s}] {src_path}", file=sys.stderr)
                print(f"             Write failed: {e}", file=sys.stderr)
                return "error"
        return "unchanged"

    if status == "modified":
        if mode == "amod":
            amod_base = "AMOD_" + os.path.splitext(fname)[0] + ".csv"
            dst_path = os.path.join(os.path.dirname(src_path), amod_base)
        else:  # clean
            rel = os.path.relpath(src_path, source_root)
            dst_path = _csv_output_name(os.path.join(output_root, rel))
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        try:
            write_csv_rows(dst_path, new_rows, encoding=encoding)
            return "modified"
        except Exception as e:
            logger.add(borough_key, src_path,
                        f"write failed: {type(e).__name__}: {e}",
                        detail=f"dst={dst_path}, new_rows={len(new_rows) if new_rows else 0}")
            print(f"  [{'error':9s}] {src_path}", file=sys.stderr)
            print(f"             Write failed: {e}", file=sys.stderr)
            return "error"

    logger.add(borough_key, src_path, f"unknown status: {status}")
    print(f"  [{'error':9s}] {src_path}  — unknown status: {status}", file=sys.stderr)
    return "error"


def walk_source(source_root):
    """Yield (borough_dir_name, full_filepath) for every data file under source_root,
    skipping AMOD_ files, manifests, and hidden/system files."""
    for entry in sorted(os.listdir(source_root)):
        bdir = os.path.join(source_root, entry)
        if not os.path.isdir(bdir):
            continue
        for root, _, files in os.walk(bdir):
            for fn in sorted(files):
                if fn.startswith(("AMOD_", ".", "_")):
                    continue
                if fn.lower().endswith((".json", ".log")):
                    continue
                # Include files with a recognised data extension OR no extension at all
                ext = os.path.splitext(fn)[1]
                if fn.lower().endswith(DATA_EXTENSIONS) or ext == "":
                    yield entry, os.path.join(root, fn)


def main():
    parser = argparse.ArgumentParser(description="Clean borough data files (per-borough header rules)")
    parser.add_argument("--mode", choices=("amod", "clean"), default="amod",
                        help="amod (default): write AMOD_<file> beside originals. "
                             "clean: mirror tree to <output>/london_boroughs/")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help=f"Source root (default: {DEFAULT_SOURCE})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_ROOT,
                        help=f"Output root for clean mode (default: {DEFAULT_OUTPUT_ROOT})")
    parser.add_argument("--files", nargs="+", metavar="FILENAME",
                        help="Process only these specific filenames (matched case-insensitively "
                             "against the basename). Substring matching is used, so partial names "
                             "work too. Example: --files april_2022 PCARD-Aug")
    parser.add_argument("--borough", nargs="+", metavar="BOROUGH",
                        help="Process only these boroughs (matched case-insensitively against "
                             "the borough directory name). Substring matching is used, so partial "
                             "names work too. Example: --borough Croydon \"Tower Hamlets\"")
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        print(f"ERROR: source directory not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    # Build the file filter (if any)
    file_filters = None
    if args.files:
        file_filters = [f.lower() for f in args.files]

    # Build the borough filter (if any)
    borough_filters = None
    if args.borough:
        borough_filters = [b.lower() for b in args.borough]

    # In clean mode, build mirrored output under <output>/<basename of source>/
    output_root = args.output
    if args.mode == "clean":
        output_root = os.path.join(args.output, os.path.basename(args.source.rstrip("/\\")))
        os.makedirs(output_root, exist_ok=True)

    log_dir = output_root if args.mode == "clean" else args.source
    log_path = os.path.join(log_dir, LOG_NAME)
    logger = Logger(log_path)

    counts = {"modified": 0, "copied": 0, "unchanged": 0, "error": 0, "skipped": 0}
    borough_errors = {}   # borough -> count
    borough_counts = {}   # borough -> total files processed

    print(f"Mode: {args.mode}")
    print(f"Source: {args.source}")
    if args.mode == "clean":
        print(f"Output: {output_root}")
    if file_filters:
        print(f"Filter: only files matching {args.files}")
    if borough_filters:
        print(f"Filter: only boroughs matching {args.borough}")
    print()

    for borough_dir, filepath in walk_source(args.source):
        # If --borough was given, skip any borough that doesn't match
        if borough_filters:
            dir_lower = borough_dir.lower()
            if not any(b in dir_lower for b in borough_filters):
                continue

        # If --files was given, skip any file that doesn't match a filter
        if file_filters:
            fname_lower = os.path.basename(filepath).lower()
            if not any(f in fname_lower for f in file_filters):
                continue

        borough_key = borough_from_dirname(borough_dir)
        borough_counts[borough_key] = borough_counts.get(borough_key, 0) + 1

        result = process_file(filepath, borough_dir, args.mode, output_root, args.source, logger)
        counts[result] = counts.get(result, 0) + 1

        if result == "error":
            borough_errors[borough_key] = borough_errors.get(borough_key, 0) + 1
        if result == "modified":
            print(f"  [modified ] {filepath}")

    logger.write()

    # Print summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total = sum(counts.values())
    print(f"  Total files processed: {total}")
    for k in ("modified", "copied", "unchanged", "skipped", "error"):
        if k in counts and counts[k]:
            print(f"  {k:10s}: {counts[k]}")

    # Per-borough error breakdown
    if borough_errors:
        print(f"\n  Errors by borough:")
        for borough in sorted(borough_errors):
            total_b = borough_counts.get(borough, 0)
            err_b = borough_errors[borough]
            print(f"    {borough:25s}: {err_b:4d} errors / {total_b:4d} files")

    if logger.entries:
        print(f"\n  Error log: {log_path}")
        # Print detailed error summary to stderr
        logger.print_summary()
    else:
        print(f"\n  No errors. {LOG_NAME} not written.")


if __name__ == "__main__":
    main()
