#!/usr/bin/env python3
"""
enrich_stp0_flag_scope.py
─────────────────────────
Add procurement_scope and flow_type_taxonomy_key_aggregate columns to a
taxonomy base file. Flag, don't filter: every row is retained.

Go-forward pipeline position: run AFTER Step 1 (so the flags exist on
tbl_taxonomy_base.csv before Step 2 builds JSONL). Step 2 then excludes
rows where procurement_scope != IN_SCOPE from the batch API run; they
can be rejoined afterwards carrying their flags.

Also drops fully-blank rows (logged, with original positions).

Usage
─────
    python enrich_stp0_flag_scope.py --input tbl_taxonomy_base.csv
    python enrich_stp0_flag_scope.py --input my_base.csv --output my_base_flagged.csv
"""

import argparse
import sys

import pandas as pd

from config import (
    CSV_ENCODING,
    FLOW_COLUMN,
    SCOPE_COLUMN,
    SCOPE_IN,
    TAXONOMY_BASE_FILE,
)
from enrich_rules import blank_row_mask, compute_flow_type, compute_scope
from utils import ScriptTimer, setup_logging


def parse_args():
    p = argparse.ArgumentParser(description="Flag scope + flow type on a taxonomy base")
    p.add_argument("--input", "-i", default=TAXONOMY_BASE_FILE,
                   help=f"Input base CSV (default: {TAXONOMY_BASE_FILE})")
    p.add_argument("--output", "-o", default=None,
                   help="Output file (default: <input stem>_flagged.csv)")
    return p.parse_args()


def main():
    args = parse_args()
    logger = setup_logging("stp0_flag_scope")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp0_flag_scope.py")

    output = args.output or args.input.rsplit(".", 1)[0] + "_flagged.csv"

    logger.info(f"Reading: {args.input}")
    df = pd.read_csv(args.input, low_memory=False, encoding="utf-8-sig")
    n_in = len(df)
    logger.info(f"Loaded {n_in:,} rows × {len(df.columns)} columns")

    # ── Drop fully-blank rows ────────────────────────────
    blanks = blank_row_mask(df)
    if blanks.any():
        positions = (df.index[blanks] + 2).tolist()   # +2 = header + 1-based
        logger.warning(
            f"Removing {int(blanks.sum())} fully-blank row(s) "
            f"at original file line(s): {positions}"
        )
        df = df[~blanks].copy()

    # ── Compute flags ────────────────────────────────────
    scope = compute_scope(df)
    flow = compute_flow_type(df, scope)
    df[SCOPE_COLUMN] = scope
    df[FLOW_COLUMN] = flow

    for col in (SCOPE_COLUMN, FLOW_COLUMN):
        logger.info(f"{col}:")
        for val, n in df[col].value_counts().items():
            amt = df.loc[df[col] == val, "total_amount"].sum() if "total_amount" in df.columns else 0
            logger.info(f"  {val:<22} {n:>8,} rows   £{amt/1e6:>10,.1f}m")

    out_of_scope = (df[SCOPE_COLUMN] != SCOPE_IN).sum()
    logger.info(f"Rows Step 2 will exclude from the batch run: {out_of_scope:,}")

    # ── Assertions & write ───────────────────────────────
    assert len(df) == n_in - int(blanks.sum()), "Row-count integrity check failed"
    df.to_csv(output, index=False, encoding=CSV_ENCODING)
    logger.info(f"Written: {output}  ({len(df):,} rows × {len(df.columns)} cols)")

    timer.end()


if __name__ == "__main__":
    main()
