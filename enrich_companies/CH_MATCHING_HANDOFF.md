# Companies House Supplier-Enrichment — Chat Handoff

**Purpose:** continue the CH supplier-matching sub-project (part of the business-type
workstream) in a new chat without losing context. Read this top-to-bottom first.

---

## 0. One-paragraph status

Stage 1 matcher (`bt_stp1b_ch_match.py`) was calibrated (`--fuzzy-cutoff 92 --auto-cutoff 101`).
We built a **recall diagnostic** (`diagnose_ch_recall.py` + a streamed token-index builder),
ran it, hand-labelled a 180-row sample, and used the labels to drive concrete matcher
improvements. We implemented three deterministic improvements to the matcher
(**previous-name aliases**, **homonym/adorned-variant ranking with a plain-company
auto-resolve**, **supplier-side match-only cleanup**), plus an upstream payment-prefix
cleaner. **Tasks A and B are now BUILT (this is the change since the doc was first written):**
`bt_stp3_submit.py` is parameterised with `--pipeline {bt,ch}` (bt unchanged) for the
disambiguation submit (Task A, §8), and `bt_stp1d_ch_resolve.py` is built and tested to roll
the deterministic + model-chosen `company_number`s into `supplier_sic.csv` with full SIC
rollup (Task B, §9). Gemini Flash 2.5 is the disambiguator that picks from the
deterministically-sorted candidate list.

**WHAT REMAINS (none of it blocking the end-to-end run):**
- Run the v2 pipeline end-to-end (the ordered commands are in §11 — START HERE).
- Deferred matcher recall item: **fuzzy-on-previous-name** (the `Handicare`-type head-token
  renames) — §10 item 1. Optional but the highest-value remaining recall gain.
- Optionally wire `clean_for_match()` into the probe's supplier side so the v2 recall read
  matches the matcher exactly (§10 item 6).

**STATUS LEGEND for this doc:** §4 = DONE (matcher edits). §6/§7 = DONE (diagnostic).
§8 = DONE (Task A built). §9 = DONE (Task B built). §10 = remaining/optional. §11 = the
run order to execute next.

---

## 1. Environment / conventions (carry forward)

- Dev: VS Code on Ubuntu (WSL), local machine 16 GB RAM; **a separate 10 GB-RAM VM** is used
  for some runs and is memory-constrained (this drove the streamed index design — see §6).
- All CSV outputs use **`utf-8-sig`**.
- Data scale: `normalized_spend.csv` ~7M rows; `Companies_House_companies_list.csv` ~5.7M rows
  (~2.75 GB). The user **already merged dissolved companies into** `Companies_House_companies_list.csv`.
- `supplier_clean` is the **immutable join key** for the whole downstream Gemini workstream
  (`enrich_stp*` taxonomy + `bt_stp*` business-type/CH). It is created **upstream** (in the
  spend-normalisation step that builds `normalized_spend.csv`), NOT in any bt_/enrich_ script.
  → **Do not regenerate `normalized_spend.csv` for CH-matching reasons** — it breaks every
  downstream lookup keyed on `supplier_clean`. (This is why CH-specific cleanup is applied
  match-only/in-memory — see §4C.)
  → **Planned: `supplier_display` column.** A post-processing step (after all pipelines
  complete) will add a user-facing `supplier_display` column with payment-gateway prefixes
  stripped via `clean_supplier_prefixes.py`'s `strip_prefixes()`. `supplier_clean` remains
  the join key; `supplier_display` is for reporting/UI only. On the next full reprocessing
  of `normalized_spend.csv`, fold `strip_prefixes()` into `normalize_boroughs.py` so new
  `supplier_clean` values are clean from the start.
- Business universe for CH matching = `supplier_context.csv` ∩ (`type==Business` in
  `distinct_suppliers_review_updated.csv`) = **73,190 suppliers**.
- The calibrated matcher command actually run:
  ```
  python bt_stp1b_ch_match.py --suppliers supplier_context.csv \
      --review distinct_suppliers_review_updated.csv --fuzzy-cutoff 92 --auto-cutoff 101
  ```
  (The script the user runs is the mem-optimised one, **renamed to `bt_stp1b_ch_match.py`** — so
  the in-repo `bt_stp1b_ch_match.py` IS the production version.)

### Key data-file gotchas (confirmed)
- `distinct_suppliers_review_updated.csv`.`amount` carries **thousands separators**
  (`"1,846,000,076.76"`). Must `str.replace(",","")` before `to_numeric`, or it silently
  parses only sub-£1,000 values (we hit this — it reported 0 suppliers >£50k until fixed).
  After fixing: 24,115 suppliers >£50k overall; **23,125 within the Business universe**.
- `business_type` column in that review file is the **authoritative** classification
  (categories like `School / education` 4,685, `Nursery / childcare` 1,784,
  `Charity / voluntary` 2,383, `Religious organisation` 504, `Other / unclassified` ~41,773).
  Use it (joined on `supplier_clean`), not name-keyword guessing, for entity segmentation.
- The matcher's production `ch_slim.parquet` was confirmed **NOT stale** — its `norm_name`
  already has ltd/limited/plc/holdings/group stripped (0 occurrences). So suffix asymmetry
  is NOT the bug (see §5 diagnosis).

---

## 2. Pipeline / file map

**Already built & calibrated (pre-existing):**
- `bt_stp1b_ch_match.py` — Stage 1 matcher (exact / fuzzy WRatio / initialism). **EDITED — see §4.**
- `bt_stp1c_ch_disambig.py` — builds the disambiguation batch: `batch_input_ch/batch_input_ch[_NNN].jsonl`
  + `batch_input_ch/ch_keymap.csv`. Each JSONL row: `key=SUPCH_N`, self-contained `request`
  with `systemInstruction` embedded, per-row `responseSchema` = enum of that supplier's candidate
  `company_number`s + `NONE`, plus a `confidence` enum. (No injection needed at submit.)
- `bt_stp3_submit.py` — generic Gemini batch submit/poll/download (built for the business-type
  pipeline). Hardcoded bt-specifics: `GCS_PREFIX_BT="business_type_enrichment"`, input dir/glob
  `batch_input_bt*.jsonl`, results dir, manifest, display-name `bt_enrich_{file}`, result stem
  `batch_results_bt_{NNN}.jsonl`, and a bt-specific `summarise()` parsing STATUS/BUSINESS_SUBTYPE.

**Built (delivered to outputs):**
- `bt_stp1b_ch_match.py` — matcher with the §4 changes (prev-name aliases, ranking + plain
  auto-resolve, match-only `clean_for_match()`, `--out-suffix`).
- `bt_stp3_submit.py` — **parameterised with `--pipeline {bt,ch}`** (Task A; bt unchanged).
- `bt_stp1d_ch_resolve.py` — **NEW; built + tested** (Task B): merges deterministic + model
  picks, rolls SIC up to all levels → `supplier_sic.csv`.
- `diagnose_ch_recall.py` — recall/missed-match diagnostic (probe + diff tables + label sheet).
- `build_ch_token_index.py` — streamed, memory-lean inverted token index builder (step A).
- `ch_token_index.py` — `TokenIndex` loader class (stable artifact later scripts import).
- `clean_supplier_prefixes.py` — standalone payment-prefix stripper (reference, `amzn` removed;
  logic now also inlined into the upstream normaliser and into the matcher's match-only key).
- `CH_MATCHING_HANDOFF.md` — this file.

**Remaining (optional, non-blocking):**
- **Fuzzy-on-previous-name** matching (the `Handicare`-type head-token renames) — §10 item 1.
- Wire `clean_for_match()` into the probe's supplier side (§10 item 6).

---

## 3. The matcher's deterministic logic & **critical sequencing**

`bt_stp1b_ch_match.py` decides each supplier in this **strict order** (first hit wins, `continue`):

1. **Supplier-side match-only clean** (NEW, §4C): `clean_for_match(supplier_clean)` →
   `normalise()` → `skey`(canonical key), `stoks`(token list), `sig`(initials), `blk`(block key).
   *CH side is normalised identically at parquet-build time; this clean is supplier-only.*

2. **Exact (current name)**: `skey in exact` (exact = `norm_name → row positions`).
   - 1 row → resolve `exact` (high).
   - else homonyms → **rank** (§4A) → if exactly 1 active → `exact_active`; elif exactly 1
     active-and-non-adorned → `exact_active`/_plain auto-resolve; else ranked homonyms (top-k)
     → candidates `exact_homonym`.

3. **Exact (previous-name alias)** (NEW, §4B): elif `skey in alias_exact`
   (`norm(previous name) → current row`). Same unique/active/plain/homonym handling, basis
   `exact_prev*`. Resolves to the **current** company (same company_number).

4. **Fuzzy** (only if no exact/alias hit): block on first token `blk`, score `WRatio`,
   `score_cutoff = fuzzy_cutoff(92)`, `limit=max(k,5)`. A unique active hit ≥ `auto_cutoff(101,
   i.e. effectively disabled)` would auto-accept; otherwise candidates.

5. **Initialism**: shared `initials_sig`; narrowed by a shared long token (surname anchor) or
   ranked by WRatio; never auto-accepted; adds to candidate pool.

6. If candidate pool non-empty → **rank** (score desc, then plain-company `pref_key` tiebreak,
   §4A) → top-k candidates to model. Else → `no_match`.

**Why sequencing is critical:** exact/alias short-circuit before fuzzy, so a clean exact never
gets polluted by fuzzy noise. Ranking happens **before** the top-k truncation in both the
homonym branch and the fuzzy branch — this is what lets the correct (plain, active) entity
*survive the cut* and lead the list the model sees. The supplier-side clean must run before
`normalise()` (else `stk`/domain tokens corrupt `skey`/`blk`/`sig`).

---

## 4. Matcher changes IMPLEMENTED this chat (in `bt_stp1b_ch_match.py`)

All three are deterministic, tested on the 10k sample, backward-compatible, and gated so v1
behaviour is unchanged when new inputs/flags are absent.

### 4A. Homonym / adorned-variant **ranking** + plain auto-resolve  (feedback item "3")
- `ADORN_WORDS = {holdings, holding, group, international, global, investments, investment,
  ventures, trustee(s), nominee(s), pension(s), secretarial, parent}`; `_is_adorned(raw_name)`
  also True if raw name contains `(` (parenthetical).
- `pref_key(pos, skey)` (lower = better): `(active? 0:1, adorned? 1:0, extra_norm_tokens,
  raw_name_token_count)`.
- Applied: in the exact/alias homonym branch, **rank candidates with `pref_key` before the
  `[:k]` cut**; if exactly one active **and** non-adorned candidate exists → **auto-resolve**
  to exact (basis `*_plain`). Same `pref_key` as a **secondary sort** on the fuzzy candidate
  list (primary = score desc).
- Verified: for `telensa` homonyms {`TELENSA LTD`,`TELENSA HOLDINGS LIMITED`,`TELENSA (UK) LIMITED`}
  → ranks `TELENSA LTD` first and auto-resolves it.

### 4B. Previous-name **aliases**  (feedback item "4")
- `CH_COLS` now includes `PreviousName_1..10.CompanyName`; `PREVNAME_COLS` constant added.
- `build_indexes()` now returns a 4th dict `alias_exact` = `norm(previous name) → [current row
  positions]` (vectorised: stack the prev-name cols, normalise only distinct non-empty values,
  group by norm; skips entries equal to the current norm).
- Matcher checks current-name exact first, then `alias_exact` (basis `exact_prev*`); resolves to
  the **current** legal entity / company_number.
- **Requires rebuilding the parquet** (old `ch_slim.parquet` lacks the prev-name columns).
- Sample: indexed 1,983 aliases; +23 exact-via-prev matches; current-name behaviour unchanged.
- **Known limitation:** only *exact-on-previous-name* is handled. Cases where the supplier is a
  *head token* of a former name (e.g. `Handicare` → former `HANDICARE ACCESSIBILITY LIMITED`,
  now `SAVARIA LIFTS (UK) LTD`) need **fuzzy-on-previous-name** — NOT yet built (see §10).
- **Caveat:** a prev-name match can resolve to a renamed husk (current name like `03047144
  LIMITED`). Same company_number/SIC still valid; optionally prefer active for `exact_prev`
  (not yet done — flag for decision).

### 4C. Supplier-side **match-only cleanup** (in-memory; feedback items "1" symptom + "3" cleaning)
- `clean_for_match(name)` defined right after `normalise()`. Strips:
  - leading payment-statement prefixes `_PAY_PREFIX_RE` = `^(stk\*|wp[-*]|sp[\s*]+|sq\s*\*|
    iz\s*\*|ztl\*|paypal\s*\*|pp\*|sumup\s*\*?|gocardless\s*\*?)` (start-anchored, stackable).
    **`amzn mktp` was explicitly removed** per user.
  - trailing web-domain + anything after: `_DOMAIN_TRAIL_RE` = `\.(co\.uk|org\.uk|com|co|org|
    net|io|biz|info)\b.*$` — `\b`-bounded so `.construction`/`.company`/`St. John` are safe.
- Wired into the **single** supplier-side call: `normalise(clean_for_match(sname))`.
- **In-memory only — NOTHING written.** Output rows still carry the original `supplier_clean`
  (`sname`) verbatim, so all downstream joins are intact. CH side is NOT cleaned this way.
- Verified no over-fire: `ACO.Construction Ltd`, `St. John Ambulance`, `Sports Direct`,
  `Asparagus SP Ltd`, `SPV Holdings` all untouched. Sample exact_unique 29→30.

### 4D. `--out-suffix` flag  (feedback item "5")
- Inserts a suffix before the extension of the 3 outputs (e.g. `--out-suffix _v2` →
  `ch_exact_v2.csv`, `ch_candidates_v2.csv`, `ch_match_report_v2.txt`). Leaves v1 intact.
- Report now also counts `exact_prev`.

### Upstream cleaner (separate, user already applied to `normalize_boroughs_X17.py`)
- The same payment-prefix logic (minus `amzn mktp`) was added to `clean_supplier()` in
  `normalize_boroughs_X17.py` as the **LAST step** (operating on `cleaned`, just before its
  return), to avoid disturbing other logic / later lookup tables: insertion point is the
  `GATEWAY_CLEAN_RE` area (line ~280) for `_PAYMENT_PREFIX_RE`, and the end of `clean_supplier()`.
  **NOTE:** the user decided NOT to regenerate `normalized_spend.csv` from this (would break
  downstream keys), hence the match-only approach in 4C is the operative one for CH now.

---

## 5. Diagnostic findings — WHY matches were failing (drives the strategy)

We built `diagnose_ch_recall.py`, ran it, and hand-labelled 180 pairs (`ch_label_sample.csv`).
Labels: **48 Y, 121 N, 11 NC**. Adjusted precision per confidence tier: **clean 57%,
head_modifier 25%, loose 6%** (loose confirmed junk → never auto-trust).

Of the 121 "N": **78 named the correct CH entity** in notes. Running that against the *real*
`ch_exact.csv`/`ch_candidates.csv` gave the decisive split:
- **34 "in ch_candidates"** — production already surfaced the right company; it's in the list for
  the model. **Pure ranking problem** → fixed by §4A (rank before the k-cut + plain auto-resolve).
- **44 "ABSENT"** (true recall misses), of which ~9 are dissolved/not-in-download (now fixed by
  the user's dissolved-companies merge), leaving ~35 findable-but-missed → addressed by §4B
  (previous names), §4C (prefix/domain cleanup), and normalisation gaps (digit/space:
  `Catch22`≠`catch 22`; plural: `eplatform`≠`eplatforms`).

**Root-cause buckets (named):**
1. *Adorned/homonym mis-pick* — both `TELENSA LTD` and `TELENSA HOLDINGS LIMITED` → `telensa`;
   arbitrary pick / k-cap truncation dropped the right one. → §4A.
2. *Previous-name* renames. → §4B (exact) + §10 (fuzzy, TODO).
3. *Payment prefixes / domain trailing*. → §4C.
4. *Digit/space & plural normalisation* (`Catch22`, `eplatforms`). → **TODO** (see §10).
5. *Dissolved / not in download*. → fixed by user's data refresh.
6. *Genuinely non-CH* (NC=11: charities, FCA-register, train stations, airports, foreign SaaS).
   → entity segmentation (`business_type`); some kept categories (Charity/voluntary, Housing
   provider) are partly non-CH → revisit.

**Key methodological insight:** the probe's relaxed `token_set_ratio` over-fires on short/
single-token suppliers (subset → 100). The fix in the diagnostic was the **distinctive-anchor
gate** + the **match-tier** system (see §7). But the *probe's pick ≠ production's candidate set*
— the probe is a recall *discovery* tool, not a measure of production candidate quality. Always
confirm against the real `ch_exact.csv`/`ch_candidates.csv`.

---

## 6. The recall diagnostic suite (built this chat)

### Memory problem & fix (critical for the 10 GB VM)
The naive token index (pandas `.explode()` of ~14M (row,token) pairs + global `argsort`) OOM'd
the VM. Fix = **two scripts**:
- `build_ch_token_index.py` (**step A, run once**): reads ONLY `norm_name` from the parquet in
  batches; two streaming passes (pass1 = document-frequency per token; pass2 = fill one
  preallocated **int32** CSR `positions` array); persists `ch_token_index.npz`
  (`vocab`, `offsets`, `positions`, `n_rows`). No explode, no argsort; peak RAM ≈ the index.
- `ch_token_index.py`: `TokenIndex` loader (`.df(token)`, `.postings(token)`, `in`). **This is the
  stable artifact downstream analysis scripts import.** Positions index the SAME row order as
  `ch_slim.parquet` — rebuild the index whenever the parquet is rebuilt (loader checks n_rows).

### `diagnose_ch_recall.py` (the probe)
- Reuses `bt_stp1b_ch_match.normalise()` so supplier-side cleaning is byte-identical to the CH
  `norm_name` in the parquet (essential for valid diff analysis).
- Reconstructs the `no_match` set (universe − exact − candidates; the matcher never writes it).
- Two scopes, run consecutively, tagged `probe_scope`: `no_match` (gave-up) and `candidates`
  (re-probe; `new_vs_production` flags finds not in production's candidate list).
- Relaxed blocking on the **rarest** supplier tokens (inverted index), `token_set_ratio`, no
  production cutoff → surfaces plausibly-missed CH records.
- **Segmentation** uses `business_type` (joined on `supplier_clean`); default-excludes
  `School / education, Nursery / childcare, Religious organisation` via `--exclude-business-types`
  (Charity NOT excluded — charitable companies/CIOs are on CH). Excluded list written to
  `ch_segmented_out.csv`.
- **match_tier gate** per pair (see §7).
- Outputs (utf-8-sig): `ch_no_match.csv`, `ch_segmented_out.csv`, `ch_probe_pairs.csv`,
  `ch_probe_report.txt`, and `ch_label_sample.csv` (stratified scope×tier, empty `label`/`note`).
- Run: `python diagnose_ch_recall.py --suppliers supplier_context.csv
  --review distinct_suppliers_review_updated.csv --min-amount 50000 --label-sample 180`.

### Bug caught & fixed during build
A name collision: the report's random-sample loop did `idx = rng.choice(...)`, **clobbering the
`idx` TokenIndex object**, which silently zeroed the candidates-scope pass when running both
scopes. Renamed to `sample_ix`. (Lesson: watch variable shadowing of the index object.)

---

## 7. The confidence tiers & label vocabulary (deterministic gate logic)

`match_tier(supplier_tokens, ch_tokens, anchor_df, anchor_max_df)` — stem-aware, returns:
- **clean** — distinctive content matches exactly; all extras are generic/legal/numeric.
- **head_modifier** — supplier fully explained AND shares the *leading* distinctive token; CH
  adds distinctive modifiers after it (`Pertemps → PERTEMPS NETWORK GROUP`).
- **loose** — a distinctive token matches but each side keeps unexplained distinctive tokens
  (the `Mace → ALIBAY-MACE` false-positive class). ~6% precision → do not auto-trust.
- **weak** — no distinctive shared content.
Helpers: `_distinctive(t)` (not generic/residue/number, len>1); `_stem(t)` folds plurals
(`energies→energy`, `companies→company`); `GENERIC_WORDS`, `EXPANSION_RESIDUES`
(plc→public, cic→community interest, cio→charitable organisation, llp→liability partnership).
Anchor gate: rarest shared token must appear in ≤ `--anchor-max-df` CH rows (kills shared-common-
word coincidences like sharing only "management").

**Label vocabulary used (for the oracle / calibration):**
- `Y` = this exact CH record is the supplier; `N` = wrong; `?` = unsure; `NC` = not a company.
- Notes optionally carry the correct CH name/number. The correct-number-in-note cross-check
  against real exact/candidates is the precision-measurement method.

The diff/rule-family flags per pair (for sizing fixes): `cat_abbrev_expansion`,
`cat_trailing_generic`, `cat_plural_stem`, `cat_prefix_abbrev`, `cat_extra_token_only`,
`cat_block_miss`, + `primary_category`. Validated: Pertemps→trailing_generic, Cambridge
Road Estate→trailing_generic, Total Energies→plural_stem, British Telecom/Bromley CIC→
abbrev_expansion.

---

## 8. Task (A): submit the CH disambiguation batch — DONE (`bt_stp3_submit.py` parameterised)

**Goal:** run the candidates through Gemini Flash 2.5 to pick the right `company_number` from
the deterministically-sorted candidate list.

**What was built (decisions all resolved):**
- `bt_stp3_submit.py` is now **parameterised with `--pipeline {bt,ch}`** (default `bt` =
  byte-for-byte unchanged). A `PROFILES` dict carries {input dir+glob, results dir+stem,
  manifest, GCS prefix, display-name prefix, manifest label, summariser}. New usage notes were
  ADDED to the docstring alongside the existing ones (nothing removed).
- ch profile: input `batch_input_ch/`, glob `batch_input_ch*.jsonl`, GCS prefix
  `companies_house_enrichment`, results `batch_results_ch/`, stem `batch_results_ch_{NNN}.jsonl`,
  manifest `ch_batch_jobs_manifest.json`, display-name `ch_disambig_{file}`.
- Added `summarise_ch()` (renamed bt's to `summarise_bt()`): parses each row's
  `{company_number, confidence}`, counts resolved-vs-NONE and HIGH/MEDIUM/LOW.
- Fixed the hardcoded-GCS-prefix collision: `upload_to_gcs()` now takes a `gcs_prefix` param
  (its `prefix` arg remains the log-line prefix); `download_results()` takes a `results_stem`;
  `Manifest` takes a `pipeline` label; `process_job()` takes the `profile`.
- **Decision (resolved): use the EXISTING one-process submit-poll-download** (no resume/
  download-from-manifest mode added). `--submit-only` still exists and works for both pipelines.
- Run: `python bt_stp3_submit.py --pipeline ch`  (bt runs unchanged: `python bt_stp3_submit.py`).

**CRITICAL SEQUENCING:** the disambiguation batch must be (re)built from the **improved
candidates** (`bt_stp1c_ch_disambig.py` over `ch_candidates_v2.csv`) — `bt_stp1c` only includes
suppliers that *have* candidates, and the §4A ranking sets the **order** Flash sees. So:
**re-run matcher (v2) → rebuild disambig batch → submit.**

---

## 9. Task (B): `bt_stp1d_ch_resolve.py` — DONE (built + tested)

Built and tested on synthetic exact/candidates/results/keymap against the **real** ONS workbook.
- Reads the model's `company_number`s from `batch_results_ch/*.jsonl`, reconciles via
  `batch_input_ch/ch_keymap.csv` (`SUPCH_N` → supplier_clean). Robust key/answer extraction
  (`key`/`custom_id`/`request.key`; response→candidates[0]→content→parts→text JSON).
- Joins back to `ch_exact_v2.csv` (deterministic finals, take precedence) and
  `ch_candidates_v2.csv` (look up name+`sic_raw` for the model's chosen number) — **no 5.7M re-read.**
- **SIC output decision (resolved): ALL LEVELS, PRIMARY SIC ONLY.** Primary SIC = first segment
  of the `"; "`-joined `sic_raw`. Rolls up via ONS: `code[:4]`=class, `code[:2]`=division,
  division→section. Emits `sic_code, sic_description, sic_class, class_description, sic_division,
  division_description, sic_section, section_description`, plus `sic_count` (how many SIC codes
  the company listed) and `sic_raw` (trace). One row per `supplier_clean`.
- `NONE`/error → written as `resolution_method = model_none / model_error` with blank SIC, so the
  table accounts for everyone who had candidates. Deterministic rows carry their `match_basis`
  (`exact`, `exact_prev`, `*_plain`) as `resolution_method`; model rows carry `model`.
- Output: `supplier_sic.csv` (utf-8-sig), keyed on `supplier_clean` for a clean join back into
  `enrich_stp*`/`bt_stp*`.
- Run: see §11 step 5.
- NOTE: ONS "reworked structure" sheet → columns `Description, SECTION, Division, Group, Class`;
  `SECTION` has a leading space (stripped); levels use `"na"` where N/A; Class is 4-digit. CH
  5-digit codes truncate cleanly to class/division; non-standard codes (e.g. `99999` dormant)
  come back with blank descriptions rather than erroring.
- NOTE: `supplier_sic.csv` contains only suppliers that were resolved or sent to the model; the
  pure `no_match` set (no SIC obtainable) is intentionally excluded — pull it from `ch_no_match.csv`.

---

## 10. Deterministic-vs-model strategy (the through-line) & remaining determinism TODOs

**Strategy:** maximise *deterministic* resolution (exact, previous-name, plain-homonym auto-
resolve) and, for the residual, hand Gemini Flash 2.5 a **small, deterministically-sorted**
candidate list (plain/active first) so its pick is easy and cheap. The ranking (§4A) is therefore
not just cosmetic — it both rescues truncated candidates AND front-loads the model's choice.

**Remaining deterministic improvements identified but NOT yet built:**
1. **Fuzzy-on-previous-name** — feed previous-name norms into the block/initials/fuzzy paths (not
   just exact), to catch `Handicare`→`HANDICARE ACCESSIBILITY` (supplier = head of former name).
   Needs a search-space refactor (entries carry their own search-norm but resolve to the canonical
   row) so scoring uses the alias norm. Highest-value remaining recall item.
2. **Digit/space normalisation** — `Catch22` vs `Catch 22`, alphanumeric run splitting.
3. **Plural/stem on the matcher side** — the diagnostic stems (`energies→energy`); the matcher's
   `normalise()` does not. Consider folding stemming into matching (carefully — affects exact keys).
4. **`exact_prev` active-preference** — optionally avoid resolving to dissolved renamed husks.
5. **Initials/mnemonic** handling was noted as wonky (LSI, CGP, ASHI, DS) — revisit if material.
6. Optionally apply `clean_for_match()` to the **probe's** supplier side too (it imports from the
   matcher; one-liner) so the v2 recall read reflects the same cleaning.

---

## 11. The exact v2 run sequence (do this next, in order) — START HERE

```
# 0. update project with the latest scripts; then:
pip install google-genai google-cloud-storage openpyxl pandas rapidfuzz pyarrow
gcloud auth application-default login          # needed only for step 4 (submit)

# 1. Rebuild matcher v2 — parquet WITH previous-name cols (dissolved already merged in the CSV)
python bt_stp1b_ch_match.py --ch Companies_House_companies_list.csv \
    --ch-parquet ch_slim_v2.parquet --rebuild-cache \
    --suppliers supplier_context.csv --review distinct_suppliers_review_updated.csv \
    --fuzzy-cutoff 92 --auto-cutoff 101 --out-suffix _v2
#   → ch_slim_v2.parquet, ch_exact_v2.csv, ch_candidates_v2.csv, ch_match_report_v2.txt

# 2. (optional) Rebuild token index + re-probe on v2, pinned to the SAME 180 labels
python build_ch_token_index.py --ch-parquet ch_slim_v2.parquet --out ch_token_index_v2.npz
python diagnose_ch_recall.py --ch-parquet ch_slim_v2.parquet --token-index ch_token_index_v2.npz \
    --exact ch_exact_v2.csv --candidates ch_candidates_v2.csv \
    --suppliers supplier_context.csv --review distinct_suppliers_review_updated.csv \
    --min-amount 50000 --label-sample 180

# 3. Build the disambiguation batch from the v2 candidates
python bt_stp1c_ch_disambig.py --candidates ch_candidates_v2.csv \
    --context supplier_context.csv --batch-size 10000
#   → batch_input_ch/batch_input_ch*.jsonl + batch_input_ch/ch_keymap.csv

# 4. Submit to Gemini Flash 2.5 (one-process submit-poll-download)
python bt_stp3_submit.py --pipeline ch
#   → batch_results_ch/batch_results_ch_*.jsonl + ch_batch_jobs_manifest.json

# 5. Resolve deterministic + model picks into supplier_sic.csv (all SIC levels, primary only)
python bt_stp1d_ch_resolve.py --exact ch_exact_v2.csv \
    --candidates ch_candidates_v2.csv --results "batch_results_ch/*.jsonl" \
    --keymap batch_input_ch/ch_keymap.csv \
    --ons publisheduksicsummaryofstructureworksheet.xlsx --out supplier_sic.csv
#   → supplier_sic.csv  (one row per supplier_clean — joins back into enrich_stp*/bt_stp*)
```

**Watch:** adding previous-name columns enlarges the parquet slightly; the index must be rebuilt
from the same parquet (n_rows check enforces this). Confirm prev-name aliases count looks sane in
the match report (`Previous-name aliases indexed: N`). The order is load-bearing: §4A ranking must
run (step 1) before the batch is built (step 3) so Flash gets the well-ordered candidate list.

**File-feed clarity:** only `ch_exact_v2.csv` + `ch_candidates_v2.csv` feed forward (steps 3–5).
`ch_probe_pairs.csv` / `ch_segmented_out.csv` / `ch_no_match.csv` are diagnostic-only (look-at).
`ch_keymap.csv` is required by step 5 — do not delete it after step 3.

---

## 12. Files in this deliverable set (outputs)

- `bt_stp1b_ch_match.py`  — matcher with §4 changes (drop-in replacement; backward compatible).
- `bt_stp3_submit.py`     — parameterised `--pipeline {bt,ch}` (Task A; bt unchanged).
- `bt_stp1d_ch_resolve.py` — final resolve → `supplier_sic.csv` (Task B; built + tested).
- `diagnose_ch_recall.py` — recall probe + tiers + label sheet.
- `build_ch_token_index.py`, `ch_token_index.py` — streamed index + loader.
- `clean_supplier_prefixes.py` — standalone prefix cleaner (reference; `amzn` removed).
- `CH_MATCHING_HANDOFF.md` — this file.

**Not re-pasted but referenced (already in the project):** `bt_stp1c_ch_disambig.py`,
`config.py`, `utils.py`, `bt_companies_house_enrichment_DESIGN.md`,
`normalize_boroughs_X17.py` (user-edited with the upstream prefix cleaner),
`distinct_suppliers_review_updated.csv`, `supplier_context.csv`,
`publisheduksicsummaryofstructureworksheet.xlsx`.
