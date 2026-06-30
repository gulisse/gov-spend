#!/usr/bin/env python3
"""
enrich_stp4_merge_batch_results.py
──────────────────────────────────
Join the Gemini batch API results back to the taxonomy base dataset,
then join taxonomy.csv to bring across Level 1 / 2 / 3 and
Clarification information.

Input
─────
• batch_results/*.jsonl   — downloaded Gemini output  (from Step 3)
• tbl_taxonomy_base.csv   — aggregated lookup          (from Step 1)
• taxonomy.csv            — 597-row classification tree

Output
──────
• tbl_taxonomy_base_enriched.csv

Usage
─────
    python enrich_stp4_merge_batch_results.py
    python enrich_stp4_merge_batch_results.py --results batch_results/batch_results_001.jsonl
"""

import argparse
import glob
import json
import os
import sys
# ── Requires the pyrightconfig.json in the vs code root for config and utils to import 
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import pandas as pd

from config import (
    BATCH_RESULTS_DIR,
    TAXONOMY_BASE_ENRICHED_FILE,
    TAXONOMY_BASE_FILE,
    TAXONOMY_FILE,
)
from utils import ScriptTimer, setup_logging


def parse_args():
    p = argparse.ArgumentParser(
        description="Merge batch results → taxonomy base → taxonomy reference"
    )
    p.add_argument(
        "--results", "-r", nargs="+", default=None,
        help=(
            "One or more batch result JSONL files  "
            f"(default: {BATCH_RESULTS_DIR}/batch_results_*.jsonl)"
        ),
    )
    p.add_argument(
        "--taxonomy-base", "-t", default=TAXONOMY_BASE_FILE,
        help=f"Taxonomy base CSV  (default: {TAXONOMY_BASE_FILE})",
    )
    p.add_argument(
        "--taxonomy", default=TAXONOMY_FILE,
        help=f"Taxonomy reference CSV  (default: {TAXONOMY_FILE})",
    )
    p.add_argument(
        "--output", "-o", default=TAXONOMY_BASE_ENRICHED_FILE,
        help=f"Output file  (default: {TAXONOMY_BASE_ENRICHED_FILE})",
    )
    return p.parse_args()


# ──────────────────────────────────────────────
# Parse batch JSONL
# ──────────────────────────────────────────────

def parse_result_file(path: str, logger) -> list[dict]:
    """
    Parse a single Gemini output JSONL into a list of flat dicts:
        {rq_id, taxonomy_code, resolution_status}
    """
    records = []
    errors = 0

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            try:
                obj = json.loads(line)

                # The 'key' field echoes our RQ_ID
                rq_id = obj.get("key")

                # Navigate response → candidates → content → parts → text
                parts = (
                    obj.get("response", {})
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )

                parsed = None
                for part in parts:
                    if "text" in part:
                        parsed = json.loads(part["text"])
                        break

                if parsed is None:
                    errors += 1
                    logger.debug(f"{path} line {line_no}: no parseable text part")
                    continue

                records.append({
                    "rq_id": rq_id,
                    "taxonomy_code": parsed.get("TAXONOMY_CODE"),
                    "resolution_status": parsed.get("RESOLUTION_STATUS"),
                })

            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                errors += 1
                logger.debug(f"{path} line {line_no}: {exc}")

    logger.info(f"  {path}: {len(records):,} parsed, {errors} errors")
    return records


def load_all_results(paths: list[str], logger) -> pd.DataFrame:
    """Parse and concatenate multiple result files."""
    all_records: list[dict] = []
    for p in paths:
        all_records.extend(parse_result_file(p, logger))

    df = pd.DataFrame(all_records)
    logger.info(f"Total parsed results: {len(df):,} rows")
    return df


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    args = parse_args()

    logger = setup_logging("stp4_merge_batch_results")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp4_merge_batch_results.py")

    # ── Resolve result files ─────────────────────────────
    if args.results:
        result_files = args.results
    else:
        pattern = os.path.join(BATCH_RESULTS_DIR, "batch_results_*.jsonl")
        result_files = sorted(glob.glob(pattern))

    if not result_files:
        logger.error("No result files found.  Run Step 3 first.")
        sys.exit(1)

    logger.info(f"Result files ({len(result_files)}):")
    for f in result_files:
        logger.info(f"  • {f}")

    # ── Load data ────────────────────────────────────────
    results_df = load_all_results(result_files, logger)

    logger.info(f"Reading taxonomy base: {args.taxonomy_base}")
    base_df = pd.read_csv(args.taxonomy_base, low_memory=False)
    logger.info(f"Taxonomy base: {len(base_df):,} rows × {len(base_df.columns)} cols")

    logger.info(f"Reading taxonomy reference: {args.taxonomy}")
    taxonomy_df = pd.read_csv(args.taxonomy, low_memory=False)
    taxonomy_df.columns = taxonomy_df.columns.str.strip()          # trim whitespace
    logger.info(f"Taxonomy reference: {len(taxonomy_df):,} codes")

    # ── NEC fallback for invalid taxonomy codes ──────
    valid_codes = set(pd.to_numeric(taxonomy_df["ID"], errors="coerce").dropna())

    def find_nec_fallback(bad_code):
        """Progressive truncation to find the most specific NEC code.
        361300 → try 361399? no → 361999? yes → use it.
        261611 → try 261699? yes → use it.
        """
        code_str = str(int(bad_code)).zfill(6)
        for prefix_len in [4, 3, 2, 1]:
            candidate = int(code_str[:prefix_len] + "9" * (6 - prefix_len))
            if candidate in valid_codes:
                return candidate
        return None

    if not results_df.empty and "taxonomy_code" in results_df.columns:
        results_df["taxonomy_code"] = pd.to_numeric(
            results_df["taxonomy_code"], errors="coerce"
        )
        invalid_mask = (
            results_df["taxonomy_code"].notna()
            & ~results_df["taxonomy_code"].isin(valid_codes)
        )
        invalid_count = invalid_mask.sum()

        if invalid_count > 0:
            logger.info(
                f"NEC fallback: {invalid_count:,} rows have "
                f"invalid taxonomy codes — remapping …"
            )
            remapped = 0
            for idx in results_df[invalid_mask].index:
                bad_code = results_df.loc[idx, "taxonomy_code"]
                nec = find_nec_fallback(bad_code)
                if nec is not None:
                    logger.info(
                        f"  {results_df.loc[idx, 'rq_id']}: "
                        f"{int(bad_code)} → {int(nec)} (NEC fallback)" # type: ignore
                    )
                    results_df.loc[idx, "taxonomy_code"] = nec
                    results_df.loc[idx, "resolution_status"] = "NEC_FALLBACK"
                    remapped += 1
                else:
                    logger.warning(
                        f"  {results_df.loc[idx, 'rq_id']}: "
                        f"{int(bad_code)} — no NEC fallback found" # type: ignore
                    )
            logger.info(f"NEC fallback: remapped {remapped:,} / {invalid_count:,}")

    # ── Merge results → base on rq_id ───────────────────
    logger.info("Merging batch results to taxonomy base on rq_id …")
    merged = base_df.merge(results_df, on="rq_id", how="left")

    matched = merged["taxonomy_code"].notna().sum()
    logger.info(
        f"Matched {matched:,} / {len(merged):,}  "
        f"({matched / len(merged) * 100:.1f}%)"
    )

    # ── Join taxonomy reference on taxonomy_code = ID ────
    taxonomy_df = taxonomy_df.rename(columns={"ID": "taxonomy_code"})
    merged["taxonomy_code"] = pd.to_numeric(merged["taxonomy_code"], errors="coerce")
    taxonomy_df["taxonomy_code"] = pd.to_numeric(
        taxonomy_df["taxonomy_code"], errors="coerce"
    )

    taxonomy_join_cols = [c for c in taxonomy_df.columns if c != "taxonomy_code"]
    logger.info(f"Joining taxonomy fields: {taxonomy_join_cols}")

    enriched = merged.merge(taxonomy_df, on="taxonomy_code", how="left")

    # ── Write output ─────────────────────────────────────
    enriched.to_csv(args.output, index=False)
    logger.info(
        f"Written: {args.output}  "
        f"({len(enriched):,} rows × {len(enriched.columns)} cols)"
    )

    # Quick quality check
    unmatched_tax = enriched["Level 1"].isna().sum() if "Level 1" in enriched.columns else -1
    if unmatched_tax > 0:
        logger.warning(
            f"{unmatched_tax:,} rows have a taxonomy_code that did not match "
            "any ID in taxonomy.csv — review for unmapped codes."
        )

    timer.end()


if __name__ == "__main__":
    main()
