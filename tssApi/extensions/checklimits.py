"""
Limit Tracker - Send limit controller
Runs on port 5013 | Daily reset at 09:00 AM GMT+01:00
"""

import json
import os
import threading
import logging
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ── Config ────────────────────────────────────────────────────────────────────
PORT            = 5013
LIMITS_FILE     = "Limit_lists.txt"
TRACKER_FILE    = "tracker.json"
HISTORY_FILE    = "History.json"
RESET_HOUR      = 9          # 09:00 AM
RESET_MINUTE    = 0
GMT_PLUS_1      = timezone(timedelta(hours=1))

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
CORS(app)   # Allow all origins (needed for browser extension)
lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_tracker() -> dict:
    """Load tracker.json from disk. Returns empty dict if missing/corrupt."""
    if not os.path.exists(TRACKER_FILE):
        return {}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_tracker(data: dict):
    """Persist tracker data to disk."""
    with open(TRACKER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_history() -> list:
    """Load History.json from disk. Returns empty list if missing/corrupt."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_to_history(list_name: str, number_sent: int, added_by: str = None, operation: str = "add"):
    """
    Append a new entry to History.json with timestamp.
    History.json is never cleared by the daily reset.
    operation: "add" or "subtract" to indicate what type of operation was performed
    """
    with lock:
        history = load_history()
        
        # Get current date and time in GMT+1
        now = datetime.now(GMT_PLUS_1)
        
        history_entry = {
            "list_name": list_name,
            "number_sent": number_sent,
            "operation": operation,  # New field to track add vs subtract
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # Only add added_by if it's provided
        if added_by:
            history_entry["added_by"] = added_by
        
        history.append(history_entry)
        
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        
        log_msg = f"[history] {operation} entry for {list_name}: {number_sent} at {now.isoformat()}"
        if added_by:
            log_msg += f" by {added_by}"
        logging.info(log_msg)


def load_limits() -> dict:
    """
    Parse Limit_lists.txt and return {list_name: limit_int}.
    Lines that can't be parsed are silently skipped.
    """
    limits = {}
    if not os.path.exists(LIMITS_FILE):
        return limits
    with open(LIMITS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Split only on the LAST comma so list names with commas work
            parts = line.rsplit(",", 1)
            if len(parts) != 2:
                continue
            name  = parts[0].strip()
            limit_str = parts[1].strip()
            try:
                limits[name] = int(limit_str)
            except ValueError:
                pass   # skip malformed lines
    return limits


def reset_tracker():
    """Wipe tracker.json — called every day at 09:00 AM GMT+1."""
    with lock:
        save_tracker({})
    print(f"[{datetime.now(GMT_PLUS_1).isoformat()}] ✅ Tracker reset at 09:00 AM GMT+1")


# ── Route ─────────────────────────────────────────────────────────────────────



@app.route("/Check_passives_lists", methods=["POST"])
def check_passives_lists():
    """
    Receives: { "liste_name": "..." }
    Returns whether the list exists in Limit_lists.txt.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    liste_name = str(body.get("liste_name", "")).strip()
    if not liste_name:
        return jsonify({"error": "liste_name is required"}), 400

    limits = load_limits()

    if liste_name in limits:
        return jsonify({"status": "existe"})
    else:
        return jsonify({"status": "not_existe"})
        
        
        
        
@app.route("/GetStatus", methods=["POST"])
def get_status_route():
    """
    Receives: { "List_name": "..." }
    Returns the current sent count, limit, and remaining — WITHOUT saving anything.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    list_name = str(body.get("List_name", "")).strip()
    if not list_name:
        return jsonify({"error": "List_name is required"}), 400

    limits  = load_limits()
    tracker = load_tracker()

    already_sent = tracker.get(list_name, 0)

    if list_name not in limits:
        return jsonify({
            "list_name"   : list_name,
            "sent"        : already_sent,
            "limit"       : "no limit",
            "remaining"   : "unlimited"
        })

    limit     = limits[list_name]
    remaining = max(0, limit - already_sent)

    return jsonify({
        "list_name" : list_name,
        "sent"      : already_sent,
        "limit"     : limit,
        "remaining" : remaining
    })


@app.route("/actually_out", methods=["POST"])
def actually_out():
    """
    Receives: { 
        "List_name": "...", 
        "how_many_sents": 500,
        "added_by": "username"  # optional
    }
    Adds how_many_sents to tracker.json and also saves to History.json.
    Returns a success message.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    list_name    = str(body.get("List_name", "")).strip()
    how_many     = body.get("how_many_sents")
    added_by     = body.get("added_by")  # Optional field

    if not list_name:
        return jsonify({"error": "List_name is required"}), 400
    if how_many is None:
        return jsonify({"error": "how_many_sents is required"}), 400
    try:
        how_many = int(how_many)
    except (ValueError, TypeError):
        return jsonify({"error": "how_many_sents must be an integer"}), 400
    if how_many <= 0:
        return jsonify({"error": "how_many_sents must be > 0"}), 400
    
    # Validate added_by if provided (optional, just ensure it's a string)
    if added_by is not None:
        added_by = str(added_by).strip()
        if not added_by:
            added_by = None

    with lock:
        tracker                = load_tracker()
        previous               = tracker.get(list_name, 0)
        tracker[list_name]     = previous + how_many
        save_tracker(tracker)

    # Save to history.json (outside the lock to avoid holding it too long)
    save_to_history(list_name, how_many, added_by, operation="add")
    
    logging.info(f"[actually_out] {list_name} → +{how_many} (total: {tracker[list_name]})")

    return jsonify({
        "status"    : "success",
        "list_name" : list_name,
        "added"     : how_many,
        "new_total" : tracker[list_name]
    })


@app.route("/actually_out_after_in", methods=["POST"])
def actually_out_after_in():
    """
    Receives: { 
        "List_name": "...", 
        "Add_Remaining": 60,
        "added_by": "username"  # optional
    }
    Subtracts Add_Remaining from tracker.json for the specified list.
    Returns a success message with the new total.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    list_name = str(body.get("List_name", "")).strip()
    add_remaining = body.get("Add_Remaining")
    added_by = body.get("added_by")  # Optional field

    if not list_name:
        return jsonify({"error": "List_name is required"}), 400
    if add_remaining is None:
        return jsonify({"error": "Add_Remaining is required"}), 400
    try:
        add_remaining = int(add_remaining)
    except (ValueError, TypeError):
        return jsonify({"error": "Add_Remaining must be an integer"}), 400
    if add_remaining <= 0:
        return jsonify({"error": "Add_Remaining must be > 0"}), 400
    
    # Validate added_by if provided (optional, just ensure it's a string)
    if added_by is not None:
        added_by = str(added_by).strip()
        if not added_by:
            added_by = None

    with lock:
        tracker = load_tracker()
        
        # Check if the list exists in tracker
        if list_name not in tracker:
            return jsonify({
                "status": "error",
                "error": f"List '{list_name}' not found in tracker.json",
                "list_name": list_name
            }), 404
        
        previous = tracker[list_name]
        
        # Check if we have enough to subtract
        if previous < add_remaining:
            return jsonify({
                "status": "error",
                "error": f"Cannot subtract {add_remaining} from {list_name} because current total is {previous}",
                "list_name": list_name,
                "current_total": previous,
                "attempted_subtract": add_remaining
            }), 400
        
        # Perform the subtraction
        tracker[list_name] = previous - add_remaining
        save_tracker(tracker)

    logging.info(f"[actually_out_after_in] {list_name} → -{add_remaining} (old: {previous}, new: {tracker[list_name]})")

    return jsonify({
        "status": "success",
        "list_name": list_name,
        "subtracted": add_remaining,
        "old_total": previous,
        "new_total": tracker[list_name]
    })


@app.route("/status", methods=["GET"])
def status():
    """
    Optional helper endpoint — returns the full current tracker
    and all configured limits, plus next reset time.
    """
    limits  = load_limits()
    tracker = load_tracker()

    now        = datetime.now(GMT_PLUS_1)
    next_reset = now.replace(hour=RESET_HOUR, minute=RESET_MINUTE, second=0, microsecond=0)
    if now >= next_reset:
        next_reset += timedelta(days=1)

    combined = {}
    all_keys = set(list(limits.keys()) + list(tracker.keys()))
    for key in all_keys:
        combined[key] = {
            "sent"     : tracker.get(key, 0),
            "limit"    : limits.get(key, "no limit"),
            "remaining": (limits[key] - tracker.get(key, 0)) if key in limits else "unlimited"
        }

    return jsonify({
        "lists"       : combined,
        "next_reset"  : next_reset.isoformat(),
        "server_time" : now.isoformat()
    })


@app.route("/reset", methods=["POST"])
def manual_reset():
    """Optional manual reset endpoint (useful for testing)."""
    reset_tracker()
    return jsonify({"status": "ok", "message": "Tracker has been reset"})


@app.route("/history", methods=["GET"])
def get_history():
    """
    Optional endpoint to retrieve the entire history.
    """
    history = load_history()
    return jsonify({
        "total_entries": len(history),
        "history": history
    })


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Etc/GMT-1")   # GMT+1
    scheduler.add_job(
        reset_tracker,
        trigger="cron",
        hour=RESET_HOUR,
        minute=RESET_MINUTE,
        id="daily_reset"
    )
    scheduler.start()
    print(f"Scheduler started — tracker resets daily at 09:00 AM GMT+1")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
    print(f"Limit Tracker running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)