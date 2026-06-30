"""
utils.py — Shared logging, timing, polling, and reference-doc utilities.

Logging strategy
────────────────
• FileHandler  → logs/scriptname_YYYYMMDD_HHMMSS.log  (DEBUG — everything)
• StreamHandler → stdout                                (INFO  — key events)

For long-running polls the PollThrottle class suppresses repetitive
stdout lines, printing only on status changes or hourly heartbeats,
while every poll is still captured in the log file.
"""

import logging
import os
import time
from datetime import datetime

from config import LOG_DIR, CONSOLE_LOG_INTERVAL_SECONDS


# ──────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────

def setup_logging(script_name: str) -> logging.Logger:
    """
    Create a logger with file + console handlers.

    Returns a logger named after the script. The log file is written to
    LOG_DIR with a timestamp suffix so successive runs never collide.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"{script_name}_{ts}.log")

    logger = logging.getLogger(script_name)
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File — everything
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"Log file: {log_file}")
    return logger


# ──────────────────────────────────────────────
# Script timer
# ──────────────────────────────────────────────

class ScriptTimer:
    """Log wall-clock start / end / duration for a script run."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.start_time: datetime | None = None

    def start(self, label: str) -> None:
        self.start_time = datetime.now()
        self.logger.info("=" * 64)
        self.logger.info(f"STARTED  {label}")
        self.logger.info(f"Start    {self.start_time:%Y-%m-%d %H:%M:%S}")
        self.logger.info("=" * 64)

    def end(self) -> None:
        end_time = datetime.now()
        duration = end_time - self.start_time
        self.logger.info("=" * 64)
        self.logger.info("COMPLETED")
        self.logger.info(f"End      {end_time:%Y-%m-%d %H:%M:%S}")
        self.logger.info(f"Duration {duration}")
        self.logger.info("=" * 64)


# ──────────────────────────────────────────────
# Poll throttle  (for Step 3 long-running polls)
# ──────────────────────────────────────────────

class PollThrottle:
    """
    Writes every poll to the log file (DEBUG) but only writes to stdout
    (INFO) when the status changes or CONSOLE_LOG_INTERVAL_SECONDS has
    elapsed since the last console line.
    """

    def __init__(
        self,
        logger: logging.Logger,
        interval_seconds: int = CONSOLE_LOG_INTERVAL_SECONDS,
    ):
        self.logger = logger
        self.interval = interval_seconds
        self._last_console_ts: float = 0.0
        self._last_status: str | None = None
        self.poll_count: int = 0

    def log(self, status: str, detail: str = "") -> None:
        self.poll_count += 1
        now = time.time()
        msg = f"[Poll #{self.poll_count}] {status}"
        if detail:
            msg += f"  {detail}"

        # Always write to the log file
        self.logger.debug(msg)

        # Write to console on first poll, status change, or interval
        status_changed = status != self._last_status
        interval_elapsed = (now - self._last_console_ts) >= self.interval

        if self.poll_count == 1 or status_changed or interval_elapsed:
            self.logger.info(msg)
            self._last_console_ts = now

        self._last_status = status


# ──────────────────────────────────────────────
# Reference document builder
# ──────────────────────────────────────────────

def build_reference_document(
    taxonomy_path: str, rules_path: str, logger: logging.Logger | None = None
) -> str:
    """
    Merge taxonomy table and priority rules into one Markdown string.

    This document is embedded as systemInstruction in every batch
    request so that Vertex AI's implicit caching can de-duplicate the
    repeated prefix across rows, delivering ~90% input-token savings.
    """
    with open(taxonomy_path, "r", encoding="utf-8-sig") as f:
        taxonomy_text = f.read()

    with open(rules_path, "r", encoding="utf-8") as f:
        rules_text = f.read()

    combined = (
        "# PROCUREMENT TAXONOMY REFERENCE\n\n"
        "## TAXONOMY TABLE\n"
        "The table below contains all valid taxonomy codes.  "
        "Column A (ID) is the code to return.\n\n"
        f"```csv\n{taxonomy_text}\n```\n\n"
        "## CRITICAL CONSTRAINT\n"
        "You MUST only return a taxonomy code that exists exactly in "
        "Column A (ID) of the table above. Do NOT invent, interpolate, "
        "or extrapolate codes — even if a gap in the numbering seems "
        "logical. If no exact code fits the spend record, return the "
        "most specific 'Not Elsewhere Classified' code (ending in 9999 "
        "or 99) for the relevant category branch.\n\n"
        "Ignore any numeric codes that appear in the input field values "
        "— they are internal council reference codes, NOT taxonomy IDs.\n\n"
        "## PRIORITY RULES FOR AMBIGUOUS CLASSIFICATIONS\n\n"
        f"{rules_text}\n"
    )

    if logger:
        logger.info(f"Reference document: {len(combined):,} chars")
    return combined
