#!/usr/bin/env python3
"""
enrich_stp2_create_batch_source_files.py
────────────────────────────────────────
Build JSONL template files for the Gemini Batch API.

Each JSONL row contains the prompt and response schema but does NOT
include systemInstruction — that is injected by Step 3 at submission
time so the template files stay small and reusable across runs.

Blank / NaN field values are omitted from every prompt.

When the source file exceeds BATCH_SIZE rows the output is split
into sequentially numbered files (batch_input_001.jsonl, …).

Usage
─────
    python enrich_stp2_create_batch_source_files.py
    python enrich_stp2_create_batch_source_files.py --input custom_base.csv
    python enrich_stp2_create_batch_source_files.py --batch-size 5000
"""

import argparse
import json
import math
import os
import re
import sys

import pandas as pd
# ── Requires the pyrightconfig.json in the vs code root for config and utils to import 
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

from config import (
    BATCH_OUTPUT_DIR,
    BATCH_SIZE,
    MODEL_CANDIDATE_COUNT,
    MODEL_MAX_OUTPUT_TOKENS,
    MODEL_TEMPERATURE,
    MODEL_TOP_K,
    MODEL_TOP_P,
    TAXONOMY_BASE_FILE,
)
from utils import ScriptTimer, setup_logging

# ──────────────────────────────────────────────
# Response schema sent with every request so
# Gemini returns structured JSON we can parse
# deterministically in Step 4.
# ──────────────────────────────────────────────
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "TAXONOMY_CODE": {
            "type": "INTEGER",
            "description": "The taxonomy ID from Column A of the reference table.",
        },
        "RESOLUTION_STATUS": {
            "type": "STRING",
            "description": (
                "Return 'CONFIDENT' if resolved via Rules 1-5, "
                "or 'AMBIGUOUS' if defaulted via Rule 6."
            ),
        },
    },
    "required": ["TAXONOMY_CODE", "RESOLUTION_STATUS"],
}

PROMPT_PREAMBLE = (
    "Analyze the aggregate council spend record below. "
    "Apply the PRIORITY RULES provided in the system instructions "
    "to resolve any mapping ambiguities. "
    "Determine the single most accurate taxonomy code from Column A "
    "of the reference table.\n\n"
    "Input Record:\n"
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Create batch JSONL files for Gemini API"
    )
    p.add_argument(
        "--input", "-i",
        default=TAXONOMY_BASE_FILE,
        help=f"Input CSV file (default: {TAXONOMY_BASE_FILE})",
    )
    p.add_argument(
        "--output-dir", "-o",
        default=BATCH_OUTPUT_DIR,
        help=f"Output directory for JSONL files (default: {BATCH_OUTPUT_DIR})",
    )
    p.add_argument(
        "--batch-size", "-b",
        type=int,
        default=BATCH_SIZE,
        help=f"Max rows per JSONL file (default: {BATCH_SIZE})",
    )
    return p.parse_args()


def build_record_block(row: pd.Series) -> str:
    """
    Format every non-empty, non-NaN field as  header: value
    one per line. The rq_id is excluded here because it is
    handled separately in the prompt structure.

    Leading council reference codes (e.g. "361300 PUBLIC TRANSPORT")
    are stripped from string values so the model classifies on the
    descriptive text, not on embedded numeric codes.
    """
    lines = []
    for col, val in row.items():
        if col == "rq_id":
            continue
        if pd.isna(val):
            continue
        if isinstance(val, str):
            val = val.strip()
            if val == "":
                continue
            # Strip leading council reference codes:
            #   "361300 PUBLIC TRANSPORT" → "PUBLIC TRANSPORT"
            #   "D0831 HOMES FOR UKRAINE" → "HOMES FOR UKRAINE"
            val = re.sub(r"^[A-Za-z]?\d{4,}\s+", "", val)
            if val == "":
                continue
        lines.append(f"{col}: {val}")
    return "\n".join(lines)


def build_jsonl_row(row: pd.Series) -> dict:
    """Construct a single JSONL request object (without systemInstruction)."""
    rq_id = str(row["rq_id"])
    record_block = build_record_block(row)
    prompt_text = f"{PROMPT_PREAMBLE}{record_block}"

    return {
        "key": rq_id,
        "request": {
            "contents": [
                {"role": "user", "parts": [{"text": prompt_text}]}
            ],
            # systemInstruction is injected by Step 3 at submission time
            "generationConfig": {
                "temperature": MODEL_TEMPERATURE,
                "topK": MODEL_TOP_K,
                "topP": MODEL_TOP_P,
                "maxOutputTokens": MODEL_MAX_OUTPUT_TOKENS,
                "candidateCount": MODEL_CANDIDATE_COUNT,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        },
    }


def main():
    args = parse_args()

    logger = setup_logging("stp2_create_batch_files")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp2_create_batch_source_files.py")

    # ── Read input ───────────────────────────────────────
    logger.info(f"Reading: {args.input}")
    df = pd.read_csv(args.input, low_memory=False)
    logger.info(f"Loaded {len(df):,} rows × {len(df.columns)} columns")

    if "rq_id" not in df.columns:
        logger.error("Column 'rq_id' not found — run Step 1 first.")
        sys.exit(1)

    # ── Prepare output directory ─────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    total_rows = len(df)
    num_files = max(1, math.ceil(total_rows / args.batch_size))

    logger.info(f"Batch size : {args.batch_size:,}")
    logger.info(f"Total rows : {total_rows:,}")
    logger.info(f"Files to create: {num_files}")

    # ── Write JSONL file(s) ──────────────────────────────
    rows_written = 0
    skipped_empty = 0

    for file_idx in range(num_files):
        start = file_idx * args.batch_size
        end = min(start + args.batch_size, total_rows)
        chunk = df.iloc[start:end]

        filename = (
            "batch_input.jsonl"
            if num_files == 1
            else f"batch_input_{file_idx + 1:03d}.jsonl"
        )
        filepath = os.path.join(args.output_dir, filename)

        file_count = 0
        with open(filepath, "w", encoding="utf-8") as fh:
            for _, row in chunk.iterrows():
                record_block = build_record_block(row)
                if not record_block.strip():
                    skipped_empty += 1
                    continue
                obj = build_jsonl_row(row)
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                file_count += 1

        rows_written += file_count
        logger.info(
            f"  {filepath}: {file_count:,} rows  "
            f"(cumulative {rows_written:,})"
        )

    if skipped_empty:
        logger.warning(f"Skipped {skipped_empty:,} rows with no usable field values")

    logger.info(f"All JSONL templates written to: {args.output_dir}/")
    timer.end()


if __name__ == "__main__":
    main()
