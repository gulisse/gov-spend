#!/usr/bin/env python3
"""
enrich_stp1_create_taxonomy_base.py
────────────────────────────────────
Create a taxonomy base lookup dataset with unique RQ_IDs.

Modes
─────
DEFAULT  (no --aggregate flag):
    Read the input CSV as-is, add an RQ_ID column, write output.

AGGREGATE (--aggregate):
    Group by the five key columns, compute metrics
    (row_count, total_amount, min/max/avg/median_amount),
    add RQ_ID, write output.

RQ_ID assignment:
    Ordered by total_amount DESC when that column exists,
    otherwise by existing row order.

Output naming:
    • Default input  → tbl_taxonomy_base.csv
    • Custom input   → <filename>_base.csv   (e.g. taxis.csv → taxis_base.csv)
    • --output flag  → whatever you specify

Usage
─────
    python enrich_stp1_create_taxonomy_base.py
    python enrich_stp1_create_taxonomy_base.py --aggregate
    python enrich_stp1_create_taxonomy_base.py --input my_data.csv --aggregate
    python enrich_stp1_create_taxonomy_base.py --input my_data.csv --output custom_out.csv
"""

import argparse
import numpy as np
import pandas as pd

# ── Requires the pyrightconfig.json in the vs code root for config and utils to import 
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from config import DEFAULT_SPEND_FILE, GROUP_COLUMNS, TAXONOMY_BASE_FILE
from utils import ScriptTimer, setup_logging


def parse_args():
    p = argparse.ArgumentParser(
        description="Create taxonomy base with RQ_IDs"
    )
    p.add_argument(
        "--input", "-i",
        default=DEFAULT_SPEND_FILE,
        help=f"Input CSV file (default: {DEFAULT_SPEND_FILE})",
    )
    p.add_argument(
        "--aggregate", "-a",
        action="store_true",
        help="Aggregate by grouping columns and compute metrics",
    )
    p.add_argument(
        "--output", "-o",
        default=None,
        help="Output file path (default: auto-generated)",
    )
    return p.parse_args()


def validate_columns(df: pd.DataFrame, required: list[str], logger) -> None:
    """Exit with a clear message if any required columns are missing."""
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.error(f"Missing required columns: {missing}")
        logger.error(f"Available columns: {list(df.columns)}")
        sys.exit(1)
    logger.info(f"Validated columns: {required}")


def main():
    args = parse_args()

    logger = setup_logging("stp1_create_taxonomy_base")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp1_create_taxonomy_base.py")

    # ── Resolve output path ──────────────────────────────
    if args.output:
        output_file = args.output
    elif args.input != DEFAULT_SPEND_FILE:
        base, ext = os.path.splitext(args.input)
        output_file = f"{base}_base{ext}"
    else:
        output_file = TAXONOMY_BASE_FILE

    # ── Read input ───────────────────────────────────────
    logger.info(f"Reading: {args.input}")
    df = pd.read_csv(args.input, low_memory=False)
    logger.info(f"Loaded {len(df):,} rows × {len(df.columns)} columns")
    logger.info(f"Columns: {list(df.columns)}")

    # ── Aggregate or pass-through ────────────────────────
    if args.aggregate:
        logger.info("MODE: Aggregation")

        validate_columns(df, GROUP_COLUMNS, logger)
        if "amount" not in df.columns:
            logger.error("Column 'amount' is required for aggregation but was not found.")
            sys.exit(1)

        logger.info(f"Grouping by: {GROUP_COLUMNS}")

        df_out = (
            df.groupby(GROUP_COLUMNS, dropna=False)
            .agg(
                row_count=("amount", "count"),
                total_amount=("amount", "sum"),
                min_amount=("amount", "min"),
                max_amount=("amount", "max"),
                avg_amount=("amount", "mean"),
                median_amount=("amount", "median"),
            )
            .reset_index()
        )

        # Round financial metrics
        for col in ["total_amount", "min_amount", "max_amount", "avg_amount", "median_amount"]:
            df_out[col] = df_out[col].round(2)

        logger.info(f"Aggregation produced {len(df_out):,} unique groups")
    else:
        logger.info("MODE: Pass-through (no aggregation)")
        df_out = df.copy()

    # ── Assign RQ_ID ─────────────────────────────────────
    if "total_amount" in df_out.columns:
        logger.info("Ordering by total_amount DESC for RQ_ID assignment")
        df_out = df_out.sort_values("total_amount", ascending=False).reset_index(drop=True)
    else:
        logger.info("total_amount not present — using existing row order for RQ_ID")
        df_out = df_out.reset_index(drop=True)

    df_out.insert(0, "rq_id", [f"RQ_{i + 1}" for i in range(len(df_out))])

    # ── Write output ─────────────────────────────────────
    df_out.to_csv(output_file, index=False)
    logger.info(f"Written: {output_file}  ({len(df_out):,} rows × {len(df_out.columns)} cols)")

    timer.end()


if __name__ == "__main__":
    main()
