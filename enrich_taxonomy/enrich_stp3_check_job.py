#!/usr/bin/env python3
"""
enrich_stp3_check_job.py
────────────────────────
Check status of batch jobs and display in a formatted table.

Modes
─────
    No arguments   → read manifest, check all jobs
    Job name(s)    → check specific job(s)
    --download     → also download results for completed jobs

Usage
─────
    python enrich_stp3_check_job.py
    python enrich_stp3_check_job.py --download
    python enrich_stp3_check_job.py <JOB_NAME>
    python enrich_stp3_check_job.py <JOB_NAME1> <JOB_NAME2> --download
"""

import argparse
import json
import os
import sys

from google import genai
from google.cloud import storage as gcs

from config import (
    BATCH_MANIFEST_FILE,
    BATCH_RESULTS_DIR,
    GCP_LOCATION,
    GCP_PROJECT,
)

import time
from datetime import datetime



# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Check batch job status and optionally download results"
    )
    p.add_argument(
        "jobs", nargs="*",
        help="Specific job name(s) to check  (default: all jobs from manifest)",
    )
    p.add_argument(
        "--manifest", "-m", default=BATCH_MANIFEST_FILE,
        help=f"Manifest file  (default: {BATCH_MANIFEST_FILE})",
    )
    p.add_argument(
        "--download", "-d", action="store_true",
        help="Download results for completed jobs that haven't been downloaded yet",
    )
    p.add_argument(
        "--results-dir", default=BATCH_RESULTS_DIR,
        help=f"Directory for downloaded results  (default: {BATCH_RESULTS_DIR})",
    )
    return p.parse_args()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_state_str(job) -> str:
    """Extract state as a plain string."""
    return job.state.value if hasattr(job.state, "value") else str(job.state)


def short_state(state: str) -> str:
    """Strip the JOB_STATE_ prefix for compact display."""
    return (
        state
        .replace("JOB_STATE_", "")
        .replace("JobState.JOB_STATE_", "")
        .replace("JobState.", "")
    )


def download_result(job, job_index: int, results_dir: str) -> str | None:
    """Download results for a completed job. Returns local path or None."""
    os.makedirs(results_dir, exist_ok=True)
    out_path = os.path.join(results_dir, f"batch_results_{job_index:03d}.jsonl")

    if os.path.exists(out_path):
        return out_path  # already downloaded

    storage_client = gcs.Client(project=GCP_PROJECT)

    # Find output URI
    gcs_uri = None
    dest = job.dest
    output_info = job.output_info

    if dest and dest.gcs_uri:
        gcs_uri = dest.gcs_uri
    elif output_info and output_info.gcs_output_directory:
        gcs_uri = output_info.gcs_output_directory
    else:
        return None

    # Parse and download
    path = gcs_uri.replace("gs://", "")
    bucket_name, _, blob_path = path.partition("/")
    bucket = storage_client.bucket(bucket_name)

    if blob_path.endswith("/") or "." not in blob_path.split("/")[-1]:
        # Directory — concatenate all files
        prefix = blob_path if blob_path.endswith("/") else blob_path + "/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        if not blobs:
            return None
        with open(out_path, "wb") as fh:
            for blob in blobs:
                if not blob.name.endswith("/"):
                    fh.write(blob.download_as_bytes())
    else:
        # Single file
        blob = bucket.blob(blob_path)
        blob.download_to_filename(out_path)

    return out_path


# ──────────────────────────────────────────────
# Table formatting
# ──────────────────────────────────────────────

def print_table(header: list[str], rows: list[list], right_align: set[int] | None = None):
    """
    Print a neatly formatted table to stdout.

    right_align: set of column indices to right-align (default: none).
    """
    if right_align is None:
        right_align = set()

    all_rows = [header] + rows
    col_widths = [
        max(len(str(row[i])) for row in all_rows)
        for i in range(len(header))
    ]

    def fmt_cell(value, col_idx):
        s = str(value)
        w = col_widths[col_idx]
        return s.rjust(w) if col_idx in right_align else s.ljust(w)

    def fmt_row(row):
        cells = [fmt_cell(v, i) for i, v in enumerate(row)]
        return " " + " │ ".join(cells) + " "

    separator = "─" + "─┼─".join("─" * w for w in col_widths) + "─"

    print()
    print(fmt_row(header))
    print(separator)
    for row in rows:
        print(fmt_row(row))
    print(separator)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    args = parse_args()

    client = genai.Client(
        enterprise=True,
        project=GCP_PROJECT,
        location=GCP_LOCATION,
    )

    # ── Build list of jobs to check ──────────────────────
    jobs_to_check = []

    if args.jobs:
        # Explicit job name(s) on the command line
        for i, name in enumerate(args.jobs, 1):
            jobs_to_check.append({
                "index": i,
                "input_file": "-",
                "job_name": name,
            })
    elif os.path.exists(args.manifest):
        # Read from manifest
        with open(args.manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        ts_now = datetime.now().isoformat()
        run_ts = manifest.get("pipeline_run", "unknown")
        model = manifest.get("model", "unknown")
        total_files = manifest.get("total_files", "?")
        print(f"\nPipeline run : {run_ts}")
        print(f"Model        : {model}")
        print(f"Total files  : {total_files}")
        print(f"Time now     : {ts_now}")

        for entry in manifest.get("jobs", []):
            jobs_to_check.append({
                "index": entry.get("index", 0),
                "input_file": os.path.basename(entry.get("input_file", "-")),
                "job_name": entry["job_name"],
            })
    else:
        print(
            f"No manifest found at '{args.manifest}' and no job names provided.\n"
            f"Run the submit script first, or pass job name(s) as arguments.\n"
            f"Usage: python {os.path.basename(__file__)} [JOB_NAME ...]"
        )
        sys.exit(1)

    if not jobs_to_check:
        print("No jobs to check.")
        return

    # ── Query each job and build table rows ──────────────
    header = ["#", "Input File", "State", "Succeeded", "Failed", "Incomplete"]
    rows = []
    downloaded = []

    state_counts: dict[str, int] = {}

    for entry in jobs_to_check:
        idx = entry["index"]
        input_file = entry["input_file"]
        job_name = entry["job_name"]

        try:
            job = client.batches.get(name=job_name)
            state = get_state_str(job)
            display_state = short_state(state)

            state_counts[display_state] = state_counts.get(display_state, 0) + 1

            # Completion stats
            s = job.completion_stats
            if s:
                succ_str = f"{s.successful_count:,}" if s.successful_count else "0"
                fail_str = f"{s.failed_count:,}" if s.failed_count else "0"
                inc_str = f"{s.incomplete_count:,}" if s.incomplete_count else "0"
            else:
                succ_str = fail_str = inc_str = "-"

            rows.append([idx, input_file, display_state, succ_str, fail_str, inc_str])

            # Download if requested and job is complete
            if args.download and display_state in ("SUCCEEDED", "PARTIALLY_SUCCEEDED"):
                dl_path = download_result(job, idx, args.results_dir)
                if dl_path:
                    downloaded.append(dl_path)

        except Exception as exc:
            short_err = str(exc)[:60]
            rows.append([idx, input_file, f"ERROR", "-", "-", "-"])
            state_counts["ERROR"] = state_counts.get("ERROR", 0) + 1
            print(f"  ⚠  Job {idx}: {short_err}")

    # ── Print table ──────────────────────────────────────
    print_table(header, rows, right_align={0, 3, 4, 5})

    # ── Summary line ─────────────────────────────────────
    total = len(jobs_to_check)
    parts = [f"{count} {state.lower()}" for state, count in sorted(state_counts.items())]
    print(f"  {total} jobs: {', '.join(parts)}")

    # ── Downloaded files ─────────────────────────────────
    if downloaded:
        print(f"\n  Downloaded {len(downloaded)} result file(s) to {args.results_dir}/:")
        for p in downloaded:
            print(f"    • {p}")

    print()


if __name__ == "__main__":
    main()
