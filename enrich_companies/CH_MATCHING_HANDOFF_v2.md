# Companies House (CH) Matching — Handoff v2

Purpose: hand this to a fresh chat in the **new Claude project linked to the latest git
repo**, so it can continue the CH supplier→Companies-House matching work without
re-deriving anything. It records the data findings, the agreed fixes, what has already
been implemented, and the remaining plan.

---

## 0. CRITICAL cautions — read first

1. **Apply changes to the LATEST files in the repo.** In the previous chat the project
   explorer contained **duplicate** copies of `bt_stp1b_ch_match.py`, and the edits were
   applied to what turned out to be an **older** copy. The code snippets in this doc are
   therefore given as **described logic + exact snippets to re-apply**, not as a
   drop-in file. Diff against the current `normalise()` before pasting.

2. **Never overwrite hand-labelled files.** The user hand-labelled the sample. A prior run
   regenerated `ch_label_sample.csv` and nearly clobbered that work; it was saved only
   because the user had renamed it to **`ch_label_sample_v1.csv`**. Rule going forward:
   **all new outputs get a version suffix** (`_v2`, etc.); never write to
   `ch_label_sample*.csv` or any hand-labelled file.

3. **All `.csv` outputs must be written with the `utf-8-sig` encoding** (BOM header). This
   is a project-wide requirement.

4. Dev environment: VS Code on Ubuntu, local.

---

## 1. What we are doing

Comparing **v1 vs v2** of the CH matcher on the user's hand-labelled sample
(`ch_label_sample_v1.csv`, ~180 rows) to measure recall improvement. `check_labels_v2.py`
scores how the labelled rows are handled by each version.

`check_labels_v2.py` reads the label file (`label` ∈ {Y, N, NC}) and extracts the
"correct" company number from the `note` column via regex (8-digit, or 2-letter+6-digit
e.g. `SC######`, `NI######`, `OC######`).

### Scoring command (step 2 of the plan)

```bash
python check_labels_v2.py \
    --labels ch_label_sample_v1.csv \
    --exact ch_exact_v2.csv \
    --candidates ch_candidates_v2.csv \
    --v1-exact ch_exact.csv \
    --v1-candidates ch_candidates.csv \
    --out label_v1_vs_v2_check.csv
```

- `--labels ch_label_sample_v1.csv` — the hand-labelled rows (the key change; do NOT let
  the script regenerate this file).
- `--exact` / `--candidates` — default to the `_v2` names; optional if they already exist.
- `--v1-exact` / `--v1-candidates` — passing the v1 files enables the **delta summary**
  (before/after count of N-rows resolved).

---

## 2. How to analyse the output — column semantics & filter order

Columns: `probe_scope`, `match_tier`, `data_gap_note`, `verdict`, plus `label`,
`correct_number`.

**`probe_scope` and `match_tier` are v1 diagnostic columns** (they record what the *v1*
matcher did). `verdict` and `data_gap_note` drive the v1→v2 scoring. This is why a
`verdict = fixed_exact` row can legitimately show `probe_scope = no_match` **or**
`candidates` — the two columns come from different versions.

### Step order for analysis

1. **Split by `label`.** `N` = the improvement set (was wrong in v1; did v2 fix it?).
   `Y` = regression check only (the script prints this). `NC` = not a company, ignore.

2. **Read `verdict` on the N rows** (the scoreboard):
   - `fixed_exact` — v2 now resolves deterministically. Best.
   - `fixed_top_candidate` — correct company is v2's rank-1 candidate. Nearly as good.
   - `in_candidates_not_top` — correct company present but not first (ranking could improve).
   - `still_absent` — v2 still misses entirely (remaining recall gap).
   - `no_reference` — labelled N but no company number in `note`; not checkable → exclude.

3. **Split N rows by `data_gap_note`:**
   - `True` — company absent from the CH download → any fix is a **data** fix (dissolved
     merge), not an algorithm fix. Report separately.
   - `False` — company *was* in CH but the matcher missed it → any fix is a genuine
     **algorithm** improvement. This is the number that matters.

4. **Within `data_gap_note == False`, split by `probe_scope`:**
   - `no_match` — v1 produced zero candidates (fixed here means previous-name / prefix /
     cleanup logic rescued it from nothing).
   - `candidates` — v1 produced candidates but the right one was missing/buried (fixed here
     means ranking promoted it).

5. **Within `still_absent`, check `match_tier`** to triage the remaining misses:
   - `clean` — near-perfect token match still missed → likely digit/space or plural/stem gap.
   - `head_modifier` — supplier is the head of the CH name → likely needs previous-name /
     containment logic.
   - `loose` — weak evidence, probably not a real match; low priority.

### Headline metric (the v2 improvement rate)

```
label == "N"
  → verdict != "no_reference"                              # denominator
    → data_gap_note == False                               # algorithm-fixable
      → verdict in {fixed_exact, fixed_top_candidate}      # numerator = v2 wins
```

---

## 3. How `normalise()` works (needed to understand the fixes)

`normalise(name) -> (canonical_key, token_list)` is applied **identically** to
`supplier_clean` and CH `CompanyName` (symmetric — this is important; the two sides must
transform the same way).

Original pipeline: lowercase → strip quotes → `&`→space → punctuation/periods→space
(`NONALNUM_RE`) → split, drop `CONNECTORS` and `SUFFIXES` → strip leading `the` →
collapse runs of single-letter tokens (`f m conway → fm conway`) → (v2 addition) re-filter
collapsed tokens against `SUFFIXES`.

Relevant constants (original):
```python
SUFFIXES = {
    "ltd", "limited", "plc", "llp", "llc", "lp", "cic", "cio", "co", "company",
    "group", "holdings", "inc", "incorporated", "ug", "gmbh",
}
CONNECTORS = {"and"}          # '&' mapped to space first
```

Indexes (`build_indexes`): `exact = ch.groupby("norm_name").indices`,
`blocks = ch.groupby("block_key").indices` (block_key = first token),
`inits = ch.groupby("initials_sig").indices`. `is_active(status)` = `"active" in
status.lower()`.

Exact-match logic in the loop (approx lines 242–259):
- `skey in exact`, pool size 1 → `exact` (high).
- else exactly 1 active in pool → `exact_active` (medium).
- else (multiple/zero active) → **`for p in pos_list[:k]`** emit `exact_homonym` (low)
  candidates. ← NOTE: `pos_list[:k]` takes the first *k* homonyms in **CH file order**
  (arbitrary). This is a real bug — see step 4.

---

## 4. Data findings (the reviewed cases)

### Two initial examples

1. **`LSI` → `L.S.I. LTD` (correctly resolved).** NON-PROBLEM. `L.S.I. LTD` normalises via
   single-letter collapse (`l s i ltd → lsi`) and matches supplier `LSI` exactly. Confirmed
   present in `ch_exact_v2.csv`, correctly assigned. No initialism fix needed for this case.

2. **`Work Works Training Solutions CIC` vs `WORK WORKS TRAINING SOLUTIONS C.I.C.`** —
   ordering bug. The suffix filter ran *before* the single-letter collapse, so `C.I.C.`
   (→ `c i c`) survived the filter, got reassembled to `cic`, and was never re-checked.
   **FIX (implemented): re-filter the collapsed `out` list against `SUFFIXES`.**

### The `still_absent` review table (11 rows) with diagnoses

| supplier_clean | correct_number | data_gap_note | root cause | fix |
|---|---|---|---|---|
| Budget Appliances | 10563689 | FALSE | supplier is a prefix of `BUDGET APPLIANCES OF BECKENHAM LIMITED`; `of` survives as noise | subset/prefix containment (5.3) + add `of` to CONNECTORS |
| Creative Activity Group Ltd | NI669375 | FALSE | should be exact `creative activity` on both sides; likely **stale v1 tier** or homonym ranking | re-check after `group` handling; homonym ranking |
| Whatson | 16654139 | FALSE | `WHATS.ON BRIGHTON LTD` — period→space splits `whats`+`on`; supplier solid `whatson` | delete intra-word period instead of spacing (1) |
| Stace Construction And Property Consulta | 16418225 | FALSE | `STACE CONSTRUCTION SERVICES LTD` — diverges after shared head (property/consultancy vs services); trading vs registered name | distinctive-head rule, candidate-only, weight by token rarity (lowest priority) |
| Next Group Plc | 11118708 | FALSE | strips `group`+`plc` → `next` → thousands of homonyms; `pos_list[:k]` file-order drops the correct one | homonym ranking (5.1) + soften descriptor stripping (5.2) |
| Andy Algar Regen Consult Ltd | 15756202 | FALSE | abbreviations `regen⊂regeneration`, `consult⊂consultancy` | prefix/stem token match, prefix ≥4 (5.4) |
| Million Voices Ltd | 13817883 | TRUE→**stale** | leading `A` in `A MILLION VOICES LTD`; **company IS present** (grep confirmed) — data_gap_note inherited from first run | gated leading-article strip (4); fix stale flag |
| Murray HAY Solicitors | OC436998 | FALSE | `MURRAY HAY LLP` — supplier has extra `solicitors` descriptor; CH ⊂ supplier | subset containment (5.3) + professional-descriptor stopwords in core key (5.5) |
| The Hospitality Company Ltd | 12411407 | FALSE | `THE HOSPITALITY COMPANY (LONDON) LTD` — `(LONDON)` leaves token `london` | geographic stopword in relaxed core key (5.5 / step 3) |
| 361 The Ridge | 13394428 | TRUE | **address fragment, not a name** (small care home, unlisted) — unmatchable by name | EXCLUDE from stats; ignore for now |
| UCS Hampstead | 16336234 | FALSE | `UNIVERSITY COLLEGE SCHOOL`; supplier initials sig `ucsh` (`hampstead` pollutes) ≠ `ucs` | strip `hampstead` as geographic token in core key (step 3) — ties to (2)/(3) |

### Data anomalies to correct in the labels (not the matcher)

- **`Million Voices Ltd` and `361 The Ridge`** were marked `data_gap_note = TRUE` but both
  appear in the CH list (grep confirmed `A MILLION VOICES LTD` and `HOMECARE AND MORE LTD`).
  The flag was **inherited/stale from the first run**. `Million Voices` has since been
  included → use the output to compare first-vs-second run. **`361 The Ridge` is a genuine
  anomaly** (unlisted small care home) → ignore for now, exclude from recall stats.

---

## 5. Verification tooling

- **Confirm a company number is in the CH list** (plain alternation catches both the
  comma-bounded `CompanyNumber` column and the `URI` column, and avoids boundary issues):
  ```bash
  grep -iE '01234567|SC123456|09876543' Companies_House_companies_list.csv
  ```
  Zero-padding matters — CH numbers are 8 chars, often leading-zero padded; include the
  zeros. A hit = present (algorithm gap); no hit = true absence (data gap).

- **Faster: search the slim parquet** (exact, avoids regex boundaries):
  ```bash
  python -c "
  import pandas as pd
  df = pd.read_parquet('ch_slim.parquet', columns=['CompanyNumber','CompanyName'])
  nums = {'01234567','SC123456','09876543'}
  print(df[df['CompanyNumber'].isin(nums)].to_string())
  "
  ```
  Note: the slim parquet projects `CompanyNumber` but **not** `URI` (per `CH_COLS`), so the
  URI concern doesn't apply there — just match the stored zero-padded format.

- **`.gitignore`:** add `*.npz` alongside `*.parquet` — both are regenerable binary caches
  (the token index writes `.npz`).

---

## 6. Agreed fixes — STEP 1 (implemented in the prior chat; RE-APPLY to the latest file)

All step-1 changes are to `normalise()` and its constants. They keep the exact key
**strict and symmetric** (both sides transform identically). Design decision (confirmed):
the fuzzy relaxations in step 3+ go into a **separate relaxed "core" key**, NOT into this
exact key, so exact matching stays strict.

### Constants (replace the originals)

```python
CONNECTORS = {"and", "of"}                 # '&' is mapped to space first
LEADING_ARTICLES = {"the", "a", "an"}      # stripped only when next token is multi-letter
QUOTE_RE = re.compile(r"[\"'`´\u201c\u201d\u2018\u2019]")
# TLDs stripped to a space (longest first so co.uk beats uk); trailing boundary
# so ".com" is not torn out of "commercial".
TLD_RE = re.compile(r"\.(?:co\.uk|org\.uk|ac\.uk|gov\.uk|com|net|org|io|uk)(?=$|[\s,./])", re.I)
INTRAWORD_DOT_RE = re.compile(r"\.(?=\w)")   # whats.on -> whatson,  c.i.c. -> cic
NONALNUM_RE = re.compile(r"[^a-z0-9]+")
```

### `normalise()` body — the changed portions

Order matters: **TLD strip must come before the intra-word period delete**, which comes
before `NONALNUM_RE`.

```python
    s = name.lower()
    s = QUOTE_RE.sub("", s)               # strip quotes
    s = s.replace("&", " ")               # ampersand -> connector slot
    s = TLD_RE.sub(" ", s)                # acme.co.uk / acme.com -> "acme "
    s = INTRAWORD_DOT_RE.sub("", s)       # whats.on -> whatson,  c.i.c. -> cic
    s = NONALNUM_RE.sub(" ", s)           # remaining punctuation -> space
    toks = [t for t in s.split() if t and t not in CONNECTORS and t not in SUFFIXES]
    # strip a leading article, but only when the next token is multi-letter,
    # so real initialisms (A B C -> abc) keep their leading letter
    while len(toks) > 1 and toks[0] in LEADING_ARTICLES and len(toks[1]) > 1:
        toks = toks[1:]
    # ... existing single-letter collapse loop unchanged ...
    # (C.I.C. fix, keep it) re-filter collapsed tokens against SUFFIXES:
    out = [t for t in out if t not in SUFFIXES]
    return " ".join(out), out
```

**Why `.com` vs `.co.uk`:** a blanket intra-word-dot delete would mangle URLs
(`acme.com → acmecom`), so TLDs are stripped to a space first (they drop like a suffix).
`co\.uk` precedes bare `uk` in the alternation so the two-part TLD wins; the `(?=$|[\s,./])`
lookahead prevents `.com` being pulled out of `commercial`.

**Why the leading-article gate:** unguarded stripping of leading `a` would turn
`A B C LTD` (tokens `a b c`) into `bc`. Gating on "next token length > 1" preserves
initialisms while still fixing `A Million Voices → million voices`.

### Regression harness (created: `test_normalise.py`)

15 seeded cases, all passing after step 1: `whats.on→whatson`, `WHATS.ON BRIGHTON→whatson
brighton`, `C.I.C.` both forms → `work works training solutions`, `L.S.I. LTD`/`LSI`→`lsi`,
`acme.com`/`acme.co.uk`→`acme`, `commercial services` untouched (no false TLD strip),
`A Million Voices`/`Million Voices`→`million voices`, `A B C→abc` (initialism preserved),
`Next Group`/`NEXT GROUP PLC`→`next`, `Budget Appliances Of Beckenham→budget appliances
beckenham`. Run: `python test_normalise.py`. Re-seed with any new edge case before each
future change.

---

## 7. Remaining plan — STEPS 2–6 (not yet implemented), in priority order

**Step 2 — Re-run the sample pipeline, then re-score immediately.** Do this *before*
building anything larger; several `still_absent` rows may already resolve, so you don't
build logic for already-fixed rows. This is also the Million-Voices first-vs-second-run
compare.
```bash
# regenerate v2 matcher outputs with the updated normalise()
python bt_stp1b_ch_match.py --suppliers distinct_suppliers_clean.csv \
    --ch ch_slim.parquet --exact-out ch_exact_v2.csv --cand-out ch_candidates_v2.csv
# (adjust flags to the latest script's actual CLI)

# re-score
python check_labels_v2.py \
    --labels ch_label_sample_v1.csv \
    --exact ch_exact_v2.csv --candidates ch_candidates_v2.csv \
    --v1-exact ch_exact.csv --v1-candidates ch_candidates.csv \
    --out label_v1_vs_v2_check.csv
```

**Step 3 — Build a relaxed `core_key()` layer** (agreed architecture: second key alongside
the exact one, consulted only in the candidate stage after an exact miss). A separate
function that additionally strips a **geographic stopword set** (`london`, `uk`,
`hampstead`, `beckenham`, …) and a **professional-descriptor set** (`solicitors`,
`services`, `consultancy`, `training`, …). Index as a third group
`core = ch.groupby("core_key").indices`. Do NOT fold these into the exact key (two firms
differing only by city would falsely merge). One function covers: UCS Hampstead, The
Hospitality Company (London), Budget…of Beckenham, Murray Hay Solicitors — and, via the
initials signature computed on core tokens, the `ucsh≠ucs` problem.

**Step 4 — Rework the homonym block** (`pos_list[:k]`) — **biggest single recall gain.**
Currently takes the first *k* homonyms in CH file order (arbitrary), so `Next Group→next`
(thousands of homonyms) drops the correct company. Rank by **active-first, then fuzzy score
on the full *unstripped* name**, before truncating to *k*. (This is 5.1 + 5.2 — full name
as the homonym tiebreaker.)

**Step 5 — Containment + prefix/stem candidate rules.**
- Subset / contiguous-prefix: if supplier tokens are a subset of (or a contiguous prefix
  of) a CH name → emit as a strong candidate (fixes `Budget Appliances ⊂ …Beckenham`,
  `MURRAY HAY ⊂ Murray Hay Solicitors`). (5.3)
- Prefix/stem token equality: treat two tokens as equal when one is a prefix of the other
  with length ≥ 4 (`regen↔regeneration`, `consult↔consultancy`). (5.4)

**Step 6 — Regression check on the Y rows** (script prints this) so no relaxation knocks
out a previously-correct match.

**Also soften descriptor stripping** (`group`, `holdings`, `company`): they aren't pure
legal forms like `ltd`/`plc`; stripping them collapses distinctive names into common words
and creates the homonym floods. Either keep them in the key or use the full name as the
homonym tiebreaker (covered by step 4).

**Lowest-priority / genuinely hard:** `Stace Construction … → STACE CONSTRUCTION SERVICES`
truly diverges after the shared head (trading name vs registered name) — only a
distinctive-head rule weighted by token rarity catches it, and even then as a candidate,
not an exact.

---

## 8. What to look for to identify improvements (after step 2 re-score)

- **Headline: the delta summary** — count of `label == "N"` rows moving `still_absent →
  fixed_*` between the v1 and v2 runs. This going up is the whole point. **`Million Voices`
  should flip here** (first-vs-second-run confirmation).

- **Rows this step-1 batch targets** — should now be `fixed_exact` / `fixed_top_candidate`:
  `Whatson`, `Work Works … C.I.C.`, `A Million Voices`, `Budget … Beckenham`.

- **`Creative Activity Group`** is the tell for **stale v1 tiers**: if it flips to
  `fixed_exact` with no containment code written yet, `group`-handling already resolved it
  → skip building extra logic for its class.

- **Rows that should STILL be `still_absent` after step 1** (they need steps 3–5):
  `Next Group`, `UCS Hampstead`, `Andy Algar Regen`, `Murray Hay Solicitors`. Unchanged =
  correct, not a failure.

- **Regression watch:** any `label == "Y"` row that drops out. The **leading-article strip**
  and **`of`-as-connector** are the two step-1 changes most likely to cause an unintended
  side effect — check there first. **A clean run = zero Y-row regressions AND a positive
  N-row delta.**

---

## 9. Files referenced

- `bt_stp1b_ch_match.py` — the matcher (contains `normalise()`, `build_indexes()`, the match
  loop). **Apply step-1 edits to the latest repo copy.**
- `check_labels_v2.py` — the v1→v2 scorer.
- `ch_label_sample_v1.csv` — hand-labelled sample (DO NOT overwrite).
- `ch_exact_v2.csv`, `ch_candidates_v2.csv` — v2 matcher outputs.
- `ch_exact.csv`, `ch_candidates.csv` — v1 outputs (for the delta).
- `ch_slim.parquet` — slim CH cache (projects `CompanyNumber`, no `URI`).
- `Companies_House_companies_list.csv` — full CH list (has `URI` column).
- `test_normalise.py` — regression harness (re-seed before each change).
