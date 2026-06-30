#!/usr/bin/env python3
"""
bt_stp1c_ch_disambig.py   (Companies House enrichment — Stage 1, Step 2)
─────────────────────────────────────────────────────────────────────────
Build the Gemini Batch API JSONL that asks the model to pick which Companies
House candidate is the SAME LEGAL ENTITY as each supplier (or NONE).

Input
─────
• ch_candidates.csv        (from bt_stp1b_ch_match.py — each miss → ≤K candidates)
• supplier_context.csv     (optional — adds the council context line per supplier)

Design
──────
• One request per supplier (groups its ≤K candidates).
• `responseSchema` is PER ROW: an enum of THAT supplier's candidate
  `company_number`s plus "NONE", so the model is structurally unable to invent a
  number — and it never emits a SIC (we read SIC from the CH file in Step 4).
• The matching rules live in `systemInstruction` (identical across rows → implicit
  caching dedupes the prefix). No taxonomy doc → tiny prompt.
• Reconciliation via a generated `supplier_id` echoed in the batch `key`; a sidecar
  ch_keymap.csv maps it back to supplier_clean.

Output
──────
• batch_input_ch/batch_input_ch[_NNN].jsonl   (submit-ready)
• batch_input_ch/ch_keymap.csv                (supplier_id ↔ supplier_clean)

Usage
─────
    python bt_stp1c_ch_disambig.py
    python bt_stp1c_ch_disambig.py --candidates ch_candidates.csv \
        --context supplier_context.csv --batch-size 10000
"""

import argparse
import json
import math
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import pandas as pd

from config import (
    BATCH_SIZE,
    MODEL_CANDIDATE_COUNT,
    MODEL_TEMPERATURE,
    MODEL_TOP_K,
    MODEL_TOP_P,
)

# disambiguation answers are tiny (a number + a word)
MAX_OUTPUT_TOKENS = 64

SYSTEM_INSTRUCTION = (
    "You match a UK council supplier to its Companies House record.\n\n"
    "You are given the supplier name, optional context about what the council bought "
    "from it, and a short list of candidate Companies House companies. Choose the "
    "candidate company_number that is the SAME LEGAL ENTITY as the supplier.\n\n"
    "Judge primarily on the NAME — allow for abbreviations, acronyms, trading names, "
    "punctuation and word-order differences. Prefer an 'active' company over a dissolved "
    "one. Use the council context ONLY as a last resort to break a tie between candidates "
    "with near-identical names; do NOT pick a company merely because its industry or SIC "
    "looks like it fits what the council bought. No supplier address is available, so do "
    "not rely on location.\n\n"
    "If none of the candidates is clearly the same legal entity, return \"NONE\". "
    "Returning NONE is correct and expected whenever you are not confident — never force "
    "a match. Also report confidence: HIGH (unmistakably the same entity), MEDIUM "
    "(likely), LOW (weak)."
)

PROMPT_PREAMBLE = (
    "Identify which candidate Companies House company is the same legal entity as the "
    "supplier below, or NONE.\n\n"
)

# columns expected from bt_stp1b output
NUM_COL = "ch_company_number"
NAME_COL = "ch_company_name"
STATUS_COL = "ch_status"
TOWN_COL = "ch_town"
SIC_COL = "sic_raw"


def parse_args():
    p = argparse.ArgumentParser(description="Build CH disambiguation batch JSONL")
    p.add_argument("--candidates", "-i", default="ch_candidates.csv",
                   help="Candidates CSV from bt_stp1b (default: ch_candidates.csv)")
    p.add_argument("--context", "-c", default="supplier_context.csv",
                   help="Supplier context CSV (optional; adds the context line)")
    p.add_argument("--output-dir", "-o", default="batch_input_ch")
    p.add_argument("--batch-size", "-b", type=int, default=BATCH_SIZE)
    return p.parse_args()


def build_prompt(name, context_descriptor, cands):
    """cands: list of dicts with number/name/status/town/sic."""
    lines = [f"Supplier: {name}"]
    if isinstance(context_descriptor, str) and context_descriptor.strip():
        lines.append(context_descriptor.strip())
    lines.append("\nCandidates:")
    for c in cands:
        bits = [f"company_number={c['number']}", c["name"]]
        if c.get("status"):
            bits.append(f"status: {c['status']}")
        if c.get("town"):
            bits.append(f"town: {c['town']}")
        if c.get("sic"):
            bits.append(f"SIC: {c['sic']}")
        lines.append("  - " + " | ".join(str(b) for b in bits if b))
    lines.append("\nReturn the company_number of the same legal entity, or NONE.")
    return PROMPT_PREAMBLE + "\n".join(lines)


def response_schema(candidate_numbers):
    return {
        "type": "OBJECT",
        "properties": {
            "company_number": {
                "type": "STRING",
                "enum": [str(n) for n in candidate_numbers] + ["NONE"],
                "description": "company_number of the same legal entity, or NONE.",
            },
            "confidence": {
                "type": "STRING",
                "enum": ["HIGH", "MEDIUM", "LOW"],
                "description": "Confidence the chosen company is the same entity.",
            },
        },
        "required": ["company_number", "confidence"],
    }


def build_row(supplier_id, name, context_descriptor, cands):
    numbers = [c["number"] for c in cands]
    return {
        "key": supplier_id,
        "request": {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [
                {"role": "user",
                 "parts": [{"text": build_prompt(name, context_descriptor, cands)}]}
            ],
            "generationConfig": {
                "temperature": MODEL_TEMPERATURE,
                "topK": MODEL_TOP_K,
                "topP": MODEL_TOP_P,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "candidateCount": MODEL_CANDIDATE_COUNT,
                "responseMimeType": "application/json",
                "responseSchema": response_schema(numbers),
            },
        },
    }


def main():
    args = parse_args()

    cand = pd.read_csv(args.candidates, dtype=str, encoding="utf-8-sig", low_memory=False)
    for col in (NUM_COL, NAME_COL, "supplier_clean"):
        if col not in cand.columns:
            sys.exit(f"'{col}' not in {args.candidates} — is this a bt_stp1b output?")

    # optional context
    ctx_map = {}
    if args.context and os.path.exists(args.context):
        ctx = pd.read_csv(args.context, dtype=str, encoding="utf-8-sig", low_memory=False)
        if "context_descriptor" in ctx.columns:
            ctx_map = dict(zip(ctx["supplier_clean"], ctx["context_descriptor"]))
        else:
            print(f"  (no context_descriptor column in {args.context} — name-only)")
    else:
        print(f"  (no context file at {args.context} — name-only prompts)")

    # group candidates per supplier (preserve order = score-ranked from bt_stp1b)
    suppliers = []
    for name, grp in cand.groupby("supplier_clean", sort=False):
        cands = [{
            "number": r[NUM_COL], "name": r[NAME_COL],
            "status": r.get(STATUS_COL, ""), "town": r.get(TOWN_COL, ""),
            "sic": r.get(SIC_COL, ""),
        } for _, r in grp.iterrows() if str(r[NUM_COL]).strip()]
        # de-dup candidate numbers (a number can appear via >1 basis)
        seen, uniq = set(), []
        for c in cands:
            if c["number"] not in seen:
                seen.add(c["number"]); uniq.append(c)
        if uniq:
            suppliers.append((name, uniq))

    print(f"Suppliers with candidates: {len(suppliers):,}")
    if not suppliers:
        sys.exit("Nothing to build.")

    os.makedirs(args.output_dir, exist_ok=True)

    # keymap
    keymap = pd.DataFrame(
        {"supplier_id": [f"SUPCH_{i+1}" for i in range(len(suppliers))],
         "supplier_clean": [s[0] for s in suppliers]}
    )
    keymap.to_csv(os.path.join(args.output_dir, "ch_keymap.csv"),
                  index=False, encoding="utf-8-sig")

    total = len(suppliers)
    nfiles = max(1, math.ceil(total / args.batch_size))
    written = 0
    for fi in range(nfiles):
        s, e = fi * args.batch_size, min((fi + 1) * args.batch_size, total)
        fname = ("batch_input_ch.jsonl" if nfiles == 1
                 else f"batch_input_ch_{fi+1:03d}.jsonl")
        fpath = os.path.join(args.output_dir, fname)
        with open(fpath, "w", encoding="utf-8") as fh:
            for i in range(s, e):
                name, cands = suppliers[i]
                row = build_row(f"SUPCH_{i+1}", name, ctx_map.get(name, ""), cands)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += (e - s)
        print(f"  {fpath}: {e-s:,} rows")

    print(f"Done. {written:,} requests across {nfiles} file(s).")
    print(f"Keymap → {os.path.join(args.output_dir, 'ch_keymap.csv')}")
    print("Submit with bt_stp3-style machinery (these JSONL already embed "
          "systemInstruction — do not inject the taxonomy doc).")


if __name__ == "__main__":
    main()
