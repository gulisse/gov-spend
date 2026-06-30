"""
config.py — Shared configuration for the taxonomy enrichment pipeline.

All scripts import from here. Update these values once rather than
editing each script individually.
"""

# ─────────────────────────────────────────────
# Google Cloud / Gemini
# ─────────────────────────────────────────────
GCP_PROJECT = "gov-data-497806"
GCP_LOCATION = "global"
MODEL_NAME = "gemini-2.5-flash-lite"    # implicit caching enabled by default
GCS_BUCKET = "gov-data-497806-batch"    # GCS bucket for batch I/O
GCS_PREFIX = "taxonomy_enrichment"      # prefix inside the bucket

# ─────────────────────────────────────────────
# Model generation parameters  (classification)
# ─────────────────────────────────────────────
MODEL_TEMPERATURE = 0
MODEL_TOP_K = 1
MODEL_TOP_P = 1.0
MODEL_MAX_OUTPUT_TOKENS = 256
MODEL_CANDIDATE_COUNT = 1

# ─────────────────────────────────────────────
# Default file paths
# ─────────────────────────────────────────────
DEFAULT_SPEND_FILE = "Normalized_spend.csv"
TAXONOMY_FILE = "taxonomy.csv"
PRIORITY_RULES_FILE = "priority_rules.md"
TAXONOMY_BASE_FILE = "tbl_taxonomy_base.csv"
TAXONOMY_BASE_ENRICHED_FILE = "tbl_taxonomy_base_enriched.csv"
SPEND_ENRICHED_FILE = "Normalized_spend_enriched.csv"

# ─────────────────────────────────────────────
# Batch API
# ─────────────────────────────────────────────
BATCH_SIZE = 10_000
BATCH_OUTPUT_DIR = "batch_input"
BATCH_RESULTS_DIR = "batch_results"
BATCH_MANIFEST_FILE = "batch_jobs_manifest.json"
MAX_CONCURRENT_JOBS = 20

# ─────────────────────────────────────────────
# Grouping / aggregation columns
# ─────────────────────────────────────────────
GROUP_COLUMNS = [
    "department",
    "expense_type",
    "service_area",
    "supplier_category",
    "supplier_clean",
]

# ─────────────────────────────────────────────
# Polling behaviour  (Step 3)
# ─────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 300
CONSOLE_LOG_INTERVAL_SECONDS = 3600

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_DIR = "logs"

# ─────────────────────────────────────────────
# CSV output encoding  (all outputs)
# ─────────────────────────────────────────────
CSV_ENCODING = "utf-8-sig"

# ═════════════════════════════════════════════
# REMEDIATION / SCOPE-FLAGGING CONFIGURATION
# ═════════════════════════════════════════════

# ── Output / working files ───────────────────
TAXONOMY_EXTENSION_FILE = "taxonomy_extension.csv"   # sentinel codes
REMEDIATION_PROPOSALS_FILE = "remediation_proposals.csv"
REMEDIATION_CHANGELOG_FILE = "remediation_changelog.csv"
VENDOR_PIN_SUMMARY_FILE = "vendor_pin_summary.csv"

# ── Sentinel taxonomy codes (out-of-scope rows) ──────────────
# These are NOT in taxonomy.csv; they live in TAXONOMY_EXTENSION_FILE
# so joins still resolve. code: (Level 1, Level 2, Level 3, Clarification)
SENTINEL_CODES = {
    999001: ("Non-Procurement", "Statutory Transfer", "",
             "Precepts, NDR/Collection Fund, PAYE/NI, pensions — out of procurement scope"),
    999002: ("Non-Procurement", "Ledger / Control Account", "",
             "Balance-sheet, holding, suspense and control-account movements"),
    999003: ("Non-Procurement", "Income / Receipts", "",
             "Income received by the council — revenue, not spend"),
}
SENTINEL_STATUTORY = 999001
SENTINEL_LEDGER = 999002
SENTINEL_INCOME = 999003

# ── procurement_scope values ─────────────────
SCOPE_IN = "IN_SCOPE"
SCOPE_STATUTORY = "STATUTORY_TRANSFER"
SCOPE_LEDGER = "LEDGER_CONTROL"

# ── flow_type_taxonomy_key_aggregate values ──
FLOW_SPEND = "SPEND"
FLOW_REFUND = "REFUND"
FLOW_INCOME = "INCOME"
FLOW_REVERSAL = "ACCOUNTING_REVERSAL"
FLOW_COLUMN = "flow_type_taxonomy_key_aggregate"
SCOPE_COLUMN = "procurement_scope"

# ── Detection patterns (case-insensitive regex) ──────────────
# Payees that are tax authorities / precepting bodies / pension funds.
STATUTORY_PAYEE_REGEX = (
    r"hmrc|revenue & customs|inland revenue|greater london authority|"
    r"gla levies|ministry of housing|department for levelling up|"
    r"london councils|teachers pensions"
)
# Expense terms indicating statutory transfers (only decisive when the
# row also landed in Financial Services, or payee matches above).
STATUTORY_TERM_REGEX = (
    r"precept|collection fund|nndr|ndr income|paye|ni creditor|"
    r"business rates payable|council tax|share of|preceptor|"
    r"payment to gov dept|payment to other local|pension|voluntary deduction"
)
# Ledger / control-account movements (either sign).
LEDGER_TERM_REGEX = (
    r"ctrl or b/s|control account|balance sheet|holding account|"
    r"suspense|dd account|creditor control"
)
# Income / receipts (negative rows only).
INCOME_TERM_REGEX = (
    r"income|receipt|fees and charges|fairer charging|client contribution|"
    r"buy.?back|recharge|reimbursement|grant received|rents? received"
)
# Anchor rules.
ACCOMMODATION_REGEX = (
    r"nightly let|temporary accommodation|emergency accommodation|"
    r"bed and breakfast|nightly paid|private sector lease"
)
ACQUISITION_REGEX = (
    r"acquisition|property purchase|dwellings - acq|land purchase"
)
# Legitimate concessionary-travel rows that must NOT be caught as statutory.
CONCESSION_REGEX = r"concessionary|freedom pass"

# Anchor rule target codes (taxonomy.csv has no temporary-accommodation
# leaf, so both anchors land on Housing Management top level).
ANCHOR_ACCOMMODATION_TARGET = 250000   # Housing Management
ANCHOR_ACQUISITION_TARGET = 250000     # Housing Management

# ── Catch-all "magnet" categories ────────────
CATCHALL_LEVEL1 = {"Consultancy", "Financial Services"}

# ── Supplier-consensus reconciliation ────────
CONSENSUS_THRESHOLD = 0.6     # modal Level 1 share required to act
# Supplier names matching this regex are excluded from consensus logic.
REDACTED_SUPPLIER_REGEX = r"Redacted|Personal Information|\*"

# ── Vendor pins ──────────────────────────────
# Vendors whose service is ONE thing regardless of buying department.
# pattern (case-insensitive regex on supplier_clean) → taxonomy code.
# ONLY add single-service intermediaries here. Multi-service vendors
# (Capita, BT, Reed, Hays…) must stay OUT — consensus handles them.
# Evidence: modal code across audited slices, June 2026.
VENDOR_PINS = {
    r"matrix scm":   261400,   # 116/201 rows already 261400 (HR > Temp & Agency)
    r"comensura":    261400,   # 4/7 rows 261400
    r"adecco":       261400,   # 30/36 rows 261400
    r"pertemps":     261400,   # 10/11 rows 261400
}

# Warn in vendor_pin_summary if less than this share of a pinned
# vendor's rows already sit in the pin target's Level 1.
VENDOR_PIN_WARN_CONCENTRATION = 0.50
