#!/usr/bin/env python3
"""
bt_stp1_build_supplier_context.py
─────────────────────────
Collapse the aggregated spend base (one row per
department × expense_type × service_area × supplier_category × supplier_clean)
down to ONE row per supplier_clean, carrying a compact, high-signal context
descriptor for the business-type model prompt.

For each categorical context field we keep the TOP-N distinct values ranked by
summed total_amount (so the dominant service line leads), de-duplicated and
blank-stripped. We also roll up scale (txn count, total spend, weighted avg).

Input  : total_spend_base.csv   (columns shown in --help)
Output : supplier_context.csv    (one row per supplier_clean, utf-8-sig)

Usage
─────
    python bt_stp1_build_supplier_context.py
    python bt_stp1_build_supplier_context.py --input total_spend_base.csv --top-n 3
    python bt_stp1_build_supplier_context.py --rank-by txn_count
"""

import argparse
import os
import pandas as pd

CONTEXT_FIELDS = ["service_area", "expense_type", "department", "supplier_category"]
# order = priority in the descriptor (service_area first: best "what is it" signal)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", "-i", default="total_spend_base.csv",
                   help="Aggregated spend base CSV (default: total_spend_base.csv)")
    p.add_argument("--output", "-o", default="supplier_context.csv",
                   help="Output CSV (default: supplier_context.csv)")
    p.add_argument("--top-n", "-n", type=int, default=3,
                   help="Distinct values to keep per context field (default: 3)")
    p.add_argument("--rank-by", choices=["total_amount", "txn_count"],
                   default="total_amount",
                   help="Rank distinct values by spend or txn count (default: total_amount)")
    p.add_argument("--max-field-chars", type=int, default=120,
                   help="Hard cap per joined field, safety only (default: 120)")
    p.add_argument("--review", default="distinct_suppliers_review_updated.csv",
                   help="Reviewed suppliers CSV used to filter to include_in_batch=Y "
                        "(default: distinct_suppliers_review_updated.csv)")
    p.add_argument("--include-col", default="include_in_batch",
                   help="Column in --review whose value 'Y' keeps a supplier (default: include_in_batch)")
    return p.parse_args()


def top_values(df, field, rank_by, top_n, cap):
    """Return Series: supplier_clean -> 'val1; val2; val3' (top-N by rank_by)."""
    sub = df[["supplier_clean", field, rank_by]].copy()
    sub[field] = sub[field].astype(str).str.strip()
    sub = sub[(sub[field] != "") & (sub[field].str.lower() != "nan")]
    if sub.empty:
        return pd.Series(dtype=str)

    g = (sub.groupby(["supplier_clean", field], as_index=False)[rank_by]
            .sum()
            .sort_values(["supplier_clean", rank_by], ascending=[True, False]))
    g["rk"] = g.groupby("supplier_clean").cumcount()
    g = g[g["rk"] < top_n]

    def join(s):
        out = "; ".join(s)
        return out[:cap].rsplit("; ", 1)[0] if len(out) > cap else out

    return g.groupby("supplier_clean")[field].apply(join)


def main():
    args = parse_args()

    df = pd.read_csv(args.input, low_memory=False, encoding="utf-8-sig")
    needed = {"supplier_clean", "txn_count", "total_amount", *CONTEXT_FIELDS}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(f"Input missing required columns: {sorted(missing)}")

    print(f"Read {len(df):,} base rows; "
          f"{df['supplier_clean'].nunique():,} distinct suppliers")

    # ── restrict to suppliers flagged include_in_batch = Y ──
    keep = None
    if args.review and os.path.exists(args.review):
        rv = pd.read_csv(args.review, low_memory=False, encoding="utf-8-sig")
        if args.include_col not in rv.columns:
            raise SystemExit(f"'{args.include_col}' not in {args.review}")
        flag = rv[args.include_col].fillna("").astype(str).str.strip().str.upper()
        keep = set(rv.loc[flag == "Y", "supplier_clean"])
        before = df["supplier_clean"].nunique()
        df = df[df["supplier_clean"].isin(keep)]
        print(f"Filtered to include_in_batch=Y: {len(keep):,} flagged; "
              f"{df['supplier_clean'].nunique():,} of {before:,} suppliers kept")
    else:
        print(f"  (no review file at {args.review} — keeping all suppliers)")

    # ── scale roll-up ────────────────────────────────────
    scale = (df.groupby("supplier_clean")
               .agg(no_of_transactions=("txn_count", "sum"),
                    total_spend=("total_amount", "sum"))
               .reset_index())
    scale["avg_amount"] = (scale["total_spend"] / scale["no_of_transactions"]).round(2)
    scale["total_spend"] = scale["total_spend"].round(2)

    out = scale

    # ── top-N distinct context values per field ──────────
    for field in CONTEXT_FIELDS:
        s = top_values(df, field, args.rank_by, args.top_n, args.max_field_chars)
        out = out.merge(s.rename(f"top_{field}"), on="supplier_clean", how="left")

    out = out.fillna("")

    # ── build a single prompt-ready descriptor ───────────
    labels = {
        "top_service_area": "Service areas",
        "top_expense_type": "Expense types",
        "top_department": "Departments",
        "top_supplier_category": "Supplier category",
    }

    def descriptor(r):
        parts = []
        for col, lab in labels.items():
            if r[col]:
                parts.append(f"{lab}: {r[col]}")
        parts.append(f"Scale: {int(r['no_of_transactions']):,} no_of_transactions, "
                     f"£{r['total_spend']:,.0f}")
        return " | ".join(parts)

    out["context_descriptor"] = out.apply(descriptor, axis=1)

    cols = (["supplier_clean", "no_of_transactions", "total_spend", "avg_amount"]
            + [f"top_{f}" for f in CONTEXT_FIELDS]
            + ["context_descriptor"])
    out = out[cols].sort_values("total_spend", ascending=False)

    out.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(out):,} suppliers -> {args.output}")
    print("\nExample descriptors:")
    for _, r in out.head(6).iterrows():
        print(f"  {r['supplier_clean']}")
        print(f"      {r['context_descriptor']}")


if __name__ == "__main__":
    main()
