#!/usr/bin/env python3
"""
bt_stp2_create_batch_files.py   (business-type batch — Step 2 / "C1")
─────────────────────────────────────────────────────────────────────
Build Gemini Batch API JSONL request files that classify suppliers into the
two-level business taxonomy.

Key design points
─────────────────
• Output field is a SINGLE `BUSINESS_SUBTYPE`, constrained by a `responseSchema`
  **enum** of all 85 sub-types — the model is structurally unable to emit an
  invalid value, so no NEC-style fallback is needed. The parent BUSINESS_TYPE is
  derived deterministically from the sub-type in Step 4.
• The reference document (taxonomy + the 5 rules) is embedded as
  `systemInstruction` in every row, so each JSONL is submit-ready and decoupled
  from the procurement pipeline. Vertex implicit caching dedupes the identical
  prefix across rows.
• Only rows flagged `include_in_batch = Y` with a blank `override_business_type`
  are sent. Hand-overridden rows skip the model and are applied in Step 4.
• Reconciliation uses a generated `supplier_id` echoed via the batch `key`
  field; a sidecar keymap maps it back to `supplier_clean`.

Input
─────
• distinct_suppliers_review_updated.csv   (the reviewed file, saved as CSV)
• supplier_context.csv            (from bt_stp1_build_supplier_context.py)
• business_type_taxonomy.csv      (16 types / 85 sub-types)
• business_rules.md               (the 5 rules)

Output
──────
• batch_input_bt/batch_input_bt.jsonl[ _NNN ]   (submit-ready)
• batch_input_bt/bt_keymap.csv                  (supplier_id ↔ supplier_clean)

Usage
─────
    python bt_stp2_create_batch_files.py
    python bt_stp2_create_batch_files.py --input distinct_suppliers_review_updated.csv \
        --context supplier_context.csv --batch-size 10000
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import OrderedDict
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

# Reuse model/generation + batch settings from the existing pipeline config.
from config import (
    BATCH_SIZE,
    MODEL_CANDIDATE_COUNT,
    MODEL_MAX_OUTPUT_TOKENS,
    MODEL_TEMPERATURE,
    MODEL_TOP_K,
    MODEL_TOP_P,
)

PROMPT_PREAMBLE = (
    "Classify the supplier below into the single most accurate BUSINESS_SUBTYPE "
    "from the controlled vocabulary in the system instructions. Apply the "
    "classification rules. Use the context fields to disambiguate the name.\n\n"
)


def parse_args():
    p = argparse.ArgumentParser(description="Build business-type batch JSONL")
    p.add_argument("--input", "-i", default="distinct_suppliers_review_updated.csv",
                   help="Reviewed suppliers CSV (default: distinct_suppliers_review_updated.csv)")
    p.add_argument("--context", "-c", default="supplier_context.csv",
                   help="Supplier context CSV (default: supplier_context.csv)")
    p.add_argument("--taxonomy", "-t", default="business_type_taxonomy.csv",
                   help="Business taxonomy CSV (default: business_type_taxonomy.csv)")
    p.add_argument("--rules", "-r", default="business_rules.md",
                   help="Business rules MD (default: business_rules.md)")
    p.add_argument("--output-dir", "-o", default="batch_input_bt",
                   help="Output directory (default: batch_input_bt)")
    p.add_argument("--batch-size", "-b", type=int, default=BATCH_SIZE,
                   help=f"Max rows per JSONL file (default: {BATCH_SIZE})")
    p.add_argument("--include-col", default="include_in_batch",
                   help="Column whose value 'Y' selects a row (default: include_in_batch)")
    p.add_argument("--override-col", default="override_business_type",
                   help="A non-blank value here excludes the row (default: override_business_type)")
    return p.parse_args()


def load_taxonomy(path):
    """Return (subtypes_list, type_by_subtype) preserving file order."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    subs, type_by = [], OrderedDict()
    for _, r in df.iterrows():
        st = str(r["business_subtype"]).strip()
        subs.append(st)
        type_by[st] = str(r["business_type"]).strip()
    return subs, type_by


def build_reference_document(taxonomy_path, rules_path, subtypes, type_by):
    """Compose the systemInstruction: taxonomy structure + the 5 rules."""
    with open(rules_path, encoding="utf-8") as f:
        rules_text = f.read()

    lines = ["# BUSINESS CLASSIFICATION REFERENCE\n",
             "## CONTROLLED VOCABULARY",
             "Assign exactly ONE BUSINESS_SUBTYPE from the list below. "
             "The parent type is shown only for context — return the sub-type.\n"]
    # group subtypes under their type, in order
    seen = []
    for st in subtypes:
        t = type_by[st]
        if t not in seen:
            seen.append(t)
            lines.append(f"\n### {t}")
        lines.append(f"- {st}")
    body = "\n".join(lines)

    return (
        f"{body}\n\n"
        "## CLASSIFICATION RULES\n\n"
        f"{rules_text}\n\n"
        "## OUTPUT\n"
        "Return the single best-fitting BUSINESS_SUBTYPE exactly as written "
        "above, and STATUS = CONFIDENT, or AMBIGUOUS if you fell back to an "
        "'Other' sub-type.\n"
    )


def response_schema(subtypes):
    return {
        "type": "OBJECT",
        "properties": {
            "BUSINESS_SUBTYPE": {
                "type": "STRING",
                "enum": subtypes,
                "description": "Single best-fitting sub-type from the vocabulary.",
            },
            "STATUS": {
                "type": "STRING",
                "enum": ["CONFIDENT", "AMBIGUOUS"],
                "description": "CONFIDENT, or AMBIGUOUS if an 'Other' default was used.",
            },
        },
        "required": ["BUSINESS_SUBTYPE", "STATUS"],
    }


def build_prompt(name, context_descriptor):
    block = f"Supplier name: {name}"
    if isinstance(context_descriptor, str) and context_descriptor.strip():
        block += f"\n{context_descriptor.strip()}"
    return f"{PROMPT_PREAMBLE}{block}"


def build_row(supplier_id, name, context_descriptor, reference_doc, schema):
    return {
        "key": supplier_id,
        "request": {
            "systemInstruction": {"parts": [{"text": reference_doc}]},
            "contents": [
                {"role": "user",
                 "parts": [{"text": build_prompt(name, context_descriptor)}]}
            ],
            "generationConfig": {
                "temperature": MODEL_TEMPERATURE,
                "topK": MODEL_TOP_K,
                "topP": MODEL_TOP_P,
                "maxOutputTokens": MODEL_MAX_OUTPUT_TOKENS,
                "candidateCount": MODEL_CANDIDATE_COUNT,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        },
    }


def main():
    args = parse_args()

    subtypes, type_by = load_taxonomy(args.taxonomy)
    reference_doc = build_reference_document(args.taxonomy, args.rules, subtypes, type_by)
    schema = response_schema(subtypes)
    print(f"Taxonomy: {len(subtypes)} sub-types | reference doc {len(reference_doc):,} chars")

    df = pd.read_csv(args.input, low_memory=False, encoding="utf-8-sig")
    if args.include_col not in df.columns:
        sys.exit(f"Column '{args.include_col}' not found in {args.input}")

    inc = df[args.include_col].fillna("").astype(str).str.strip().str.upper() == "Y"
    if args.override_col in df.columns:
        ov = df[args.override_col].fillna("").astype(str).str.strip()
        inc &= ov.eq("") | ov.str.lower().eq("nan")
    sel = df[inc].copy()
    print(f"Selected {len(sel):,} of {len(df):,} rows for the batch")

    if sel.empty:
        sys.exit("Nothing to send — no rows with include_in_batch = Y.")

    # join context
    if os.path.exists(args.context):
        ctx = pd.read_csv(args.context, low_memory=False, encoding="utf-8-sig")
        ctx = ctx[["supplier_clean", "context_descriptor"]]
        sel = sel.merge(ctx, on="supplier_clean", how="left")
    else:
        print(f"  (no context file at {args.context} — name-only prompts)")
        sel["context_descriptor"] = ""

    sel = sel.reset_index(drop=True)
    sel["supplier_id"] = [f"SUP_{i+1}" for i in range(len(sel))]

    os.makedirs(args.output_dir, exist_ok=True)

    # keymap sidecar
    keymap_path = os.path.join(args.output_dir, "bt_keymap.csv")
    sel[["supplier_id", "supplier_clean"]].to_csv(
        keymap_path, index=False, encoding="utf-8-sig")

    total = len(sel)
    nfiles = max(1, math.ceil(total / args.batch_size))
    written = 0
    for fi in range(nfiles):
        s, e = fi * args.batch_size, min((fi + 1) * args.batch_size, total)
        chunk = sel.iloc[s:e]
        fname = ("batch_input_bt.jsonl" if nfiles == 1
                 else f"batch_input_bt_{fi+1:03d}.jsonl")
        fpath = os.path.join(args.output_dir, fname)
        with open(fpath, "w", encoding="utf-8") as fh:
            for _, r in chunk.iterrows():
                row = build_row(r["supplier_id"], r["supplier_clean"],
                                r.get("context_descriptor", ""), reference_doc, schema)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += len(chunk)
        print(f"  {fpath}: {len(chunk):,} rows")

    print(f"Done. {written:,} rows across {nfiles} file(s).")
    print(f"Keymap → {keymap_path}")
    print("These JSONL already contain systemInstruction — submit as-is "
          "(do NOT re-inject the procurement reference doc).")


if __name__ == "__main__":
    main()
