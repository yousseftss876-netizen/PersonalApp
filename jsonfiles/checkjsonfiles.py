#!/usr/bin/env python3
"""
Drop File Cleaner Service
Runs on port 5413 and automatically deletes JSON drop files from previous days.
File format: drop_[HH]_[DD]_[MM]_[YYYY].json
"""

import os
import glob
import logging
import threading
from datetime import datetime, date
from flask import Flask, jsonify

# ── Configuration ──────────────────────────────────────────────────────────────
PORT = 5413
# Folder where drop_*.json files live (change this to your actual path)
DROP_FILES_DIR = os.environ.get("DROP_FILES_DIR", ".")
# How often to check for old files (seconds). Default: every hour
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 3600))

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)


# ── Core logic ─────────────────────────────────────────────────────────────────

def parse_drop_date(filename: str) -> date | None:
    """
    Parse a drop filename and return its date.
    Expected format: drop_HH_DD_MM_YYYY.json
    Returns None if the filename doesn't match.
    """
    basename = os.path.basename(filename)
    # Strip extension
    if not basename.endswith(".json"):
        return None
    name = basename[:-5]  # remove .json

    parts = name.split("_")
    # Expect exactly 5 parts: ['drop', HH, DD, MM, YYYY]
    if len(parts) != 5 or parts[0] != "drop":
        return None

    try:
        day   = int(parts[2])
        month = int(parts[3])
        year  = int(parts[4])
        return date(year, month, day)
    except ValueError:
        return None


def delete_old_files(dry_run: bool = False) -> dict:
    """
    Scan DROP_FILES_DIR for drop_*.json files whose date < today and delete them.
    Returns a summary dict.
    """
    today = date.today()
    pattern = os.path.join(DROP_FILES_DIR, "drop_*.json")
    candidates = glob.glob(pattern)

    deleted = []
    skipped = []
    errors  = []

    for filepath in sorted(candidates):
        file_date = parse_drop_date(filepath)

        if file_date is None:
            log.debug("Skipping (unrecognised format): %s", filepath)
            skipped.append(filepath)
            continue

        if file_date < today:
            if dry_run:
                log.info("[DRY-RUN] Would delete: %s  (date=%s)", filepath, file_date)
                deleted.append(filepath)
            else:
                try:
                    os.remove(filepath)
                    log.info("Deleted: %s  (date=%s)", filepath, file_date)
                    deleted.append(filepath)
                except OSError as exc:
                    log.error("Failed to delete %s: %s", filepath, exc)
                    errors.append({"file": filepath, "error": str(exc)})
        else:
            log.debug("Keeping (today or future): %s  (date=%s)", filepath, file_date)

    summary = {
        "today":        str(today),
        "dry_run":      dry_run,
        "deleted":      deleted,
        "deleted_count": len(deleted),
        "skipped":      skipped,
        "errors":       errors,
        "checked_at":   datetime.now().isoformat(timespec="seconds"),
    }
    log.info(
        "Cleanup done — deleted %d file(s), %d skipped, %d error(s)",
        len(deleted), len(skipped), len(errors),
    )
    return summary


# ── Background scheduler ───────────────────────────────────────────────────────

def _scheduler_loop():
    """Run delete_old_files() immediately, then repeat every CHECK_INTERVAL seconds."""
    while True:
        log.info("Scheduler: running cleanup …")
        delete_old_files()
        # Sleep in small chunks so the thread exits cleanly if the process dies
        remaining = CHECK_INTERVAL
        while remaining > 0:
            threading.Event().wait(min(remaining, 60))
            remaining -= 60


def start_scheduler():
    thread = threading.Thread(target=_scheduler_loop, daemon=True, name="CleanerScheduler")
    thread.start()
    log.info(
        "Scheduler started — checking every %d second(s) for old drop files in: %s",
        CHECK_INTERVAL,
        os.path.abspath(DROP_FILES_DIR),
    )


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Drop File Cleaner",
        "drop_files_dir": os.path.abspath(DROP_FILES_DIR),
        "check_interval_seconds": CHECK_INTERVAL,
        "today": str(date.today()),
        "endpoints": {
            "GET  /":           "Service info",
            "GET  /status":     "List files and their status",
            "POST /cleanup":    "Run cleanup now (deletes old files)",
            "POST /dry-run":    "Simulate cleanup without deleting",
        },
    })


@app.route("/status", methods=["GET"])
def status():
    """List all drop files and say whether they would be deleted."""
    today = date.today()
    pattern = os.path.join(DROP_FILES_DIR, "drop_*.json")
    files = []

    for filepath in sorted(glob.glob(pattern)):
        file_date = parse_drop_date(filepath)
        files.append({
            "file":      os.path.basename(filepath),
            "date":      str(file_date) if file_date else None,
            "would_delete": (file_date < today) if file_date else False,
        })

    return jsonify({
        "today":       str(today),
        "total_files": len(files),
        "files":       files,
    })


@app.route("/cleanup", methods=["POST"])
def cleanup():
    """Trigger an immediate cleanup (actually deletes files)."""
    result = delete_old_files(dry_run=False)
    return jsonify(result)


@app.route("/dry-run", methods=["POST"])
def dry_run():
    """Simulate a cleanup without deleting anything."""
    result = delete_old_files(dry_run=True)
    return jsonify(result)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting Drop File Cleaner on port %d", PORT)
    log.info("Watching directory: %s", os.path.abspath(DROP_FILES_DIR))

    start_scheduler()

    # use_reloader=False so the scheduler thread isn't duplicated by Flask's reloader
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)