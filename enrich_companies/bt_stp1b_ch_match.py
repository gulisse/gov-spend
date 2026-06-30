#!/usr/bin/env python3
"""
bt_stp1b_ch_match.py   (Companies House enrichment — Stage 1, matcher)
──────────────────────────────────────────────────────────────────────
Resolve each supplier_clean to a Companies House (CH) record by:
  1. EXACT match on a normalised name key (deterministic, free).
  2. For misses, generate <=K candidates via:
       • FUZZY  — blocking on first significant token + rapidfuzz.
       • INITIALISM — shared initials-signature (handles  BBC ↔ British
         Broadcasting Corporation  and  F M Conway ↔ Fred Mathew Conway).
  3. AUTO-ACCEPT a fuzzy/initialism candidate only when it is unique + active
     and scores above --auto-cutoff; everything else is left for the model.
 
Outputs
───────
  • ch_exact.csv       — confidently resolved suppliers (+ SIC carried through)
  • ch_candidates.csv  — each unresolved supplier → <=K candidate CH records
  • ch_match_report.txt — coverage stats (share resolved / needing model / unmatched)
 
The full CH file (5.7M rows, 2.75GB) is projected once to a slim Parquet cache
with precomputed match keys; re-runs load the Parquet in seconds.
 
Speed: normalisation is vectorised; fuzzy scoring uses rapidfuzz.process.cdist
with workers=-1 (internal multithread). No outer ThreadPoolExecutor — it would
oversubscribe the cores cdist already uses.
 
Usage
─────
    python bt_stp1b_ch_match.py                       # defaults
    python bt_stp1b_ch_match.py --ch Companies_House_companies_list.csv \
        --suppliers supplier_context.csv --k 5
    python bt_stp1b_ch_match.py --rebuild-cache       # force re-project the CH file
 
Prereqs
───────
    pip install pandas pyarrow rapidfuzz
"""
 
import argparse
import gc
import os
import re
import sys
import time
from datetime import datetime
 
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from rapidfuzz import fuzz, process # type: ignore
 
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

# ──────────────────────────────────────────────
# CH columns we need (header whitespace is stripped on read)
# ──────────────────────────────────────────────
CH_COLS = [
    "CompanyName", "CompanyNumber", "CompanyStatus", "CompanyCategory",
    "RegAddress.PostTown",
    "SICCode.SicText_1", "SICCode.SicText_2",
    "SICCode.SicText_3", "SICCode.SicText_4",
] + [f"PreviousName_{i}.CompanyName" for i in range(1, 11)]   # (2) aliases
 
PREVNAME_COLS = [f"PreviousName_{i}.CompanyName" for i in range(1, 11)]
 
# (3) Words signalling a NON-operating / parent / variant entity. When several CH rows
# share a supplier's norm (homonyms), we prefer the plain operating company over these.
ADORN_WORDS = {
    "holdings", "holding", "group", "international", "global", "investments",
    "investment", "ventures", "trust", "trustee", "trustees", "trustees'", "nominee",
    "nominees", "pension", "pensions", "secretarial", "parent",
}
 
# ──────────────────────────────────────────────
# Normalisation  (applied IDENTICALLY to supplier_clean and CH CompanyName)
# ──────────────────────────────────────────────
SUFFIXES = {
    "ltd", "limited", "plc", "llp", "llc", "lp", "cic", "cio", "co", "company",
    "group", "holdings", "inc", "incorporated", "ug", "gmbh",
}
CONNECTORS = {"and"}                       # '&' is mapped to space first
QUOTE_RE = re.compile(r"[\"'`´\u201c\u201d\u2018\u2019]")
NONALNUM_RE = re.compile(r"[^a-z0-9]+")
 
 
def normalise(name: str):
    """Return (canonical_key, token_list)."""
    if not isinstance(name, str):
        return "", []
    s = name.lower()
    s = QUOTE_RE.sub("", s)               # strip quotes
    s = s.replace("&", " ")               # ampersand -> connector slot
    s = NONALNUM_RE.sub(" ", s)           # punctuation/periods -> space
    toks = [t for t in s.split() if t and t not in CONNECTORS and t not in SUFFIXES]
    if toks and toks[0] == "the":
        toks = toks[1:]
    # collapse runs of single-letter tokens:  f m conway -> fm conway
    out, buf = [], []
    for t in toks:
        if len(t) == 1:
            buf.append(t)
        else:
            if buf:
                out.append("".join(buf)); buf = []
            out.append(t)
    if buf:
        out.append("".join(buf))
    return " ".join(out), out
 
 
# ──────────────────────────────────────────────
# SUPPLIER-SIDE, MATCH-ONLY cleanup (in-memory; NEVER written to disk).
# Applied to supplier_clean ONLY, immediately before normalise(), so the CH comparison
# sees a cleaner name. The original supplier_clean is left untouched and remains the join
# key for every downstream lookup — nothing is regenerated. NOT applied to the CH side.
# Handles the two things normalise() can't: leading card/payment-statement descriptors
# (normalise would keep 'stk' as a token) and trailing web-domains + descriptors.
# ──────────────────────────────────────────────
_PAY_PREFIX_RE = re.compile(
    r"^(?:stk\*|wp[-*]|sp[\s*]+|sq\s*\*\s*|iz\s*\*\s*|ztl\*|paypal\s*\*\s*|pp\*|"
    r"sumup\s*\*?\s*|gocardless\s*\*?\s*)",
    re.IGNORECASE,
)
# strip a trailing web-domain (and anything after it). TLDs are explicit + \b-bounded so
# '.construction'/'.company'/'St. John' are NOT touched. e.g. 'Eplatform.co Ebooks'->'Eplatform'
_DOMAIN_TRAIL_RE = re.compile(
    r"\.(?:co\.uk|org\.uk|com|co|org|net|io|biz|info)\b.*$", re.IGNORECASE
)
 
 
def clean_for_match(name):
    if not isinstance(name, str):
        return name
    s = name
    for _ in range(3):                                   # stacked descriptors
        new = _PAY_PREFIX_RE.sub("", s, count=1).lstrip(" *-:/.")
        if new == s:
            break
        s = new
    s = _DOMAIN_TRAIL_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or name                                     # never empty
 
 
def initials_sig(tokens) -> str:
    """Initials signature; short tokens (<=3) expand to their letters so that
    'fm'->f,m and 'bbc'->b,b,c. Aligns acronyms / spaced-initials / full forms.
        F M Conway      -> [fm, conway]          -> f,m,c  -> 'fmc'
        Fred Mathew Conway -> [fred, mathew, conway] -> f,m,c -> 'fmc'
        BBC             -> [bbc]                  -> b,b,c  -> 'bbc'
    """
    letters = []
    for t in tokens:
        if t.isdigit():
            continue
        letters.extend(list(t) if len(t) <= 3 else [t[0]])
    return "".join(letters)
 
 
def block_key(tokens) -> str:
    """First significant token — the fuzzy blocking key."""
    return tokens[0] if tokens else ""
 
 
# ──────────────────────────────────────────────
# Build / load the slim CH Parquet cache
# ──────────────────────────────────────────────
 
def _needed(colname: str) -> bool:
    return colname.strip() in set(CH_COLS)
 
 
def build_ch_parquet(ch_csv: str, parquet_path: str, logger_print, chunksize=500_000):
    """Project + normalise the CH CSV to a slim Parquet (chunked, low memory)."""
    logger_print(f"Building CH cache from {ch_csv} → {parquet_path}")
    writer = None
    total = 0
    t0 = time.time()
    reader = pd.read_csv(
        ch_csv, dtype=str, chunksize=chunksize, usecols=_needed,
        encoding="utf-8-sig", on_bad_lines="skip", low_memory=True,
    )
    for chunk in reader:
        chunk.columns = [c.strip() for c in chunk.columns]
        name = chunk["CompanyName"].fillna("")
        norm = name.map(normalise)
        chunk["norm_name"] = norm.map(lambda x: x[0])
        chunk["block_key"] = norm.map(lambda x: block_key(x[1]))
        chunk["initials_sig"] = norm.map(lambda x: initials_sig(x[1]))
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(parquet_path, table.schema)
        writer.write_table(table)
        total += len(chunk)
        logger_print(f"  …{total:,} rows")
    if writer:
        writer.close()
    logger_print(f"CH cache built: {total:,} rows in {time.time()-t0:.1f}s")
 
 
def load_ch(parquet_path: str) -> pd.DataFrame:
    df = pq.read_table(parquet_path).to_pandas()
    # collapse the 4 SIC text columns into one ';'-joined raw field
    sic_cols = [c for c in ["SICCode.SicText_1", "SICCode.SicText_2",
                            "SICCode.SicText_3", "SICCode.SicText_4"] if c in df.columns]
    df["sic_raw"] = (
        df[sic_cols].fillna("").agg("; ".join, axis=1)
        .str.replace(r"(;\s*)+", "; ", regex=True).str.strip("; ").str.strip()
    )
    # Low-cardinality columns -> category: large RAM saving on a 5.7M-row frame
    # (a handful of statuses, ~tens of thousands of towns vs 5.7M object strings).
    for c in ("CompanyStatus", "CompanyCategory", "RegAddress.PostTown"):
        if c in df.columns:
            df[c] = df[c].astype("category")
    return df
 
 
# ──────────────────────────────────────────────
# Indexes
# ──────────────────────────────────────────────
 
def build_indexes(ch: pd.DataFrame):
    exact = ch.groupby("norm_name").indices            # norm_name -> array of row positions
    blocks = ch.groupby("block_key").indices
    inits = ch.groupby("initials_sig").indices
    # (2) Previous-name EXACT aliases: norm(previous name) -> canonical CH row positions.
    # A supplier matching a former name resolves to the CURRENT company. Built vectorised
    # (stack the prev-name columns, normalise only the distinct non-empty values).
    alias_exact = {}
    prev_cols = [c for c in PREVNAME_COLS if c in ch.columns]
    if prev_cols:
        long = ch[prev_cols].stack()                   # (row, col) -> prev name
        long = long[long.astype(str).str.len() > 0]
        if len(long):
            uniq = pd.unique(long.values)
            nmap = {u: normalise(u)[0] for u in uniq}
            an = long.map(nmap) # type: ignore
            pos = long.index.get_level_values(0).to_numpy()
            cur = ch["norm_name"].to_numpy()
            alias = pd.DataFrame({"norm": an.to_numpy(), "pos": pos})
            alias = alias[(alias["norm"].str.len() > 0)
                          & (alias["norm"].to_numpy() != cur[pos])]   # skip == current
            alias_exact = alias.groupby("norm")["pos"].apply(lambda s: list(dict.fromkeys(s))).to_dict()
    return exact, blocks, inits, alias_exact
 
 
def is_active(status: str) -> bool:
    return isinstance(status, str) and "active" in status.lower()
 
 
def _is_adorned(raw_name: str) -> bool:
    rl = (raw_name or "").lower()
    return ("(" in rl) or any(w in rl.split() for w in ADORN_WORDS)
 
 
# ──────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────
 
def match_suppliers(suppliers: pd.DataFrame, cols: dict, exact, blocks, inits, alias_exact,
                    k: int, fuzzy_cutoff: int, auto_cutoff: int, logger_print,
                    progress_every: int = 1000):
    names = cols["names"]
    norms = cols["norms"]
    nums = cols["nums"]
    status = cols["status"]
    towns = cols["towns"]
    sics = cols["sics"]
 
    exact_rows, cand_rows = [], []
    stats = {"total": 0, "exact_unique": 0, "exact_active_pick": 0,
             "exact_prev": 0, "homonym_to_model": 0, "fuzzy_auto": 0,
             "to_model": 0, "no_match": 0}
 
    def rec(pos, basis, score, conf):
        return {
            "ch_row": pos, "ch_company_number": nums[pos], "ch_company_name": names[pos],
            "ch_status": status[pos], "ch_town": towns[pos], "sic_raw": sics[pos],
            "match_basis": basis, "score": score, "confidence": conf,
        }
 
    # (3) preference for ranking homonyms / tied candidates — lower is better:
    #   active first, then NON-adorned (no holdings/group/parenthetical), then fewer
    #   extra norm tokens vs the supplier, then shorter raw name. Surfaces the plain
    #   operating company so it survives the top-k cut and leads the model's list.
    def pref_key(pos, skey):
        n_extra = max(0, len(norms[pos].split()) - len(skey.split()))
        return (0 if is_active(status[pos]) else 1,
                1 if _is_adorned(names[pos]) else 0,
                n_extra,
                len(str(names[pos]).split()))
 
    supplier_names = suppliers["supplier_clean"].tolist()
    n_total = len(supplier_names)
    t_start = time.time()
    logger_print(f"Match start : {datetime.now():%Y-%m-%d %H:%M:%S}  ({n_total:,} suppliers)")
 
    for i, sname in enumerate(supplier_names, 1):
        stats["total"] += 1
 
        # ── progress heartbeat ──────────────────
        if progress_every and i % progress_every == 0:
            el = time.time() - t_start
            rate = i / el if el > 0 else 0.0
            eta = (n_total - i) / rate if rate > 0 else 0.0
            res = stats["exact_unique"] + stats["exact_active_pick"] + stats["fuzzy_auto"]
            cands = stats["homonym_to_model"] + stats["to_model"]
            logger_print(
                f"  [{datetime.now():%H:%M:%S}] {i:>7,}/{n_total:,} ({i/n_total*100:4.1f}%)"
                f" | {res:,} resolved, {cands:,} cands, {stats['no_match']:,} no-match"
                f" | {rate:,.0f} rows/s | ETA {eta:,.0f}s"
            )
 
        skey, stoks = normalise(clean_for_match(sname) if isinstance(sname, str) else "")
        sig = initials_sig(stoks)
        blk = block_key(stoks)
 
        # ── 1. exact (current name, then previous-name alias) ──
        pos_list, ebasis = None, "exact"
        if skey and skey in exact:
            pos_list, ebasis = list(exact[skey]), "exact"
        elif skey and skey in alias_exact:
            pos_list, ebasis = list(alias_exact[skey]), "exact_prev"
 
        if pos_list is not None:
            if len(pos_list) == 1:
                p = pos_list[0]
                conf = "high" if ebasis == "exact" else "medium"
                exact_rows.append({"supplier_clean": sname, **rec(p, ebasis, 100, conf)})
                stats["exact_unique" if ebasis == "exact" else "exact_prev"] += 1
                continue
            # (3) rank the homonyms; prefer plain active operating company
            ranked = sorted(pos_list, key=lambda p: pref_key(p, skey))
            actives = [p for p in pos_list if is_active(status[p])]
            if len(actives) == 1:
                p = actives[0]
                exact_rows.append({"supplier_clean": sname,
                                   **rec(p, ebasis + "_active", 100, "medium")})
                stats["exact_active_pick" if ebasis == "exact" else "exact_prev"] += 1
                continue
            # (3) plain-winner auto-resolve: exactly one active & non-adorned candidate
            plain = [p for p in actives if not _is_adorned(names[p])]
            if len(plain) == 1:
                p = plain[0]
                exact_rows.append({"supplier_clean": sname,
                                   **rec(p, ebasis + "_plain", 100, "medium")})
                stats["exact_active_pick" if ebasis == "exact" else "exact_prev"] += 1
                continue
            # otherwise → ranked homonyms to the model (right one survives the cut)
            for p in ranked[:k]:
                cand_rows.append({"supplier_clean": sname,
                                  **rec(p, ebasis + "_homonym", 100, "low")})
            stats["homonym_to_model"] += 1
            continue
 
        # ── 2. candidates ───────────────────────
        cand_scores = {}                  # pos -> (score, basis)
        fuzzy_auto = None                 # (pos, score) if a unique strong fuzzy hit
 
        # 2a. FUZZY — block on first token, score with WRatio, cutoff-gated.
        if blk and blk in blocks:
            choices = {p: norms[p] for p in blocks[blk]}
            scored = process.extract(
                skey, choices, scorer=fuzz.WRatio,
                limit=max(k, 5), score_cutoff=fuzzy_cutoff,
            )                              # -> [(norm, score, pos), …]
            for (_, sc, p) in scored:
                cand_scores[p] = (int(sc), "fuzzy")
            strong = [(int(sc), p) for (_, sc, p) in scored
                      if sc >= auto_cutoff and is_active(status[p])]
            if len(strong) == 1:
                fuzzy_auto = strong[0]
 
        # 2b. INITIALISM — shared initials signature. NOT surface-fuzz gated
        #     (acronym↔expansion is dissimilar by characters). Narrow a big
        #     signature pool with a shared long token (the surname-style anchor);
        #     for a bare acronym, rank by WRatio and cap. Never auto-accepted.
        long_toks = [t for t in stoks if len(t) > 3]
        if sig and sig in inits:
            ipool = list(inits[sig])
            if long_toks:
                L = max(long_toks, key=len)
                ipool = [p for p in ipool if f" {L} " in f" {norms[p]} "]
            else:
                ipool = [p for (_, _, p) in process.extract(
                    skey, {p: norms[p] for p in ipool},
                    scorer=fuzz.WRatio, limit=k)]
            for p in ipool[:k]:
                if p not in cand_scores:           # keep a stronger fuzzy score if present
                    cand_scores[p] = (int(fuzz.WRatio(skey, norms[p])), "initialism")
 
        if not cand_scores:
            stats["no_match"] += 1
            continue
 
        # auto-accept ONLY a unique strong fuzzy hit (never an initialism guess)
        if fuzzy_auto is not None:
            sc, p = fuzzy_auto
            exact_rows.append({"supplier_clean": sname, **rec(p, "fuzzy_auto", sc, "medium")})
            stats["fuzzy_auto"] += 1
            continue
 
        # else → top-K candidates for the model (score first, then plain-company pref)
        ranked = sorted(cand_scores.items(),
                        key=lambda kv: (-kv[1][0], pref_key(kv[0], skey)))[:k]
        for p, (sc, basis) in ranked:
            cand_rows.append({"supplier_clean": sname, **rec(p, basis, sc, "low")})
        stats["to_model"] += 1
 
    el = time.time() - t_start
    logger_print(
        f"Match end   : {datetime.now():%Y-%m-%d %H:%M:%S}  | "
        f"{n_total:,} rows in {el:.1f}s ({n_total/max(el, 1e-9):,.0f}/s)"
    )
    return pd.DataFrame(exact_rows), pd.DataFrame(cand_rows), stats
 
 
# ──────────────────────────────────────────────
# CLI / main
# ──────────────────────────────────────────────
 
def parse_args():
    p = argparse.ArgumentParser(description="Stage 1 Companies House matcher")
    p.add_argument("--ch", default="Companies_House_companies_list.csv",
                   help="Full CH bulk CSV (default: Companies_House_companies_list.csv)")
    p.add_argument("--ch-parquet", default="ch_slim.parquet",
                   help="Slim CH cache path (default: ch_slim.parquet)")
    p.add_argument("--suppliers", default="supplier_context.csv",
                   help="Suppliers CSV with a supplier_clean column")
    p.add_argument("--review", default=None,
                   help="Optional review CSV to filter to type=Business")
    p.add_argument("--out-exact", default="ch_exact.csv")
    p.add_argument("--out-candidates", default="ch_candidates.csv")
    p.add_argument("--report", default="ch_match_report.txt")
    p.add_argument("--out-suffix", default="",
                   help="Suffix inserted before the extension of the three output files "
                        "(e.g. --out-suffix _v2 -> ch_exact_v2.csv). Leaves v1 intact.")
    p.add_argument("--k", type=int, default=5, help="Max candidates per miss (default 5)")
    p.add_argument("--fuzzy-cutoff", type=int, default=82,
                   help="Min WRatio to keep a candidate (default 82)")
    p.add_argument("--auto-cutoff", type=int, default=95,
                   help="Min WRatio to auto-accept a unique active match (default 95)")
    p.add_argument("--progress-every", type=int, default=1000,
                   help="Print a progress line every N suppliers (default 1000; 0 = off)")
    p.add_argument("--rebuild-cache", action="store_true")
    return p.parse_args()
 
 
def main():
    args = parse_args()
    log = print
 
    # CH cache
    if args.rebuild_cache or not os.path.exists(args.ch_parquet):
        if not os.path.exists(args.ch):
            log(f"ERROR: CH file not found: {args.ch}"); sys.exit(1)
        build_ch_parquet(args.ch, args.ch_parquet, log)
    log(f"Loading CH cache: {args.ch_parquet}")
    ch = load_ch(args.ch_parquet)
    log(f"CH rows: {len(ch):,}")
 
    # suppliers
    sup = pd.read_csv(args.suppliers, dtype=str, encoding="utf-8-sig", low_memory=False)
    if "supplier_clean" not in sup.columns:
        log("ERROR: --suppliers needs a supplier_clean column"); sys.exit(1)
    if args.review and os.path.exists(args.review):
        rv = pd.read_csv(args.review, dtype=str, encoding="utf-8-sig", low_memory=False)
        biz = set(rv.loc[rv.get("type", pd.Series(dtype=str)).fillna("").str.strip()
                         == "Business", "supplier_clean"])
        before = len(sup)
        sup = sup[sup["supplier_clean"].isin(biz)]
        log(f"Filtered to type=Business: {len(sup):,} of {before:,}")
    sup = sup.drop_duplicates(subset="supplier_clean")
    log(f"Suppliers to match: {len(sup):,}")
 
    exact_idx, block_idx, init_idx, alias_idx = build_indexes(ch)
    log(f"Previous-name aliases indexed: {len(alias_idx):,}")
 
    # Materialise the per-row columns the matcher needs as plain lists, then drop
    # the DataFrame BEFORE the long match loop. The indices above and these lists
    # are independent of `ch`, so nothing downstream touches the frame again —
    # holding it would just be several GB of dead weight during the loop.
    cols = {
        "names":  ch["CompanyName"].tolist(),
        "norms":  ch["norm_name"].tolist(),
        "nums":   ch["CompanyNumber"].tolist(),
        "status": ch["CompanyStatus"].tolist(),
        "towns":  ch["RegAddress.PostTown"].tolist(),
        "sics":   ch["sic_raw"].tolist(),
    }
    del ch
    gc.collect()
 
    t0 = time.time()
    exact_df, cand_df, stats = match_suppliers(
        sup, cols, exact_idx, block_idx, init_idx, alias_idx,
        args.k, args.fuzzy_cutoff, args.auto_cutoff, log,
        progress_every=args.progress_every,
    )
    elapsed = time.time() - t0
 
    suffix = args.out_suffix
    def _suf(path):
        if not suffix:
            return path
        base, ext = os.path.splitext(path)
        return f"{base}{suffix}{ext}"
    out_exact, out_cands, out_report = _suf(args.out_exact), _suf(args.out_candidates), _suf(args.report)
 
    for df, path in [(exact_df, out_exact), (cand_df, out_cands)]:
        df.to_csv(path, index=False, encoding="utf-8-sig")
 
    # ── report ──────────────────────────────────
    t = max(stats["total"], 1)
    resolved = (stats["exact_unique"] + stats["exact_active_pick"]
                + stats["exact_prev"] + stats["fuzzy_auto"])
    need_model = stats["homonym_to_model"] + stats["to_model"]
    lines = [
        "Companies House — Stage 1 match report",
        "=" * 44,
        f"suppliers matched      : {stats['total']:,}",
        f"match time             : {elapsed:.1f}s",
        "",
        f"RESOLVED (no model)    : {resolved:,}  ({resolved/t*100:.1f}%)",
        f"  exact unique         : {stats['exact_unique']:,}",
        f"  exact (active/plain) : {stats['exact_active_pick']:,}",
        f"  exact via prev name  : {stats['exact_prev']:,}",
        f"  fuzzy/initials auto  : {stats['fuzzy_auto']:,}",
        f"NEEDS MODEL (cands)    : {need_model:,}  ({need_model/t*100:.1f}%)",
        f"  homonyms→model       : {stats['homonym_to_model']:,}",
        f"  fuzzy/initials→model : {stats['to_model']:,}",
        f"NO MATCH               : {stats['no_match']:,}  ({stats['no_match']/t*100:.1f}%)",
        "",
        f"→ {out_exact}         : {len(exact_df):,} rows",
        f"→ {out_cands}    : {len(cand_df):,} rows  "
        f"({cand_df['supplier_clean'].nunique() if len(cand_df) else 0:,} suppliers)",
        f"  (model calls needed ≈ suppliers in candidates)",
    ]
    report = "\n".join(lines)
    with open(out_report, "w", encoding="utf-8-sig") as f:
        f.write(report + "\n")
    log("\n" + report)
 
 
if __name__ == "__main__":
    main()
 
