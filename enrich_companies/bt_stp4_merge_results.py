#!/usr/bin/env python3
"""
bt_stp4_merge_results.py   (business-type batch — Step 4 / "C3")
─────────────────────────────────────────────────────────────────
Merge Gemini business-type batch results back onto the supplier list and
produce the final two-level labels.

Each supplier's final label comes from one of three sources, in priority order:
  1. override   — a hand-typed `override_business_type` in the reviewed file
  2. model      — the batch result (rows sent with include_in_batch = Y)
  3. baseline   — for trusted rows NOT sent (include_in_batch = N, type=Business),
                  the deterministic keyword label is mapped into the new taxonomy

Non-business rows (Gov Agency, Department, Persons Name, Personal Address,
Redacted, Nan) keep their deterministic type and carry no business sub-type
unless they were rescued into the batch.

Input
─────
• batch_results_bt/*.jsonl       (downloaded Gemini output)
• batch_input_bt/bt_keymap.csv   (supplier_id ↔ supplier_clean, from Step 2)
• distinct_suppliers_review_updated.csv  (reviewed file)
• business_type_taxonomy.csv     (sub-type → type + id)

Output
──────
• distinct_suppliers_enriched.csv   (utf-8-sig)

Usage
─────
    python bt_stp4_merge_results.py
    python bt_stp4_merge_results.py --results batch_results_bt/batch_results_bt_001.jsonl
"""

import argparse
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

# Maps the deterministic keyword labels (classify_suppliers.SECTOR) onto the
# locked 85-sub-type vocabulary, for trusted rows that bypass the model.
BASELINE_TO_SUBTYPE = {
    "Legal services": "Legal",
    "Care home / residential care": "Care home / residential",
    "Children's residential & fostering": "Children's residential & fostering",
    "Care & support services": "Home care & supported living",
    "Nursery / childcare": "Nursery & early years",
    "Higher education": "Higher education / university",
    "School / education": "Other Education",
    "GP / medical practice": "GP / health centre",
    "Dental practice": "Dental practice",
    "Pharmacy": "Pharmacy",
    "Healthcare provider": "Other Healthcare",
    "Therapy & counselling": "Therapy & counselling",
    "Building trades": "Building trades",
    "Construction & contracting": "General construction & contracting",
    "Property & facilities": "Facilities management",
    "Housing provider": "Housing association / registered provider",
    "Recruitment & staffing": "Recruitment & staffing",
    "Accountancy & audit": "Accountancy & audit",
    "Consultancy": "Consultancy",
    "IT & technology": "IT infrastructure & services",
    "Telecoms": "Telecoms",
    "Catering & food": "Catering services",
    "Cleaning services": "Cleaning & janitorial",
    "Security services": "Security",
    "Passenger transport": "Other Passenger Transport",
    "Transport & logistics": "Haulage & freight",
    "Motor & vehicle services": "Vehicle repair & maintenance",
    "Utilities & energy": "Energy & power",
    "Waste & recycling": "Waste, recycling & environmental",
    "Grounds & landscaping": "Grounds & landscaping",
    "Printing & signage": "Printing & signage",
    "Media & marketing": "Media, marketing & design",
    "Design": "Media, marketing & design",
    "Training & tuition": "Tuition & training",
    "Interpreting & translation": "Interpreting & translation",
    "Charity / voluntary": "Charity / voluntary",
    "Religious organisation": "Religious organisation",
    "Insurance": "Insurance",
    "Financial services": "Banking & finance",
    "Architecture": "Architecture",
    "Engineering": "Engineering",
    "Accommodation / hotel": "Temporary & emergency accommodation",
    "Arts & culture": "Arts & culture",
    "Sport & leisure": "Sport & leisure",
    "Goods & supplies": "Domestic & general goods",
    "Advice & advocacy": "Advice & advocacy",
    "Other / unclassified": "Other / unclassified",
}


def parse_args():
    p = argparse.ArgumentParser(description="Merge business-type batch results")
    p.add_argument("--results", "-r", nargs="+", default=None,
                   help="Result JSONL(s) (default: batch_results_bt/*.jsonl)")
    p.add_argument("--results-dir", default="batch_results_bt")
    p.add_argument("--keymap", "-k", default="batch_input_bt/bt_keymap.csv")
    p.add_argument("--suppliers", "-s", default="distinct_suppliers_review_updated.csv")
    p.add_argument("--taxonomy", "-t", default="business_type_taxonomy.csv")
    p.add_argument("--override-col", default="override_business_type")
    p.add_argument("--include-col", default="include_in_batch")
    p.add_argument("--output", "-o", default="distinct_suppliers_enriched.csv")
    return p.parse_args()


def parse_results(paths):
    """Return dict supplier_id -> (subtype, status)."""
    out = {}
    errors = 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    sid = obj.get("key")
                    parts = (obj.get("response", {})
                                .get("candidates", [{}])[0]
                                .get("content", {})
                                .get("parts", []))
                    parsed = next((json.loads(p["text"]) for p in parts if "text" in p), None)
                    if parsed is None or sid is None:
                        errors += 1
                        continue
                    out[sid] = (parsed.get("BUSINESS_SUBTYPE"),
                                parsed.get("STATUS", "UNKNOWN"))
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    errors += 1
    print(f"Parsed {len(out):,} results ({errors} errors)")
    return out


def main():
    args = parse_args()

    # taxonomy: subtype -> (type, id)
    tax = pd.read_csv(args.taxonomy, encoding="utf-8-sig")
    tax.columns = tax.columns.str.strip()
    type_by = dict(zip(tax["business_subtype"].str.strip(), tax["business_type"].str.strip()))
    id_by = dict(zip(tax["business_subtype"].str.strip(), tax["id"].str.strip()))
    valid = set(type_by)

    # results
    if args.results:
        result_files = args.results
    else:
        result_files = sorted(glob.glob(os.path.join(args.results_dir, "*.jsonl")))
    results = parse_results(result_files) if result_files else {}
    if not result_files:
        print("No result files found — only overrides/baseline will be applied.")

    # keymap supplier_id -> supplier_clean
    sid_to_name = {}
    if os.path.exists(args.keymap):
        km = pd.read_csv(args.keymap, encoding="utf-8-sig")
        sid_to_name = dict(zip(km["supplier_id"], km["supplier_clean"]))
    # name -> (subtype, status) from model
    model_by_name = {}
    for sid, (st, status) in results.items():
        name = sid_to_name.get(sid)
        if name is not None:
            model_by_name[name] = (st, status)

    sup = pd.read_csv(args.suppliers, low_memory=False, encoding="utf-8-sig")

    final_type, final_sub, final_id, final_status, source = [], [], [], [], []
    for _, r in sup.iterrows():
        name = r["supplier_clean"]
        t = str(r.get("type", "")).strip()
        ov = str(r.get(args.override_col, "")).strip()
        ov_t = str(r.get("override_type", "")).strip()
        inc = str(r.get(args.include_col, "")).strip().upper() == "Y"
        baseline = str(r.get("business_type", "")).strip()

        st = ty = bid = ""
        status = src = ""

        if ov and ov.lower() != "nan":                       # 1a. subtype override
            st = ov; src = "override"; status = "OVERRIDE"
        elif ov_t and ov_t.lower() != "nan":                 # 1b. type-only override
            st = f"Other {ov_t}"; src = "override"; status = "OVERRIDE_TYPE_ONLY"
        elif name in model_by_name:                          # 2. model
            st, status = model_by_name[name]; src = "model"
        elif t == "Business" and not inc:                    # 3. baseline map
            st = BASELINE_TO_SUBTYPE.get(baseline, "Other / unclassified")
            src = "baseline"; status = "DETERMINISTIC"
        else:
            # non-business not rescued, or business with no result
            src = "none"; status = ""

        if st:
            if st in valid:
                ty, bid = type_by[st], id_by[st]
            else:                                            # safety net
                st = "Other / unclassified"
                ty, bid, status = type_by.get(st, "Other / Unclassified"), id_by.get(st, ""), "INVALID_LABEL"

        final_sub.append(st); final_type.append(ty); final_id.append(bid)
        final_status.append(status); source.append(src)

    sup["final_business_type"] = final_type
    sup["final_business_subtype"] = final_sub
    sup["final_bt_id"] = final_id
    sup["classification_status"] = final_status
    sup["classification_source"] = source

    sup.to_csv(args.output, index=False, encoding="utf-8-sig")

    from collections import Counter
    print(f"Wrote {len(sup):,} rows → {args.output}")
    print("source:", dict(Counter(source)))
    print("status:", dict(Counter(final_status)))


if __name__ == "__main__":
    main()
