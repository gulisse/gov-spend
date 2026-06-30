# Companies House Supplier Enrichment — Design Working Doc

**Status:** DRAFT v0.6 — Stage 1 built+calibrated; **Step 2 (disambiguation batch) built** (`bt_stp1c_ch_disambig.py`).
**Self-contained** so a new chat can continue from this file alone.
**Owner:** (you)  ·  **Drafted with:** Claude  ·  **Last updated:** 2026-06-15

**Changelog**
- v0.6 — calibrated on the full 5.7M run: auto-accept DISABLED (WRatio≥95 ~30-50%%
  wrong on eyeball), `--fuzzy-cutoff 92` (85-87 band was noise; 92-band ~2/3 real), K=5 kept.
  Locked flags: `--fuzzy-cutoff 92 --auto-cutoff 101`. Step 2 disambiguation builder written
  (`bt_stp1c_ch_disambig.py`): per-row enum of candidate company_numbers + NONE.
- v0.5 — Stage 1 matcher implemented + validated on synthetic fixtures (every path)
  and the 10k CH sample. Clarified the initials linkage (deterministic candidate gen,
  model disambiguates, auto-accept only when unique+active). Confirmed: no outer
  ThreadPoolExecutor over `cdist(workers=-1)` (oversubscribes).
- v0.4 — made self-contained for handoff. Added: full normalisation rules + worked
  examples; performance/parallelism section; confirmed ONS SIC 2007 reference + join key
  + pseudo-code handling; final output schema (incl. `ch_company_number`, `ch_company_name`);
  environment/validation (local-only; dev on 10k sample).
- v0.3 — spaced-initials normalisation; speed plan (Parquet/DuckDB, not regex); SIC roll-up
  from code structure; ISIC parked; no-address disambiguation + homonyms.
- v0.2 — six-step two-stage process; SIC as 3rd taxonomy dimension.
- v0.1 — initial framing + decision points.

---

## 1. Goal

Improve `final_business_subtype` accuracy **and** add an authoritative industry dimension
by enriching each supplier with **Companies House (CH)** data — SIC codes/text, company
number, official name, status — resolved **deterministically in our own code**, with the
model used only to disambiguate genuinely unclear matches.

Canonical case: **IBM**. Keyword baseline → `Other / unclassified`; CH SIC `62020`
(IT consultancy) → maps to **ICT** and gives the model a real signal.

---

## 2. Settled design choices

- **Bulk file, not the API.** `Companies_House_companies_list.csv` (5,698,277 rows, 2.75 GB)
  carries `CompanyName`, `CompanyNumber`, `CompanyStatus`, `CompanyCategory`,
  `RegAddress.PostTown`, and SIC in `SICCode.SicText_1..4`. No rate limit, offline.
- **In-code, not model-side tools.** Batch can't call APIs; grounding fights `responseSchema`
  + breaks implicit caching. CH-in-code is free + deterministic.
- **Model only resolves the hard matches.** Exact matches + non-companies cost zero. The
  model only ever *picks a record*; it never emits the SIC (so it can't fabricate one).
- **Enrich-all** business suppliers (skip gov/person/charity/redacted/Nan from `bt_stp0`),
  so SIC coverage spans the dataset — still cheap (only fuzzy misses hit the model).
- **D4 = model decides business_subtype with per-row SIC context.** Resolved SIC goes in the
  row's context line, NOT a SIC dictionary per row; cached reference doc unchanged.
- **SIC roll-up from the ONS UK SIC 2007 reference** (see §7), not ISIC (different codes).
- **Calibrated Stage 1 flags (locked): `--fuzzy-cutoff 92 --auto-cutoff 101`.** Auto-accept is
  disabled — WRatio is a candidate generator only; the model makes every match decision.

---

## 3. Three output taxonomy dimensions (the payoff)

1. **Procurement taxonomy** (597 codes) — *what was bought*.
2. **Bespoke business-type** (16 / 85) — *what the supplier is*.
3. **SIC 2007** (5-digit class → group → division → section) — *official registered industry* (NEW).

Dimensions 2 vs 3 disagreeing = automatic **review flag**. SIC blank where not in CH.

---

## 4. Name normalisation  (the heart of matching)

**Applied IDENTICALLY to `supplier_clean` and CH `CompanyName`.** Steps:
1. lowercase; Unicode-fold.
2. strip quote chars (`" " ' ' \` ´`) and leading/trailing stray punctuation (e.g. `!LTD`).
3. replace `/ - .` with space; remove remaining punctuation.
4. **drop connector tokens** `&` and `and` (unifies ampersand / "and" / juxtaposition).
5. strip company-form suffixes (`ltd limited plc llp llc lp cic cio co company group
   holdings`, leading `the`). Configurable list.
6. **collapse runs of single-letter tokens** into one token (`f m` → `fm`).
7. collapse whitespace → the canonical key.

**Handled deterministically → exact match:**

| Variants | Canonical key |
|---|---|
| `F M Conway Ltd` · `FM Conway Limited` · `F.M. Conway` · `F & M Conway` | `fm conway` |
| `Marks & Spencer plc` · `Marks and Spencer` | `marks spencer` |
| `J & B Recycling` | `jb recycling` |
| `"!BIG IMPACT GRAPHICS LIMITED"` | `big impact graphics` |

**NOT handled deterministically → fuzzy/initialism candidates + model (lower confidence):**

| Case | Route |
|---|---|
| `IBM` ↔ `International Business Machines`, `BBC` ↔ `British Broadcasting Corporation` | initialism index → candidates → model |
| `F M Conway` ↔ `Fred Mathew Conway` (initials↔forenames) | initialism index → candidate → model |
| `IBM UK Ltd` ↔ `IBM United Kingdom Limited` (`uk`↔`united kingdom`) | fuzzy |
| misspellings, trading-name ≠ registered name | fuzzy/model |

**On the boundary (clarified v0.5):** `BBC`↔`British Broadcasting Corporation` and `F M Conway`↔`Fred Mathew Conway` are the *same* mechanism — both reduce to the same initials signature (`bbc`, `fmc`) so candidate generation is **deterministic** for both. What is *not* deterministic is auto-accepting one answer (initials are lossy: `fmc` could be `Frank Morris Conway`). Rule: generate the candidate deterministically, **auto-accept without the model only when the match is unique + active**, else hand the candidates to the model. A long shared token (surname `conway`) narrows the signature pool; a bare acronym (`BBC`) cannot be auto-resolved and always goes to the model.

---

## 5. Pipeline — the six steps

### Stage 1 — Entity resolution (supplier → CH record → authoritative SIC)

**Step 1 · Exact match + candidate generation**  *(new, deterministic, local, £0)*
- **In:** `supplier_context.csv` (business suppliers only); the CH Parquet (built from the CSV).
- **One-off build (persisted):** project the ~6 needed CH columns → Parquet; precompute &
  store `normalised_name`, `block_key` (first significant token / trigram), `initials_signature`.
- **Match:**
  - **Exact** hash/DuckDB join on `normalised_name`.
    - *Homonyms (no supplier address to split them):* prefer the single **active** company;
      if multiple active → low confidence, leave SIC blank (don't guess).
  - **Misses → ≤K candidates** pooled & de-duped from: (1) **fuzzy** (blocking + `rapidfuzz`,
    score within block only); (2) **initialism** (acronym tokens → initials-signature index,
    candidates only, never auto-accept); (3) optional token-overlap.
- **Out:** `ch_exact.csv`; `ch_candidates.csv` (each miss → ≤K records with `match_basis`).

**Step 2 · Build the disambiguation batch**  *(new)*
- **In:** `ch_candidates.csv`.
- **Prompt:** pick the candidate that is the *same legal entity*, on **name (primary)** incl.
  acronym/abbreviation/trading-name reasoning; **prefer `active` over dissolved**; council
  spend context only as a last-resort tie-breaker between near-identical names; NOT on
  SIC-fits-activity (circularity guard). **No supplier address available → locality is not a
  signal.** `responseSchema` = enum of candidate `company_number`s + `NONE`, + `confidence`.
  No taxonomy doc → minimal prompt.
- **Out:** `batch_input_ch/*.jsonl` + `ch_keymap.csv`.

**Step 3 · Submit + download**  *(new; reuses `bt_stp3` machinery)* → `batch_results_ch/*.jsonl`.

**Step 4 · Resolve SIC**  *(new, deterministic)*
- **In:** `ch_exact.csv`, `batch_results_ch/*.jsonl`, `ch_keymap.csv`, CH Parquet, ONS SIC ref.
- **Does:** combine exact + model-chosen numbers; read SIC code(s)+text, `company_number`,
  `company_name`, `status` from CH by number (model never emits SIC). Parse code = first 5
  digits of SicText; join to ONS ref for `sic_section`/`sic_division` (§7).
- **Out:** `supplier_sic.csv` (schema in §8).

### Stage 2 — Classification (SIC → business sub-type)

**Step 5 · Inject SIC into context**  *(new, small)* — append resolved SIC to each supplier's
`context_descriptor` (e.g. `Companies House: 62020 IT consultancy activities; status active`).
Per-row only. → `supplier_context_enriched.csv`.

**Step 6 · Existing business-type batch, unchanged** (`bt_stp2`→`bt_stp3`→`bt_stp4`) — model
decides sub-type now with SIC as context; cached reference-doc prefix unchanged (implicit
caching preserved). → `distinct_suppliers_enriched.csv` (+ SIC dimension columns from §8).

---

## 6. Performance — fastest local execution

- **Storage:** Parquet (compressed, columnar, typed) — loads in seconds vs re-parsing 2.75 GB CSV.
- **Engine:** Polars or DuckDB for load + normalisation + exact join — already multi-core &
  vectorised; no manual threading there. (Regex is NOT the accelerator.)
- **Fuzzy residual:** `rapidfuzz` releases the GIL, so a `ThreadPoolExecutor` over the misses
  genuinely parallelises across cores. `rapidfuzz.process.cdist(workers=-1)` multithreads
  internally and is the simplest equivalent. **No outer `ThreadPoolExecutor`** over
  `cdist(workers=-1)` — it would create cores×pool threads and oversubscribe. They are
  alternative ways to use the same cores, not stackable.
- **Build once, persist** the Parquet + indexes (normalised-name / block / initials) → re-runs
  instant. Slim table fits in RAM (~hundreds of MB).

---

## 7. SIC 2007 reference (the ONS workbook)

- File: `publisheduksicsummaryofstructureworksheet.xlsx`, sheet **`reworked structure`**.
- Columns: `Description, SECTION (A–U), Division, Group, Class, Sub Class, Most disaggregated level`.
- **Join key:** CH 5-digit SIC → ONS `Most disaggregated level` (5-digit, e.g. class `6202`
  → `62020`) → gives `SECTION` + `Division` + description hierarchy.
- **Pseudo-codes** in CH not in pure SIC 2007 (`99999` Dormant, `74990` Non-trading,
  `98000/99000` households) → special bucket; `sic_section` = e.g. `"Non-trading/Dormant"`,
  not joined.

---

## 8. Final output schema (columns added)

`supplier_sic.csv` (one row per supplier) and carried into `distinct_suppliers_enriched.csv`:

| Column | Source |
|---|---|
| `ch_company_number` | matched CH record |
| `ch_company_name` | official registered name (differs from `supplier_clean`) |
| `ch_status` | active / dissolved / liquidation … |
| `sic_codes` | all CH SIC codes (`;`-joined) |
| `sic_text` | CH SIC descriptions |
| `sic_division` / `sic_section` | derived via ONS ref (§7) |
| `match_basis` | exact / initialism / fuzzy / model / none |
| `confidence` | high / medium / low |

All CSV outputs **utf-8-sig**.

---

## 9. Still-open / iterating decisions

- **D2 — RESOLVED.** Auto-accept disabled (`--auto-cutoff 101`); fuzzy candidate floor
  `--fuzzy-cutoff 92`; K=5. On the 73,190-supplier run: 51%% exact-resolved, ~17k to the model,
  ~25%% no-match. The model is the sole match decider.
- **K (candidates per miss):** proposed **5**.
- **D3 — SIC→type:** "inform only" (model decides). Add a *small* SIC→type hint table to the
  cached doc only if SIC→type proves inconsistent — never the full ~700-code list.

---

## 10. Cost & runtime

- **CH data cost: £0** (local file). **Model calls = fuzzy misses only.**
- Marginal Gemini cost: pennies on Flash-Lite (tiny disambiguation prompts; Step 6 adds one
  short SIC line per row; cached prefix unchanged).

---

## 11. Risks / edge cases

- **Name matching** — dominant risk; mitigated by symmetric normalisation + exact-first +
  model disambiguation + strict confidence.
- **Homonyms, no supplier address** — can't split same-name companies by location; prefer
  single active, else low-confidence/blank.
- **Acronym noise** — initialism candidates capped at K, never auto-accepted.
- **Circularity** — never pick a record because its SIC fits expected activity (Step 2 guard).
- **No CH presence** — sole traders, persons, overseas, trading names → SIC blank.
- **Multiple SICs** — store all; pick a primary for hierarchy navigation.
- **Stale snapshot** — monthly bulk file; acceptable.

---

## 12. Environment & validation

- **Local-only:** the full 2.75 GB CH file cannot be uploaded; all runs happen on your machine.
- **Dev/validation here uses the 10k sample** `Companies_House_companies_list_sample.csv` for
  correctness; you run the real file locally (CH path is a CLI arg).
- **Load quirks:** strip whitespace from CH header names (`" CompanyNumber"`); names have
  leading junk (`!LTD`) handled by normalisation.

---

## 13. Out of scope (v0.4)

Officers, PSC, filing history, streaming API, full ISIC crosswalk. SIC + status + name + number only.

---

## 14. Rejected alternatives (for the record)

- **Model calls CH directly (function calling) in batch** — impossible; no tool loop.
- **Grounding in batch** — unconfirmed, fights `responseSchema`, per-query fee, kills caching.
- **CH REST API as primary** — rate-limited, two calls/supplier; optional fallback only.
- **ISIC Rev 4 as the SIC reference** — different codes from UK SIC 2007; not interchangeable.
- **Deterministic-only matching** — brittle on messy/acronym names; model handles the residual.
- **"Compressed regex" to speed matching** — regex isn't the lever; columnar + hashing +
  blocking + rapidfuzz is.

---

## 15. Build order (for the new chat)

1. **Stage 1 matcher** (`bt_stp1b_ch_match.py`) — **DONE, calibrated** (`--fuzzy-cutoff 92 --auto-cutoff 101`). CH→Parquet,
   normalise both sides, exact join, candidate generation (fuzzy + initialism), emits
   `ch_exact.csv` + `ch_candidates.csv` + `ch_match_report.txt`. **Next: run on the full
   5.7M file, upload `ch_match_report.txt` to calibrate D2 (cutoffs) and K.**
2. **Disambiguation batch** (`bt_stp1c_ch_disambig.py`) — **DONE.** Per-supplier prompt + per-row
   enum of candidate company_numbers + NONE; rules in cached systemInstruction; emits
   `batch_input_ch/*.jsonl` + `ch_keymap.csv`. **Next: submit (reuse bt_stp3), then Step 4 resolve.**
3. **Submit/download** — reuse `bt_stp3` (separate GCS prefix/manifest/results dir).
4. **Resolve SIC** (`bt_stp1d_ch_resolve.py`): → `supplier_sic.csv` (+ ONS roll-up).
5. **Inject context** (`bt_stp1e_inject_sic.py`): → `supplier_context_enriched.csv`.
6. Run existing `bt_stp2`→`bt_stp4` on the enriched context.
