#!/usr/bin/env python3
"""
ch_token_index.py — loader for the persisted Companies House token index.

This is the STABLE artifact that downstream analysis scripts import. The index maps
each normalised token to the CH row positions whose `norm_name` contains it, stored in
a compact CSR-style layout so it loads fast and small (no 5.7M re-read, no rebuild):

    vocab[i]                      -> the i-th token (sorted)
    positions[offsets[i]:offsets[i+1]]  -> CH row positions containing vocab[i]
    df(token) = offsets[i+1] - offsets[i]               (document frequency)

Positions are int32 (CH has < 2.1bn rows), offsets int64. Build it once with
build_ch_token_index.py; load it here.

    from ch_token_index import TokenIndex
    idx = TokenIndex.load("ch_token_index.npz")
    idx.df("pertemps")          # how many CH rows contain this token
    idx.postings("pertemps")    # np.int32 array of CH row positions
    "pertemps" in idx           # membership

`positions` index into the SAME row order as ch_slim.parquet, so a position p refers to
ch_slim row p (CompanyNumber, CompanyName, norm_name, ...). Keep the two in sync: if the
parquet is rebuilt, rebuild the index.
"""

import numpy as np

_EMPTY = np.empty(0, dtype=np.int32)


class TokenIndex:
    def __init__(self, vocab, offsets, positions, n_rows):
        self.vocab = vocab            # object/unicode array, sorted
        self.offsets = offsets        # int64, length len(vocab)+1
        self.positions = positions    # int32, concatenated CH row positions
        self.n_rows = int(n_rows)     # number of CH rows the index was built over
        # token -> vocab id. Built once; ~1M entries for the full file.
        self._id = {t: i for i, t in enumerate(self.vocab.tolist())}

    @classmethod
    def load(cls, path):
        z = np.load(path, allow_pickle=True)   # our own trusted artifact
        return cls(z["vocab"], z["offsets"], z["positions"], int(z["n_rows"]))

    def __contains__(self, token):
        return token in self._id

    def __len__(self):
        return len(self.vocab)

    def df(self, token):
        i = self._id.get(token)
        if i is None:
            return 0
        return int(self.offsets[i + 1] - self.offsets[i])

    def postings(self, token):
        i = self._id.get(token)
        if i is None:
            return _EMPTY
        return self.positions[self.offsets[i]:self.offsets[i + 1]]
