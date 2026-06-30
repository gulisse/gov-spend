#!/usr/bin/env python3
"""
build_ch_token_index.py — STEP A: build the CH token index once, cheaply, and persist it.

Why this is a separate step
───────────────────────────
The probe needs an inverted index (token -> CH row positions) so blocking isn't limited
to the first word. Building that naively (pandas .explode() of ~14M (row, token) pairs
then a global argsort) holds several giant intermediates in RAM at once and OOMs a 10 GB
box. This builder does the SAME index without those intermediates:

  • reads ONLY the `norm_name` column from ch_slim.parquet, in batches (never the 2.75 GB CSV);
  • two streaming passes — pass 1 counts document frequency per token, pass 2 fills a single
    preallocated int32 positions array (CSR layout). No explode, no argsort;
  • positions are int32 (4 bytes) not int64, halving the largest array;
  • peak memory ≈ the final index (~tens to low-hundreds of MB), not 2–3× it.

Output: ch_token_index.npz  (vocab, offsets, positions, n_rows) — loaded by ch_token_index.TokenIndex.
Run it once after each matcher run that rebuilds ch_slim.parquet; the probe then loads the cache.

    python build_ch_token_index.py                       # ch_slim.parquet -> ch_token_index.npz
    python build_ch_token_index.py --batch 250000        # smaller batches = lower peak RAM
"""

import argparse
import sys
import time

import numpy as np

try:
    import pyarrow.parquet as pq
except ImportError:
    sys.exit("pyarrow is required (pip install pyarrow)")


def parse_args():
    p = argparse.ArgumentParser(description="Build + persist the CH token index (step A)")
    p.add_argument("--ch-parquet", default="ch_slim.parquet")
    p.add_argument("--out", default="ch_token_index.npz")
    p.add_argument("--batch", type=int, default=500_000,
                   help="Rows per read batch. Lower it if RAM is tight.")
    p.add_argument("--column", default="norm_name",
                   help="Column holding the production-normalised name")
    return p.parse_args()


def iter_norm_batches(pf, column, batch):
    """Yield Python lists of norm_name strings, in stable parquet row order."""
    for rb in pf.iter_batches(batch_size=batch, columns=[column]):
        yield rb.column(0).to_pylist()


def main():
    a = parse_args()
    pf = pq.ParquetFile(a.ch_parquet)
    n_rows = pf.metadata.num_rows
    print(f"CH parquet: {a.ch_parquet}  ({n_rows:,} rows)  column='{a.column}'  batch={a.batch:,}")

    # ── PASS 1: document frequency per token (rows containing it) ──
    t0 = time.time()
    df = {}
    row = 0
    for chunk in iter_norm_batches(pf, a.column, a.batch):
        for s in chunk:
            if s:
                for t in set(s.split()):        # set() => document frequency, not term freq
                    df[t] = df.get(t, 0) + 1
            row += 1
    if row != n_rows:
        print(f"  note: read {row:,} rows (metadata said {n_rows:,})")
    print(f"  pass 1: {len(df):,} distinct tokens in {time.time()-t0:.1f}s")

    # ── vocab + CSR offsets ──
    vocab = np.array(sorted(df), dtype=object)
    tok2id = {t: i for i, t in enumerate(vocab.tolist())}
    counts = np.fromiter((df[t] for t in vocab), dtype=np.int64, count=len(vocab))
    total = int(counts.sum())
    offsets = np.zeros(len(vocab) + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    if total - 1 > np.iinfo(np.int32).max:
        sys.exit("CH has > int32 rows; widen positions dtype to int64")
    positions = np.empty(total, dtype=np.int32)
    cursor = offsets[:-1].copy()                 # write head per token
    del counts, df

    # ── PASS 2: fill positions (same row order) ──
    t1 = time.time()
    row = 0
    for chunk in iter_norm_batches(pf, a.column, a.batch):
        for s in chunk:
            if s:
                for t in set(s.split()):
                    i = tok2id[t]
                    positions[cursor[i]] = row
                    cursor[i] += 1
            row += 1
    print(f"  pass 2: {total:,} postings in {time.time()-t1:.1f}s")

    # sanity: every token's slice fully written
    if not np.array_equal(cursor, offsets[1:]):
        sys.exit("internal error: postings fill mismatch")

    np.savez(a.out, vocab=vocab, offsets=offsets, positions=positions,
             n_rows=np.int64(n_rows))
    mb = (positions.nbytes + offsets.nbytes) / 1e6
    print(f"→ {a.out}  | {len(vocab):,} tokens, {total:,} postings "
          f"(~{mb:.0f} MB arrays) | total {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
