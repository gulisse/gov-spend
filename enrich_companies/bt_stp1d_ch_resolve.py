#!/usr/bin/env python3
"""
bt_stp1d_ch_resolve.py   (Companies House enrichment — Stage 1, Step 4 / final)
───────────────────────────────────────────────────────────────────────────────
Turn the matcher's deterministic resolutions PLUS the model's disambiguation picks
into one supplier→SIC table. One row per supplier_clean. Primary SIC only, rolled
up to ALL levels (subclass → class → division → section) via the ONS structure.

Why no 5.7M re-read: every company we resolve already appears as a row in
ch_exact*.csv (deterministic) or ch_candidates*.csv (a candidate the model chose),
and those rows already carry ch_company_name + sic_raw. We look the SIC up there.

Inputs
──────
• --exact        ch_exact_v2.csv          (deterministic finals from bt_stp1b)
• --candidates   ch_candidates_v2.csv     (model chose one of these per supplier)
• --results      batch_results_ch/*.jsonl (Gemini picks: {company_number, confidence})
• --keymap       batch_input_ch/ch_keymap.csv   (SUPCH_N ↔ supplier_clean)
• --ons          publisheduksicsummaryofstructureworksheet.xlsx  (SIC hierarchy)

Output
──────
• --out supplier_sic.csv   (one row per supplier_clean; utf-8-sig)
    supplier_clean, ch_company_number, ch_company_name, resolution_method,
    confidence, sic_code, sic_description, sic_class, class_description,
    sic_division, division_description, sic_section, section_description,
    sic_count, sic_raw

Resolution precedence:  deterministic (exact*) wins over model; model wins over
nothing. Suppliers the model returned NONE for (or that errored) are written with
an empty company_number and resolution_method='model_none' / 'model_error' so the
table accounts for every supplier that had candidates.

Usage
─────
    python bt_stp1d_ch_resolve.py            # all defaults (_v2 inputs)
    python bt_stp1d_ch_resolve.py --exact ch_exact_v2.csv \
        --candidates ch_candidates_v2.csv --results "batch_results_ch/*.jsonl" \
        --keymap batch_input_ch/ch_keymap.csv \
        --ons publisheduksicsummaryofstructureworksheet.xlsx --out supplier_sic.csv

Prerequisites
─────────────
    pip install pandas openpyxl
"""

import argparse
import glob
import json
import os
import re
import sys

import pandas as pd

NUM_COL = "ch_company_number"
NAME_COL = "ch_company_name"
SIC_COL = "sic_raw"
BASIS_COL = "match_basis"
CONF_COL = "confidence"

_CODE_RE = re.compile(r"(\d{4,5})")          # leading SIC code in a SicText string


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Resolve CH matches + model picks into supplier_sic.csv")
    p.add_argument("--exact", default="ch_exact_v2.csv")
    p.add_argument("--candidates", default="ch_candidates_v2.csv")
    p.add_argument("--results", default="batch_results_ch/*.jsonl",
                   help="Glob for Gemini result JSONL files")
    p.add_argument("--keymap", default="batch_input_ch/ch_keymap.csv")
    p.add_argument("--ons", default="publisheduksicsummaryofstructureworksheet.xlsx")
    p.add_argument("--out", default="supplier_sic.csv")
    p.add_argument("--include-unresolved", action="store_true",
                   help="Also write model NONE/error rows (default: on)", default=True)
    return p.parse_args()


# ──────────────────────────────────────────────
# ONS SIC hierarchy  (4-digit Class → Group → Division → Section, with descriptions)
# CH gives 5-digit subclasses, so class = code[:4], division = code[:2].
# ──────────────────────────────────────────────
def load_sic_hierarchy(path):
    if not os.path.exists(path):
        sys.exit(f"ONS workbook not found: {path}")
    df = pd.read_excel(path, sheet_name="reworked structure", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    for c in ("SECTION", "Division", "Group", "Class", "Description"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()

    section_desc, division_desc, group_desc, class_desc = {}, {}, {}, {}
    division_to_section = {}
    na = {"na", "nan", "", "none"}
    for _, r in df.iterrows():
        desc = r.get("Description", "")
        sec = r.get("SECTION", "")
        div = r.get("Division", "")
        grp = r.get("Group", "")
        cls = r.get("Class", "")
        sec_l, div_l = sec.lower(), div.lower()
        cls_l, grp_l = cls.lower(), grp.lower()
        if sec_l not in na and div_l in na:                       # section-level row
            section_desc[sec] = desc
        if div_l not in na and grp_l in na and cls_l in na:       # division-level row
            division_desc[div] = desc
        if div_l not in na and sec_l not in na:
            division_to_section.setdefault(div, sec)
        if grp_l not in na and cls_l in na:                       # group-level row
            group_desc[grp] = desc
        if cls_l not in na:                                       # class-level row
            class_desc[cls] = desc
    return {"section": section_desc, "division": division_desc,
            "group": group_desc, "class": class_desc, "div2sec": division_to_section}


def primary_sic(sic_raw):
    """First SIC of the '; '-joined sic_raw → (code, subclass_description)."""
    if not isinstance(sic_raw, str) or not sic_raw.strip():
        return "", ""
    first = sic_raw.split(";")[0].strip()
    if not first:
        return "", ""
    m = _CODE_RE.match(first)
    code = m.group(1) if m else ""
    desc = first.split(" - ", 1)[1].strip() if " - " in first else first
    return code, desc


def sic_count(sic_raw):
    if not isinstance(sic_raw, str):
        return 0
    return sum(1 for seg in sic_raw.split(";") if seg.strip())


def rollup(sic_raw, H):
    """Return all SIC levels for the primary code."""
    code, subclass_desc = primary_sic(sic_raw)
    out = {"sic_code": code, "sic_description": subclass_desc,
           "sic_class": "", "class_description": "",
           "sic_division": "", "division_description": "",
           "sic_section": "", "section_description": ""}
    if not code:
        return out
    cls, div = code[:4], code[:2]
    sec = H["div2sec"].get(div, "")
    out["sic_class"] = cls
    out["class_description"] = H["class"].get(cls, "")
    out["sic_division"] = div
    out["division_description"] = H["division"].get(div, "")
    out["sic_section"] = sec
    out["section_description"] = H["section"].get(sec, "")
    return out


# ──────────────────────────────────────────────
# Parse the Gemini batch results  (key=SUPCH_N → {company_number, confidence})
# ──────────────────────────────────────────────
def _extract_key(obj):
    for k in ("key", "custom_id", "id"):
        if obj.get(k):
            return str(obj[k])
    req = obj.get("request") or {}
    if isinstance(req, dict) and req.get("key"):
        return str(req["key"])
    return None


def _extract_answer(obj):
    parts = (obj.get("response", {}).get("candidates", [{}])[0]
                .get("content", {}).get("parts", []))
    for p in parts:
        if "text" in p:
            try:
                parsed = json.loads(p["text"])
                return (str(parsed.get("company_number", "")).strip(),
                        str(parsed.get("confidence", "")).strip())
            except (json.JSONDecodeError, TypeError):
                return None, None
    return None, None


def load_model_picks(results_glob, keymap_path):
    if not os.path.exists(keymap_path):
        sys.exit(f"Keymap not found: {keymap_path}")
    km = pd.read_csv(keymap_path, dtype=str, encoding="utf-8-sig")
    id2sup = dict(zip(km["supplier_id"], km["supplier_clean"]))

    files = sorted(glob.glob(results_glob))
    if not files:
        print(f"  (no result files match {results_glob} — deterministic-only run)")
    picks = {}     # supplier_clean -> (company_number or "", confidence, status)
    n_lines = 0
    for fp in files:
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = _extract_key(obj)
                sup = id2sup.get(key) if key else None
                if not sup:
                    continue
                num, conf = _extract_answer(obj)
                if num is None:
                    picks[sup] = ("", "", "model_error")
                elif num.upper() == "NONE" or not num:
                    picks[sup] = ("", conf or "", "model_none")
                else:
                    picks[sup] = (num, conf or "", "model")
    print(f"  parsed {n_lines:,} result lines → {len(picks):,} supplier picks "
          f"({len(files)} file(s))")
    return picks


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    args = parse_args()

    print("Loading ONS SIC hierarchy…")
    H = load_sic_hierarchy(args.ons)
    print(f"  sections={len(H['section'])}  divisions={len(H['division'])}  "
          f"classes={len(H['class'])}")

    if not os.path.exists(args.exact):
        sys.exit(f"Missing {args.exact} (run bt_stp1b first).")
    exact = pd.read_csv(args.exact, dtype=str, encoding="utf-8-sig").fillna("")
    cand = (pd.read_csv(args.candidates, dtype=str, encoding="utf-8-sig").fillna("")
            if os.path.exists(args.candidates) else pd.DataFrame())
    print(f"Deterministic (exact): {len(exact):,} rows | candidates: {len(cand):,} rows")

    # candidate lookup: (supplier_clean, number) -> {name, sic_raw}
    cand_lookup = {}
    if len(cand):
        for _, r in cand.iterrows():
            cand_lookup[(r["supplier_clean"], r[NUM_COL])] = (r[NAME_COL], r.get(SIC_COL, ""))

    model_picks = load_model_picks(args.results, args.keymap)

    rows = []
    seen = set()

    # 1) deterministic resolutions take precedence
    for _, r in exact.iterrows():
        sup = r["supplier_clean"]
        if sup in seen:
            continue
        seen.add(sup)
        sic_raw = r.get(SIC_COL, "")
        rows.append({
            "supplier_clean": sup,
            "ch_company_number": r[NUM_COL],
            "ch_company_name": r[NAME_COL],
            "resolution_method": r.get(BASIS_COL, "exact"),
            "confidence": r.get(CONF_COL, "high"),
            "sic_count": sic_count(sic_raw),
            "sic_raw": sic_raw,
            **rollup(sic_raw, H),
        })

    # 2) model picks for suppliers not already resolved deterministically
    for sup, (num, conf, status) in model_picks.items():
        if sup in seen:
            continue
        seen.add(sup)
        if status == "model" and num:
            name, sic_raw = cand_lookup.get((sup, num), ("", ""))
            rows.append({
                "supplier_clean": sup,
                "ch_company_number": num,
                "ch_company_name": name,
                "resolution_method": "model",
                "confidence": conf.lower(),
                "sic_count": sic_count(sic_raw),
                "sic_raw": sic_raw,
                **rollup(sic_raw, H),
            })
        elif args.include_unresolved:
            rows.append({
                "supplier_clean": sup,
                "ch_company_number": "",
                "ch_company_name": "",
                "resolution_method": status,           # model_none / model_error
                "confidence": conf.lower(),
                "sic_count": 0, "sic_raw": "",
                **rollup("", H),
            })

    out = pd.DataFrame(rows, columns=[
        "supplier_clean", "ch_company_number", "ch_company_name", "resolution_method",
        "confidence", "sic_code", "sic_description", "sic_class", "class_description",
        "sic_division", "division_description", "sic_section", "section_description",
        "sic_count", "sic_raw",
    ])
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    # report
    resolved = out[out["ch_company_number"] != ""]
    by_method = out["resolution_method"].value_counts()
    with_sic = (resolved["sic_code"] != "").sum()
    print("\n" + "=" * 52)
    print(f"supplier_sic.csv written: {len(out):,} rows")
    print(f"  resolved to a company : {len(resolved):,}")
    print(f"  with a primary SIC    : {with_sic:,}")
    print(f"  unresolved (NONE/err) : {len(out) - len(resolved):,}")
    print("  by method:")
    for m, n in by_method.items():
        print(f"    {m:18}: {n:,}")
    print("=" * 52)
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
