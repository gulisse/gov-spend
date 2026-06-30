# Taxonomy Enrichment Pipeline — Handoff Notes

Part of the **GOV data project**. This file is the source-of-truth summary of the
pipeline's final state and the reasoning behind its design. Upload it at the start
of a new chat to get oriented in one step. **The scripts on disk are authoritative**
— this README explains them, it does not replace them.

---

## What the pipeline does

Classifies UK council procurement spend against a 597-code taxonomy using Gemini
2.5 Flash-Lite via the Vertex AI **Batch API**. Raw spend (~7M rows) is aggregated
to unique combinations, each combination is classified once, and the result is
merged back to the granular spend.

---

## Files

| File | Role | Changed in refactor? |
|------|------|----------------------|
| `config.py` | Shared settings (model, paths, batch size, concurrency) | Yes |
| `utils.py` | Logging, timing, polling, **`build_reference_document`** | Yes |
| `enrich_stp1_create_taxonomy_base.py` | Aggregate spend, assign `rq_id` | No |
| `enrich_stp2_create_batch_source_files.py` | Build JSONL request templates | Yes |
| `enrich_stp3_submit_batch_api.py` | Inject system instruction, upload to GCS, submit + poll + download | Yes (rewritten) |
| `enrich_stp3_check_job.py` | Status table for all jobs; optional download | Yes (rewritten) |
| `enrich_stp4_merge_batch_results.py` | Parse results, NEC fallback, merge to base + taxonomy | Yes |
| `enrich_stp5_merge_to_spend.py` | Merge enriched base back to granular spend | No |

Data files: `Normalized_spend.csv` (raw), `taxonomy.csv` (597 codes),
`priority_rules.md` (Rules 1-6). Intermediates: `tbl_taxonomy_base.csv`,
`batch_input/*.jsonl`, `batch_results/*.jsonl`, `batch_jobs_manifest.json`.
Outputs: `tbl_taxonomy_base_enriched.csv`, `Normalized_spend_enriched.csv`.

---

## Run order

```bash
# 1. Aggregate + assign rq_id   (--aggregate only if input is raw granular spend)
python enrich_stp1_create_taxonomy_base.py --input <file>.csv [--aggregate]

# 2. Build JSONL request templates (no system instruction yet)
python enrich_stp2_create_batch_source_files.py --input <file>_base.csv

# 3. Submit (rolling pool of 20, parallel). Use --submit-only to fire-and-monitor.
python enrich_stp3_submit_batch_api.py

#    Monitor from another terminal at any time:
python enrich_stp3_check_job.py            # add --download to pull completed results

# 4. Merge results → base → taxonomy labels
python enrich_stp4_merge_batch_results.py --taxonomy-base <file>_base.csv

# 5. (optional) Merge enriched base back to granular spend
python enrich_stp5_merge_to_spend.py
```

A custom Step 1 input `X.csv` produces `X_base.csv`. Step 2 splits into
`batch_input_NNN.jsonl` at `BATCH_SIZE` rows each.

---

## Key configuration (`config.py`)

- `MODEL_NAME = "gemini-2.5-flash-lite"`
- `BATCH_SIZE = 10_000` — keeps each JSONL file well under the GCS 1 GB limit once
  the reference doc is injected (~52 KB per row).
- `MAX_CONCURRENT_JOBS = 20` — rolling pool: 20 jobs in-flight; as one finishes,
  the next starts.
- Deterministic decoding: `temperature=0, topK=1`.

---

## Design decisions (the important part)

**Batch API + implicit caching, not explicit caching.** The original error —
*"does not support cached content with batch prediction"* — is real: explicit
caches cannot be combined with batch on any Gemini model. Implicit caching is
enabled by default on 2.5 models in batch and gives the same ~90% discount on the
repeated prefix with zero cache lifecycle. We rely on that.

**Reference doc embedded as `systemInstruction`, injected at submission.** Step 2
writes small templates *without* the taxonomy doc; Step 3 injects the full
reference doc (taxonomy + rules, built by `build_reference_document`) into every
row at upload time via a temp file. Keeps templates small/inspectable and lets
implicit caching dedupe the identical prefix across all rows.

**Vertex AI batch reads input from GCS.** `client.batches.create(src=gs://...)`
requires the JSONL in a bucket — hence the upload step. (Online/AI-Studio allows
direct file upload; enterprise batch does not.)

**Reconciliation is via the batch `key` field, never a model output.** Step 2 sets
`"key": "RQ_846"`; the API echoes it verbatim on each response line; Step 4 merges
on it (`obj.get("key")` → `rq_id`, `how="left"`). The model's answer carries no ID.

**The `RQ_ID` output field was REMOVED from the response schema.** It told the model
to "echo the RQ_ID from the input record," but the ID is never in the prompt — so on
some rows the model spiralled (runaway digits / looping codes), hit `MAX_TOKENS`
before emitting `TAXONOMY_CODE`, and produced unparseable JSON → no code. Removing it
fixed those failures and trimmed output tokens. Reconciliation was unaffected (see
above). Response schema now: `TAXONOMY_CODE` + `RESOLUTION_STATUS` only.

**`contents` entries need `"role": "user"`.** The batch API rejects rows without it
(*"Please use a valid role"*). This is set in `build_jsonl_row`.

**Step 2 strips embedded council reference codes from field values.** Regex
`^[A-Za-z]?\d{4,}\s+` turns `"361300 PUBLIC TRANSPORT"` into `"PUBLIC TRANSPORT"`,
so the model classifies on description, not on a number it would otherwise parrot
back as the taxonomy code. **Exception: `supplier_clean` is NOT stripped** (business
names can legitimately start with digits).

**Step 4 NEC fallback.** Any returned code that does not exist in `taxonomy.csv` is
remapped to the nearest "Not Elsewhere Classified" code by progressive truncation
(`code[:4]+99` → `code[:3]+999` → `code[:2]+9999`). Remapped rows are flagged
`resolution_status = "NEC_FALLBACK"` for easy review. This catches *invalid* codes
only — it cannot help a *missing* code (NaN), which indicates a request that failed
or returned nothing.

**Lean prompt for Flash-Lite — deliberately.** The prompt is intentionally minimal:
taxonomy table + a short "use only exact codes from Column A, else NEC; ignore
numeric codes in inputs" constraint + the user's Priority Rules 1-6. A
**supplier-anchor rule was tried and reverted**: telling the model to prefer
transport codes and naming code ranges (3614xx vs 2614xx) anchored it on number
patterns and caused a ~9× explosion in NEC fallbacks (81 vs 9), and wrongly pushed
rail tickets (legitimately at 261620 in the HR branch) into fabricated Passenger
Transport codes. Lesson: Flash-Lite is already strong at this; extra prescriptive
detail over-steers it. Fix deterministic problems in code (Steps 2 & 4), reserve the
prompt for genuine judgment.

---

## Known, accepted tradeoffs

- **26xx vs 36xx prefix slips (rare).** Occasionally a clear taxi spend lands in
  Human Resources → Temporary & Agency Staff (e.g. 261413) instead of Passenger
  Transport → Taxi Services (361xxx), because suffixes match and travel is split
  across two top-level branches. Accepted as rare. If it ever becomes frequent, fix
  it deterministically in Step 4 (supplier matches taxi pattern + code is 2614xx →
  remap to 3614xx) — **not** in the prompt.
- **Baseline accuracy** on the taxis sample was ~98.5% valid codes before the
  supplier-rule experiment; that leaner configuration is the one to keep.

---

## `supplier_clean` vs `supplier_display`

`supplier_clean` is the **immutable join key** across every pipeline (taxonomy,
business-type, Companies House). Some values carry payment-gateway prefixes
(e.g. `Stk*shutterstock`, `Wp-kamset Digital`) that are artefacts of card-acquirer
statement descriptors, not part of the company name.

These prefixes are **not** stripped from `supplier_clean` because regenerating
`normalized_spend.csv` would break every downstream lookup. Instead:

- **For matching:** `clean_supplier_prefixes.py` (in `enrich_companies/`) provides
  `strip_prefixes()`, applied in-memory only via `clean_for_match()` in the CH
  matcher. No data files are modified.
- **For end users:** a `supplier_display` column will be added as a **post-processing
  step** after all pipelines complete, using the same `strip_prefixes()` logic.
  This gives users a clean supplier name while `supplier_clean` remains untouched
  as the join key.
- **On next full reprocessing:** integrate `strip_prefixes()` into
  `normalize_boroughs.py` so new `supplier_clean` values are clean from the start,
  eliminating the need for the workaround.

---

## Continuity checklist for a new chat

1. Re-upload the current scripts (they are the source of truth).
2. Upload this README.
3. Note: the work container resets between sessions; nothing persists automatically.
4. Treat uploaded files as authoritative over anything found in old chat history —
   the history contains superseded versions (RQ_ID present then removed, supplier
   rule added then reverted).
