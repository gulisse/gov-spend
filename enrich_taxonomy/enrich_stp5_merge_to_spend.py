#!/usr/bin/env python3
"""
enrich_stp5_merge_to_spend.py
─────────────────────────────
Merge tbl_taxonomy_base_enriched.csv back to the full granular
Normalized_spend.csv, applying taxonomy codes and all taxonomy
reference fields (Level 1 / 2 / 3, Clarification, resolution_status)
to every row.

Join keys:  department, expense_type, service_area,
            supplier_category, supplier_clean

Output
──────
Normalized_spend_enriched.csv  — same row count as the input spend
file, with enrichment columns appended.

Usage
─────
    python enrich_stp5_merge_to_spend.py
    python enrich_stp5_merge_to_spend.py --spend raw_data.csv --enriched custom_enriched.csv
"""

import argparse
import sys , os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
import pandas as pd

from config import (
    DEFAULT_SPEND_FILE,
    GROUP_COLUMNS,
    SPEND_ENRICHED_FILE,
    TAXONOMY_BASE_ENRICHED_FILE,
)
from utils import ScriptTimer, setup_logging


def parse_args():
    p = argparse.ArgumentParser(
        description="Merge enriched taxonomy base to granular spend data"
    )
    p.add_argument(
        "--spend", "-s", default=DEFAULT_SPEND_FILE,
        help=f"Normalized spend CSV  (default: {DEFAULT_SPEND_FILE})",
    )
    p.add_argument(
        "--enriched", "-e", default=TAXONOMY_BASE_ENRICHED_FILE,
        help=f"Enriched taxonomy base CSV  (default: {TAXONOMY_BASE_ENRICHED_FILE})",
    )
    p.add_argument(
        "--output", "-o", default=SPEND_ENRICHED_FILE,
        help=f"Output file  (default: {SPEND_ENRICHED_FILE})",
    )
    return p.parse_args()


def main():
    args = parse_args()

    logger = setup_logging("stp5_merge_to_spend")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp5_merge_to_spend.py")

    # ── Load spend data ──────────────────────────────────
    logger.info(f"Reading spend data: {args.spend}")
    spend_df = pd.read_csv(args.spend, low_memory=False)
    logger.info(f"Spend: {len(spend_df):,} rows × {len(spend_df.columns)} cols")

    # ── Load enriched base ───────────────────────────────
    logger.info(f"Reading enriched base: {args.enriched}")
    enriched_df = pd.read_csv(args.enriched, low_memory=False)
    logger.info(f"Enriched base: {len(enriched_df):,} rows × {len(enriched_df.columns)} cols")

    # ── Validate join columns exist in both ──────────────
    for col in GROUP_COLUMNS:
        if col not in spend_df.columns:
            logger.error(f"Column '{col}' missing from spend file")
            sys.exit(1)
        if col not in enriched_df.columns:
            logger.error(f"Column '{col}' missing from enriched base")
            sys.exit(1)
    logger.info(f"Join columns validated: {GROUP_COLUMNS}")

    # ── Select enrichment columns ────────────────────────
    #    Exclude the grouping columns (already in spend)
    #    and the aggregation metrics (row_count, total_amount, etc.)
    #    which belong to the base, not the granular spend.
    aggregate_metrics = {
        "rq_id", "row_count", "total_amount",
        "min_amount", "max_amount", "avg_amount", "median_amount",
    }
    group_set = set(GROUP_COLUMNS)

    enrich_cols = [
        c for c in enriched_df.columns
        if c not in group_set and c not in aggregate_metrics
    ]
    logger.info(f"Enrichment columns: {enrich_cols}")

    # Build the lookup: grouping columns + enrichment columns only
    lookup_df = enriched_df[GROUP_COLUMNS + enrich_cols].copy()
    lookup_df = lookup_df.drop_duplicates(subset=GROUP_COLUMNS)
    logger.info(f"Lookup rows (deduplicated): {len(lookup_df):,}")

    # ── Merge ────────────────────────────────────────────
    logger.info("Merging …")
    result = spend_df.merge(lookup_df, on=GROUP_COLUMNS, how="left")

    # Sanity: row count should match input spend
    if len(result) != len(spend_df):
        logger.warning(
            f"Row count changed after merge: {len(spend_df):,} → {len(result):,}.  "
            "This may indicate duplicate grouping keys in the enriched base."
        )

    # Match rate
    code_col = "taxonomy_code" if "taxonomy_code" in result.columns else None
    if code_col:
        matched = result[code_col].notna().sum()
        logger.info(
            f"Matched: {matched:,} / {len(result):,}  "
            f"({matched / len(result) * 100:.1f}%)"
        )

    # ── Write output ─────────────────────────────────────
    result.to_csv(args.output, index=False)
    logger.info(
        f"Written: {args.output}  "
        f"({len(result):,} rows × {len(result.columns)} cols)"
    )

    timer.end()


if __name__ == "__main__":
    main()
