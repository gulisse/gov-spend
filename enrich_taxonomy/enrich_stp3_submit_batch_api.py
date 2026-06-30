#!/usr/bin/env python3
"""
enrich_stp3_submit_batch_api.py
───────────────────────────────
Submit taxonomy enrichment jobs to the Gemini Batch API.

Workflow
───────
    1. Build reference document (taxonomy.csv + priority_rules.md)
    2. For each JSONL template from Step 2:
       a. Inject reference doc as systemInstruction into every row
       b. Upload augmented JSONL to GCS
       c. Submit batch job
    3. Poll all jobs concurrently (rolling pool of MAX_CONCURRENT_JOBS)
    4. Download results as each job completes
    5. Summarise confident / ambiguous / error counts
    6. Write manifest file throughout for check_job visibility

The reference doc is embedded in every row's systemInstruction so
that Vertex AI's implicit caching automatically de-duplicates it
across requests, delivering ~90% savings on the repeated prefix
with zero cache lifecycle management.

Usage
─────
    python enrich_stp3_submit_batch_api.py
    python enrich_stp3_submit_batch_api.py --submit-only
    python enrich_stp3_submit_batch_api.py --input-files custom1.jsonl custom2.jsonl
    python enrich_stp3_submit_batch_api.py --max-concurrent 10

Prerequisites
─────────────
    pip install google-genai google-cloud-storage
    Authenticated via:  gcloud auth application-default login
"""

import argparse
import concurrent.futures
import glob
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

from google import genai
from google.genai import types
from google.cloud import storage as gcs

# ── Requires the pyrightconfig.json in the vs code root for config and utils to import 
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

from config import (
    BATCH_MANIFEST_FILE,
    BATCH_OUTPUT_DIR,
    BATCH_RESULTS_DIR,
    CONSOLE_LOG_INTERVAL_SECONDS,
    GCP_LOCATION,
    GCP_PROJECT,
    GCS_BUCKET,
    GCS_PREFIX,
    MAX_CONCURRENT_JOBS,
    MODEL_NAME,
    POLL_INTERVAL_SECONDS,
    PRIORITY_RULES_FILE,
    TAXONOMY_FILE,
)
from utils import PollThrottle, ScriptTimer, build_reference_document, setup_logging


# ──────────────────────────────────────────────
# Terminal / downloadable batch-job states
# ──────────────────────────────────────────────
TERMINAL_STATES = frozenset({
    "JOB_STATE_SUCCEEDED", "JobState.JOB_STATE_SUCCEEDED",
    "JOB_STATE_PARTIALLY_SUCCEEDED", "JobState.JOB_STATE_PARTIALLY_SUCCEEDED",
    "JOB_STATE_FAILED", "JobState.JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED", "JobState.JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED", "JobState.JOB_STATE_EXPIRED",
})

DOWNLOADABLE_STATES = frozenset({
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
})


# ──────────────────────────────────────────────
# Manifest  (thread-safe)
# ──────────────────────────────────────────────

class Manifest:
    """Thread-safe manifest for tracking batch job lifecycle."""

    def __init__(self, path: str, model: str, total_files: int):
        self._path = path
        self._lock = threading.Lock()
        self._data = {
            "pipeline_run": datetime.now().isoformat(),
            "model": model,
            "total_files": total_files,
            "jobs": [],
        }

    def add_job(self, index: int, input_file: str, job_name: str) -> None:
        """Register a newly submitted job."""
        with self._lock:
            self._data["jobs"].append({
                "index": index,
                "input_file": input_file,
                "job_name": job_name,
                "state": "SUBMITTED",
                "submitted_at": datetime.now().isoformat(),
                "completed_at": None,
                "result_file": None,
                "stats": None,
            })
            self._flush()

    def update_job(self, job_name: str, **kwargs) -> None:
        """Update fields on an existing job entry."""
        with self._lock:
            for entry in self._data["jobs"]:
                if entry["job_name"] == job_name:
                    entry.update(kwargs)
                    break
            self._flush()

    def _flush(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Submit taxonomy enrichment to Gemini Batch API"
    )
    p.add_argument(
        "--input-files", "-i", nargs="+", default=None,
        help="Override JSONL input file(s)  (default: batch_input/*.jsonl)",
    )
    p.add_argument(
        "--taxonomy", default=TAXONOMY_FILE,
        help=f"Taxonomy CSV  (default: {TAXONOMY_FILE})",
    )
    p.add_argument(
        "--rules", default=PRIORITY_RULES_FILE,
        help=f"Priority rules MD  (default: {PRIORITY_RULES_FILE})",
    )
    p.add_argument(
        "--results-dir", default=BATCH_RESULTS_DIR,
        help=f"Directory for downloaded results  (default: {BATCH_RESULTS_DIR})",
    )
    p.add_argument(
        "--poll-interval", type=int, default=POLL_INTERVAL_SECONDS,
        help=f"Seconds between status checks  (default: {POLL_INTERVAL_SECONDS})",
    )
    p.add_argument(
        "--max-concurrent", type=int, default=2,
        help="Max concurrent batch jobs  (default: 2 — Gemini<->GCS throttling)",
    )
    p.add_argument(
        "--submit-only", action="store_true",
        help="Submit all jobs and exit without polling  (use check_job to monitor)",
    )
    p.add_argument(
        "--stagger", type=int, default=5,
        help="Seconds between job starts to ease Gemini<->GCS contention  (default: 5)",
    )
    return p.parse_args()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_state_str(job) -> str:
    """Extract state as a plain string from a batch job object."""
    return job.state.value if hasattr(job.state, "value") else str(job.state)


def inject_system_instruction(
    template_path: str, reference_doc: str, logger, prefix: str
) -> str:
    """
    Read a Step-2 JSONL template, inject systemInstruction with the
    reference doc into every row, write to a temp file.

    Returns the temp file path (caller must delete after upload).
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8",
    )
    count = 0
    try:
        with open(template_path, "r", encoding="utf-8") as fin:
            for line in fin:
                obj = json.loads(line)
                obj["request"]["systemInstruction"] = {
                    "parts": [{"text": reference_doc}]
                }
                tmp.write(json.dumps(obj, ensure_ascii=False) + "\n")
                count += 1
    finally:
        tmp.close()

    logger.info(f"{prefix} Injected systemInstruction into {count:,} rows")
    return tmp.name


def upload_to_gcs(
    local_path: str, storage_client, logger, prefix: str
) -> str:
    """Upload a local file to GCS and return its gs:// URI."""
    blob_name = f"{GCS_PREFIX}/{os.path.basename(local_path)}"
    bucket = storage_client.bucket(GCS_BUCKET)
    blob = bucket.blob(blob_name)

    file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
    logger.info(f"{prefix} Uploading to GCS ({file_size_mb:.0f} MB) …")
    blob.upload_from_filename(local_path)

    gcs_uri = f"gs://{GCS_BUCKET}/{blob_name}"
    logger.info(f"{prefix} Upload complete → {gcs_uri}")
    return gcs_uri


def submit_batch_job(client, gcs_uri: str, display_name: str, logger, prefix: str):
    """Create a batch prediction job on Vertex AI."""
    job = client.batches.create(
        model=MODEL_NAME,
        src=gcs_uri,
        config=types.CreateBatchJobConfig(
            display_name=display_name,
        ),
    )
    logger.info(f"{prefix} Batch job created → {job.name}")
    return job


def poll_until_done(client, job, poll_interval: int, logger, prefix: str):
    """Block until the batch job reaches a terminal state."""
    throttle = PollThrottle(logger, CONSOLE_LOG_INTERVAL_SECONDS)
    logger.info(
        f"{prefix} Polling every {poll_interval}s  "
        f"(console heartbeat every {CONSOLE_LOG_INTERVAL_SECONDS}s)"
    )

    while True:
        refreshed = client.batches.get(name=job.name)
        state = get_state_str(refreshed)
        throttle.log(state, prefix)

        if state in TERMINAL_STATES:
            logger.info(f"{prefix} Terminal state → {state}")
            return refreshed

        time.sleep(poll_interval)


# ──────────────────────────────────────────────
# Download results
# ──────────────────────────────────────────────

def download_results(
    client, job, results_dir: str, job_index: int, logger, prefix: str
) -> str | None:
    """
    Download output from a completed Vertex AI batch job.

    Checks (in order):
      1. job.dest.gcs_uri           — single output file
      2. job.output_info.gcs_output_directory — output directory
      3. job.dest.inlined_responses  — embedded in response object
    """
    os.makedirs(results_dir, exist_ok=True)
    out_path = os.path.join(results_dir, f"batch_results_{job_index:03d}.jsonl")

    stats = job.completion_stats
    if stats:
        logger.info(
            f"{prefix} Completion stats — "
            f"succeeded: {stats.successful_count}, "
            f"failed: {stats.failed_count}, "
            f"incomplete: {stats.incomplete_count}"
        )

    storage_client = gcs.Client(project=GCP_PROJECT)

    # Strategy 1: Direct GCS URI from dest
    dest = job.dest
    if dest and dest.gcs_uri:
        try:
            _download_gcs_uri(storage_client, dest.gcs_uri, out_path, logger, prefix)
            return out_path
        except Exception as exc:
            logger.warning(f"{prefix} dest.gcs_uri download failed: {exc}")

    # Strategy 2: GCS output directory
    output_info = job.output_info
    if output_info and output_info.gcs_output_directory:
        try:
            _download_gcs_directory(
                storage_client, output_info.gcs_output_directory,
                out_path, logger, prefix,
            )
            return out_path
        except Exception as exc:
            logger.warning(f"{prefix} GCS directory download failed: {exc}")

    # Strategy 3: Inlined responses
    if dest and dest.inlined_responses:
        logger.info(f"{prefix} Extracting {len(dest.inlined_responses)} inlined responses")
        with open(out_path, "w", encoding="utf-8") as fh:
            for resp in dest.inlined_responses:
                fh.write(json.dumps(resp.model_dump(), default=str) + "\n")
        logger.info(f"{prefix} Results saved → {out_path}")
        return out_path

    logger.error(f"{prefix} No downloadable output found")
    logger.debug(f"{prefix} dest={dest}  output_info={output_info}")
    return None


def _parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    """Split gs://bucket/path into (bucket, path)."""
    path = gcs_uri.replace("gs://", "")
    bucket_name, _, blob_path = path.partition("/")
    return bucket_name, blob_path


def _download_gcs_uri(storage_client, gcs_uri, local_path, logger, prefix):
    bucket_name, blob_path = _parse_gcs_uri(gcs_uri)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.download_to_filename(local_path)
    logger.info(f"{prefix} Results saved → {local_path}")


def _download_gcs_directory(storage_client, gcs_dir, local_path, logger, prefix):
    bucket_name, prefix_path = _parse_gcs_uri(gcs_dir)
    if prefix_path and not prefix_path.endswith("/"):
        prefix_path += "/"

    bucket = storage_client.bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=prefix_path))

    if not blobs:
        raise FileNotFoundError(f"No files found in {gcs_dir}")

    logger.info(f"{prefix} Found {len(blobs)} file(s) in output directory")

    with open(local_path, "wb") as fh:
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            logger.debug(f"{prefix} Downloading gs://{bucket_name}/{blob.name}")
            fh.write(blob.download_as_bytes())

    logger.info(f"{prefix} Results saved → {local_path}")


# ──────────────────────────────────────────────
# Summarise
# ──────────────────────────────────────────────

def summarise_results(result_path: str, logger, prefix: str) -> dict:
    """Parse downloaded JSONL and return/log classification statistics."""
    if not result_path or not os.path.exists(result_path):
        logger.warning(f"{prefix} No result file to summarise")
        return {}

    total = confident = ambiguous = errors = 0
    code_counts: dict[int, int] = {}

    with open(result_path, "r", encoding="utf-8") as fh:
        for line in fh:
            total += 1
            try:
                obj = json.loads(line)
                parts = (
                    obj.get("response", {})
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                parsed = None
                for part in parts:
                    if "text" in part:
                        parsed = json.loads(part["text"])
                        break

                if parsed is None:
                    errors += 1
                    continue

                status = parsed.get("RESOLUTION_STATUS", "UNKNOWN").upper()
                code = parsed.get("TAXONOMY_CODE")

                if status == "CONFIDENT":
                    confident += 1
                elif status == "AMBIGUOUS":
                    ambiguous += 1
                else:
                    errors += 1

                if code is not None:
                    code_counts[code] = code_counts.get(code, 0) + 1

            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                errors += 1

    pct = lambda n: f"{n / max(total, 1) * 100:.1f}%"

    logger.info(
        f"{prefix} Summary: {total:,} total  |  "
        f"{confident:,} confident ({pct(confident)})  |  "
        f"{ambiguous:,} ambiguous ({pct(ambiguous)})  |  "
        f"{errors:,} errors ({pct(errors)})  |  "
        f"{len(code_counts):,} unique codes"
    )

    return {
        "total": total,
        "confident": confident,
        "ambiguous": ambiguous,
        "errors": errors,
        "unique_codes": len(code_counts),
    }


# ──────────────────────────────────────────────
# Job worker  (runs inside the thread pool)
# ──────────────────────────────────────────────

def process_job(
    jsonl_path: str,
    job_index: int,
    total_jobs: int,
    reference_doc: str,
    client,
    storage_client,
    manifest: Manifest,
    args,
    logger,
) -> dict:
    """
    Full lifecycle for one batch job:
        inject → upload → submit → (poll → download → summarise)

    Returns a result dict consumed by the main summary.
    """
    filename = os.path.basename(jsonl_path)
    prefix = f"[{job_index:>2}/{total_jobs} {filename}]"

    result = {
        "index": job_index,
        "input_file": jsonl_path,
        "result_path": None,
        "state": None,
        "error": None,
    }

    tmp_path = None
    try:
        # ── 1. Inject systemInstruction ──────────────────
        tmp_path = inject_system_instruction(
            jsonl_path, reference_doc, logger, prefix,
        )

        # ── 2. Upload to GCS ────────────────────────────
        gcs_uri = upload_to_gcs(tmp_path, storage_client, logger, prefix)

        # ── 3. Submit batch job ─────────────────────────
        display_name = f"enrich_{filename}"
        job = submit_batch_job(client, gcs_uri, display_name, logger, prefix)

        # Register in manifest immediately
        manifest.add_job(job_index, jsonl_path, job.name)

        # ── Submit-only mode: stop here ─────────────────
        if args.submit_only:
            result["state"] = "SUBMITTED"
            logger.info(f"{prefix} Submitted (submit-only mode — use check_job to monitor)")
            return result

        # ── 4. Poll until terminal ──────────────────────
        completed = poll_until_done(
            client, job, args.poll_interval, logger, prefix,
        )
        state = get_state_str(completed)
        result["state"] = state

        # ── 5. Download results ─────────────────────────
        stats_dict = None
        if completed.completion_stats:
            s = completed.completion_stats
            stats_dict = {
                "succeeded": s.successful_count,
                "failed": s.failed_count,
                "incomplete": s.incomplete_count,
            }

        if state in DOWNLOADABLE_STATES:
            rpath = download_results(
                client, completed, args.results_dir, job_index, logger, prefix,
            )
            result["result_path"] = rpath

            # ── 6. Summarise ────────────────────────────
            if rpath:
                summarise_results(rpath, logger, prefix)

            manifest.update_job(
                job.name,
                state=state,
                completed_at=datetime.now().isoformat(),
                result_file=rpath,
                stats=stats_dict,
            )
        else:
            logger.error(f"{prefix} Job ended with state {state} — skipping download")
            manifest.update_job(
                job.name,
                state=state,
                completed_at=datetime.now().isoformat(),
                stats=stats_dict,
            )

    except Exception as exc:
        result["error"] = str(exc)
        logger.error(f"{prefix} FAILED — {exc}", exc_info=True)

    finally:
        # Clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return result


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    args = parse_args()

    logger = setup_logging("stp3_submit_batch_api")
    timer = ScriptTimer(logger)
    timer.start("enrich_stp3_submit_batch_api.py")

    # ── Initialise clients ───────────────────────────────
    logger.info(f"Connecting to GenAI  project={GCP_PROJECT}  location={GCP_LOCATION}")
    client = genai.Client(
        enterprise=True,
        project=GCP_PROJECT,
        location=GCP_LOCATION,
    )
    storage_client = gcs.Client(project=GCP_PROJECT)
    logger.info("Clients initialised")

    # ── Resolve JSONL input files ────────────────────────
    if args.input_files:
        input_files = args.input_files
    else:
        pattern = os.path.join(BATCH_OUTPUT_DIR, "batch_input*.jsonl")
        input_files = sorted(glob.glob(pattern))

    if not input_files:
        logger.error("No JSONL input files found.  Run Step 2 first.")
        sys.exit(1)

    total_jobs = len(input_files)
    logger.info(f"Input files ({total_jobs}):")
    for f in input_files:
        logger.info(f"  • {f}")

    # ── Build reference document ─────────────────────────
    reference_doc = build_reference_document(args.taxonomy, args.rules, logger)

    # ── Initialise manifest ──────────────────────────────
    manifest = Manifest(BATCH_MANIFEST_FILE, MODEL_NAME, total_jobs)
    logger.info(f"Manifest → {BATCH_MANIFEST_FILE}")

    # ── Submit & process jobs in a rolling pool ──────────
    mode = "submit-only" if args.submit_only else "end-to-end"
    logger.info(
        f"Mode: {mode}  |  Max concurrent: {args.max_concurrent}  |  "
        f"Model: {MODEL_NAME}"
    )

    all_results: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.max_concurrent
    ) as executor:
        futures = {}
        for idx, jsonl_path in enumerate(input_files, 1):
            future = executor.submit(
                process_job,
                jsonl_path, idx, total_jobs, reference_doc,
                client, storage_client, manifest, args, logger,
            )
            futures[future] = jsonl_path
            time.sleep(args.stagger)        # stagger starts to ease contention

        # Collect results as each job completes
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
                all_results.append(result)
                if result.get("error"):
                    logger.error(f"Job for {os.path.basename(path)}: {result['error']}")
            except Exception as exc:
                logger.error(f"Unhandled exception for {os.path.basename(path)}: {exc}")
                all_results.append({"input_file": path, "error": str(exc)})

    # ── Final pipeline summary ───────────────────────────
    succeeded = sum(
        1 for r in all_results
        if r.get("state") in DOWNLOADABLE_STATES
    )
    failed = sum(1 for r in all_results if r.get("error"))
    submitted = sum(1 for r in all_results if r.get("state") == "SUBMITTED")
    other = total_jobs - succeeded - failed - submitted

    logger.info("=" * 64)
    logger.info("PIPELINE SUMMARY")
    logger.info(f"  Total jobs:       {total_jobs}")
    logger.info(f"  Succeeded:        {succeeded}")
    if failed:
        logger.info(f"  Failed / Error:   {failed}")
    if other:
        logger.info(f"  Other terminal:   {other}")
    if args.submit_only:
        logger.info(f"  Submitted:        {submitted}  (use check_job.py to monitor)")
    logger.info(f"  Manifest:         {BATCH_MANIFEST_FILE}")
    logger.info(f"  Results dir:      {args.results_dir}")
    logger.info("=" * 64)

    timer.end()


if __name__ == "__main__":
    main()
