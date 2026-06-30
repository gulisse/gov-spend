#!/usr/bin/env python3
"""
bt_stp3_submit.py   (business-type batch — Step 3, STANDALONE)
───────────────────────────────────────────────────────────────
Submit the business-type enrichment JSONL files to the Gemini Batch API.

This is a SEPARATE pipeline from the procurement-taxonomy enrichment. It does
NOT import or share state with enrich_stp3_*:
  • its own GCS prefix, results dir and manifest
  • NO systemInstruction injection — the bt JSONL from bt_stp2 already embed the
    reference doc, so each row is uploaded as-is
  • concurrency capped at 2 (Gemini ↔ GCS throttling/contention seen above this)
  • small submission stagger + retry/backoff on the upload/submit hops

Workflow per job:  upload JSONL to GCS → create batch → poll → download → summarise

Usage
─────
    python bt_stp3_submit.py
    python bt_stp3_submit.py --submit-only
    python bt_stp3_submit.py --input-files batch_input_bt/batch_input_bt.jsonl
    python bt_stp3_submit.py --max-concurrent 2 --stagger 5

Pipelines  (--pipeline {bt,ch})                                      [added]
──────────────────────────────
The same submit/poll/download machinery now serves two pipelines via a profile.
`--pipeline bt` (the DEFAULT) is byte-for-byte the original behaviour above.

    --pipeline bt   (default)  business-type enrichment
        input  : batch_input_bt/batch_input_bt*.jsonl
        results: batch_results_bt/batch_results_bt_NNN.jsonl
        manifest: bt_batch_jobs_manifest.json   GCS: business_type_enrichment/
        summary : counts CONFIDENT / AMBIGUOUS / sub-types

    --pipeline ch              Companies House disambiguation (bt_stp1c output)
        input  : batch_input_ch/batch_input_ch*.jsonl
        results: batch_results_ch/batch_results_ch_NNN.jsonl
        manifest: ch_batch_jobs_manifest.json   GCS: companies_house_enrichment/
        summary : counts resolved vs NONE, by HIGH/MEDIUM/LOW confidence

The per-pipeline input dir, results dir and manifest are auto-selected from the
profile; override any of them explicitly with --input-files / --results-dir /
--manifest. Everything else (--max-concurrent, --stagger, --submit-only,
--poll-interval) is shared and unchanged. Examples:

    python bt_stp3_submit.py --pipeline ch
    python bt_stp3_submit.py --pipeline ch --submit-only
    python bt_stp3_submit.py --pipeline ch --input-files batch_input_ch/batch_input_ch.jsonl

Prerequisites
─────────────
    pip install google-genai google-cloud-storage
    gcloud auth application-default login
"""

import argparse
import concurrent.futures
import glob
import json
import os
import sys
import threading
import time
from datetime import datetime

from google import genai
from google.genai import types
from google.cloud import storage as gcs

# Shared environment/config only (project, bucket, model, poll cadence) — NOT
# any enrich-pipeline logic. The bt flow is otherwise self-contained.
from config import (
    GCP_LOCATION,
    GCP_PROJECT,
    GCS_BUCKET,
    MODEL_NAME,
    POLL_INTERVAL_SECONDS,
    CONSOLE_LOG_INTERVAL_SECONDS,
)
from utils import PollThrottle, ScriptTimer, setup_logging

# ── bt-specific constants (kept separate from the enrich pipeline) ──
BT_INPUT_DIR = "batch_input_bt"
BT_RESULTS_DIR = "batch_results_bt"
BT_MANIFEST_FILE = "bt_batch_jobs_manifest.json"
GCS_PREFIX_BT = "business_type_enrichment"
DEFAULT_MAX_CONCURRENT = 2          # Gemini↔GCS throttling — keep low
DEFAULT_STAGGER_SECONDS = 5         # gap between job starts to ease contention
UPLOAD_RETRIES = 4

# ── ch-specific constants (Companies House disambiguation) ──
CH_INPUT_DIR = "batch_input_ch"
CH_RESULTS_DIR = "batch_results_ch"
CH_MANIFEST_FILE = "ch_batch_jobs_manifest.json"
GCS_PREFIX_CH = "companies_house_enrichment"

TERMINAL_STATES = frozenset({
    "JOB_STATE_SUCCEEDED", "JobState.JOB_STATE_SUCCEEDED",
    "JOB_STATE_PARTIALLY_SUCCEEDED", "JobState.JOB_STATE_PARTIALLY_SUCCEEDED",
    "JOB_STATE_FAILED", "JobState.JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED", "JobState.JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED", "JobState.JOB_STATE_EXPIRED",
})
DOWNLOADABLE_STATES = frozenset({
    "JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED",
})


# ──────────────────────────────────────────────
# Manifest (thread-safe, bt-specific file)
# ──────────────────────────────────────────────
class Manifest:
    def __init__(self, path, model, total_files, pipeline="business_type"):
        self._path = path
        self._lock = threading.Lock()
        self._data = {
            "pipeline": pipeline,
            "pipeline_run": datetime.now().isoformat(),
            "model": model,
            "total_files": total_files,
            "jobs": [],
        }

    def add_job(self, index, input_file, job_name):
        with self._lock:
            self._data["jobs"].append({
                "index": index, "input_file": input_file, "job_name": job_name,
                "state": "SUBMITTED", "submitted_at": datetime.now().isoformat(),
                "completed_at": None, "result_file": None, "stats": None,
            })
            self._flush()

    def update_job(self, job_name, **kw):
        with self._lock:
            for e in self._data["jobs"]:
                if e["job_name"] == job_name:
                    e.update(kw); break
            self._flush()

    def _flush(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Submit a Gemini batch (bt or ch pipeline)")
    p.add_argument("--pipeline", choices=("bt", "ch"), default="bt",
                   help="Which pipeline profile to run (default: bt = original behaviour)")
    p.add_argument("--input-files", "-i", nargs="+", default=None,
                   help="JSONL input file(s) (default: <profile input dir>/*.jsonl)")
    p.add_argument("--results-dir", default=None,
                   help="Downloaded results dir (default: from profile)")
    p.add_argument("--manifest", default=None,
                   help="Manifest file (default: from profile)")
    p.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_SECONDS,
                   help=f"Seconds between status checks (default: {POLL_INTERVAL_SECONDS})")
    p.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT,
                   help=f"Max concurrent jobs (default: {DEFAULT_MAX_CONCURRENT} — Gemini<->GCS throttling)")
    p.add_argument("--stagger", type=int, default=DEFAULT_STAGGER_SECONDS,
                   help=f"Seconds between job starts (default: {DEFAULT_STAGGER_SECONDS})")
    p.add_argument("--submit-only", action="store_true",
                   help="Submit and exit without polling")
    return p.parse_args()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def get_state_str(job):
    return job.state.value if hasattr(job.state, "value") else str(job.state)


def upload_to_gcs(local_path, storage_client, logger, prefix, gcs_prefix):
    """Upload JSONL to GCS with retry/backoff (eases GCS contention)."""
    blob_name = f"{gcs_prefix}/{os.path.basename(local_path)}"
    bucket = storage_client.bucket(GCS_BUCKET)
    blob = bucket.blob(blob_name)
    size_mb = os.path.getsize(local_path) / (1024 * 1024)

    last_exc = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            logger.info(f"{prefix} Uploading to GCS ({size_mb:.0f} MB), attempt {attempt}")
            blob.upload_from_filename(local_path)
            uri = f"gs://{GCS_BUCKET}/{blob_name}"
            logger.info(f"{prefix} Upload complete -> {uri}")
            return uri
        except Exception as exc:                       # transient GCS errors
            last_exc = exc
            wait = min(60, 5 * 2 ** (attempt - 1))
            logger.warning(f"{prefix} Upload failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{prefix} GCS upload failed after {UPLOAD_RETRIES} attempts: {last_exc}")


def submit_batch_job(client, gcs_uri, display_name, logger, prefix):
    last_exc = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            job = client.batches.create(
                model=MODEL_NAME, src=gcs_uri,
                config=types.CreateBatchJobConfig(display_name=display_name),
            )
            logger.info(f"{prefix} Batch job created -> {job.name}")
            return job
        except Exception as exc:
            last_exc = exc
            wait = min(60, 5 * 2 ** (attempt - 1))
            logger.warning(f"{prefix} Submit failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{prefix} batch submit failed after {UPLOAD_RETRIES} attempts: {last_exc}")


def poll_until_done(client, job, poll_interval, logger, prefix):
    throttle = PollThrottle(logger, CONSOLE_LOG_INTERVAL_SECONDS)
    logger.info(f"{prefix} Polling every {poll_interval}s")
    while True:
        refreshed = client.batches.get(name=job.name)
        state = get_state_str(refreshed)
        throttle.log(state, prefix)
        if state in TERMINAL_STATES:
            logger.info(f"{prefix} Terminal state -> {state}")
            return refreshed
        time.sleep(poll_interval)


def _parse_gcs_uri(uri):
    path = uri.replace("gs://", "")
    bucket, _, blob = path.partition("/")
    return bucket, blob


def download_results(job, results_dir, job_index, logger, prefix, results_stem):
    os.makedirs(results_dir, exist_ok=True)
    out_path = os.path.join(results_dir, f"{results_stem}_{job_index:03d}.jsonl")
    storage_client = gcs.Client(project=GCP_PROJECT)

    dest = job.dest
    output_info = job.output_info

    # 1. direct file
    if dest and dest.gcs_uri:
        try:
            b, p = _parse_gcs_uri(dest.gcs_uri)
            storage_client.bucket(b).blob(p).download_to_filename(out_path)
            logger.info(f"{prefix} Results -> {out_path}")
            return out_path
        except Exception as exc:
            logger.warning(f"{prefix} dest.gcs_uri download failed: {exc}")

    # 2. output directory
    if output_info and output_info.gcs_output_directory:
        try:
            b, pfx = _parse_gcs_uri(output_info.gcs_output_directory)
            if pfx and not pfx.endswith("/"):
                pfx += "/"
            blobs = [x for x in storage_client.bucket(b).list_blobs(prefix=pfx)
                     if not x.name.endswith("/")]
            if not blobs:
                raise FileNotFoundError("empty output directory")
            with open(out_path, "wb") as fh:
                for blob in blobs:
                    fh.write(blob.download_as_bytes())
            logger.info(f"{prefix} Results -> {out_path}")
            return out_path
        except Exception as exc:
            logger.warning(f"{prefix} directory download failed: {exc}")

    # 3. inlined
    if dest and dest.inlined_responses:
        with open(out_path, "w", encoding="utf-8") as fh:
            for resp in dest.inlined_responses:
                fh.write(json.dumps(resp.model_dump(), default=str) + "\n")
        logger.info(f"{prefix} Results -> {out_path}")
        return out_path

    logger.error(f"{prefix} No downloadable output found")
    return None


def summarise_bt(result_path, logger, prefix):
    if not result_path or not os.path.exists(result_path):
        return {}
    total = confident = ambiguous = errors = 0
    subs = {}
    with open(result_path, encoding="utf-8") as fh:
        for line in fh:
            total += 1
            try:
                obj = json.loads(line)
                parts = (obj.get("response", {}).get("candidates", [{}])[0]
                            .get("content", {}).get("parts", []))
                parsed = next((json.loads(p["text"]) for p in parts if "text" in p), None)
                if parsed is None:
                    errors += 1; continue
                status = str(parsed.get("STATUS", "")).upper()
                st = parsed.get("BUSINESS_SUBTYPE")
                if status == "CONFIDENT":
                    confident += 1
                elif status == "AMBIGUOUS":
                    ambiguous += 1
                else:
                    errors += 1
                if st:
                    subs[st] = subs.get(st, 0) + 1
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                errors += 1
    pct = lambda n: f"{n / max(total,1) * 100:.1f}%"
    logger.info(f"{prefix} Summary: {total:,} total | {confident:,} confident "
                f"({pct(confident)}) | {ambiguous:,} ambiguous ({pct(ambiguous)}) | "
                f"{errors:,} errors | {len(subs):,} unique sub-types")
    return {"total": total, "confident": confident, "ambiguous": ambiguous,
            "errors": errors, "unique_subtypes": len(subs)}


def summarise_ch(result_path, logger, prefix):
    """CH disambiguation results: each row's response is {company_number, confidence}."""
    if not result_path or not os.path.exists(result_path):
        return {}
    total = resolved = none = errors = 0
    conf = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    with open(result_path, encoding="utf-8") as fh:
        for line in fh:
            total += 1
            try:
                obj = json.loads(line)
                parts = (obj.get("response", {}).get("candidates", [{}])[0]
                            .get("content", {}).get("parts", []))
                parsed = next((json.loads(p["text"]) for p in parts if "text" in p), None)
                if parsed is None:
                    errors += 1; continue
                num = str(parsed.get("company_number", "")).strip().upper()
                c = str(parsed.get("confidence", "")).strip().upper()
                if c in conf:
                    conf[c] += 1
                if num and num != "NONE":
                    resolved += 1
                else:
                    none += 1
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                errors += 1
    pct = lambda n: f"{n / max(total,1) * 100:.1f}%"
    logger.info(f"{prefix} Summary: {total:,} total | {resolved:,} resolved "
                f"({pct(resolved)}) | {none:,} NONE ({pct(none)}) | {errors:,} errors | "
                f"conf H/M/L = {conf['HIGH']:,}/{conf['MEDIUM']:,}/{conf['LOW']:,}")
    return {"total": total, "resolved": resolved, "none": none, "errors": errors,
            "confidence": conf}


# ── pipeline profiles: everything that differs between bt and ch ──
PROFILES = {
    "bt": {
        "input_dir": BT_INPUT_DIR, "glob": "batch_input_bt*.jsonl",
        "results_dir": BT_RESULTS_DIR, "results_stem": "batch_results_bt",
        "manifest": BT_MANIFEST_FILE, "gcs_prefix": GCS_PREFIX_BT,
        "display_prefix": "bt_enrich", "manifest_label": "business_type",
        "summariser": summarise_bt,
    },
    "ch": {
        "input_dir": CH_INPUT_DIR, "glob": "batch_input_ch*.jsonl",
        "results_dir": CH_RESULTS_DIR, "results_stem": "batch_results_ch",
        "manifest": CH_MANIFEST_FILE, "gcs_prefix": GCS_PREFIX_CH,
        "display_prefix": "ch_disambig", "manifest_label": "companies_house",
        "summariser": summarise_ch,
    },
}


# ──────────────────────────────────────────────
# Per-job lifecycle (no injection step)
# ──────────────────────────────────────────────
def process_job(jsonl_path, job_index, total_jobs, client, storage_client,
                manifest, args, logger, profile):
    filename = os.path.basename(jsonl_path)
    prefix = f"[{job_index:>2}/{total_jobs} {filename}]"
    result = {"index": job_index, "input_file": jsonl_path,
              "result_path": None, "state": None, "error": None}
    try:
        gcs_uri = upload_to_gcs(jsonl_path, storage_client, logger, prefix,
                                profile["gcs_prefix"])
        display_name = f"{profile['display_prefix']}_{filename}"
        job = submit_batch_job(client, gcs_uri, display_name, logger, prefix)
        manifest.add_job(job_index, jsonl_path, job.name)

        if args.submit_only:
            result["state"] = "SUBMITTED"
            logger.info(f"{prefix} Submitted (submit-only)")
            return result

        completed = poll_until_done(client, job, args.poll_interval, logger, prefix)
        state = get_state_str(completed)
        result["state"] = state

        stats_dict = None
        if completed.completion_stats:
            s = completed.completion_stats
            stats_dict = {"succeeded": s.successful_count, "failed": s.failed_count,
                          "incomplete": s.incomplete_count}

        if state in DOWNLOADABLE_STATES:
            rpath = download_results(completed, args.results_dir, job_index, logger,
                                     prefix, profile["results_stem"])
            result["result_path"] = rpath
            if rpath:
                profile["summariser"](rpath, logger, prefix)
            manifest.update_job(job.name, state=state,
                                completed_at=datetime.now().isoformat(),
                                result_file=rpath, stats=stats_dict)
        else:
            logger.error(f"{prefix} Ended {state} — skipping download")
            manifest.update_job(job.name, state=state,
                                completed_at=datetime.now().isoformat(), stats=stats_dict)
    except Exception as exc:
        result["error"] = str(exc)
        logger.error(f"{prefix} FAILED — {exc}", exc_info=True)
    return result


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    args = parse_args()
    profile = PROFILES[args.pipeline]
    # resolve profile-dependent defaults (explicit flags win)
    if args.results_dir is None:
        args.results_dir = profile["results_dir"]
    if args.manifest is None:
        args.manifest = profile["manifest"]

    logger = setup_logging(f"{args.pipeline}_stp3_submit")
    timer = ScriptTimer(logger)
    timer.start(f"bt_stp3_submit.py ({args.pipeline})")

    logger.info(f"Connecting GenAI  project={GCP_PROJECT}  location={GCP_LOCATION}")
    client = genai.Client(enterprise=True, project=GCP_PROJECT, location=GCP_LOCATION)
    storage_client = gcs.Client(project=GCP_PROJECT)

    if args.input_files:
        input_files = args.input_files
    else:
        input_files = sorted(glob.glob(os.path.join(profile["input_dir"], profile["glob"])))
    if not input_files:
        logger.error(f"No JSONL input files found for pipeline '{args.pipeline}'. "
                     f"Run the matching build step first.")
        sys.exit(1)

    total_jobs = len(input_files)
    logger.info(f"Pipeline: {args.pipeline} | Input files ({total_jobs}):")
    for f in input_files:
        logger.info(f"  • {f}")

    manifest = Manifest(args.manifest, MODEL_NAME, total_jobs,
                        pipeline=profile["manifest_label"])
    mode = "submit-only" if args.submit_only else "end-to-end"
    logger.info(f"Mode: {mode} | Max concurrent: {args.max_concurrent} | "
                f"Stagger: {args.stagger}s | Model: {MODEL_NAME}")

    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_concurrent) as ex:
        futures = {}
        for idx, path in enumerate(input_files, 1):
            futures[ex.submit(process_job, path, idx, total_jobs, client,
                              storage_client, manifest, args, logger, profile)] = path
            time.sleep(args.stagger)        # stagger starts to ease contention
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                all_results.append(fut.result())
            except Exception as exc:
                logger.error(f"Unhandled exception for {os.path.basename(path)}: {exc}")
                all_results.append({"input_file": path, "error": str(exc)})

    succeeded = sum(1 for r in all_results if r.get("state") in DOWNLOADABLE_STATES)
    failed = sum(1 for r in all_results if r.get("error"))
    submitted = sum(1 for r in all_results if r.get("state") == "SUBMITTED")

    logger.info("=" * 64)
    logger.info(f"{profile['manifest_label'].upper()} PIPELINE SUMMARY")
    logger.info(f"  Total jobs: {total_jobs}")
    logger.info(f"  Succeeded:  {succeeded}")
    if failed:
        logger.info(f"  Failed:     {failed}")
    if args.submit_only:
        logger.info(f"  Submitted:  {submitted}  (monitor via manifest {args.manifest})")
    logger.info(f"  Manifest:   {args.manifest}")
    logger.info(f"  Results:    {args.results_dir}")
    logger.info("=" * 64)
    timer.end()


if __name__ == "__main__":
    main()
