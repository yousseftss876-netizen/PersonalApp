"""
Gmail Multi-Account Email Fetcher  —  runs forever until Ctrl+C

- Waits for gmailaccounts_activated.txt to appear if missing
- Uses IMAP INTERNALDATE (when Gmail actually received the email)
  instead of the 'Date' header (which can be forged/customized)
- Auto-detects inbox categories: Primary · Promotions · Social · Updates · Forums
- JSON output matches TSS1_daenerys.targaryen.west@gmail.com.json exactly
"""

import imaplib
import email
import json
import os
import threading
import re
import logging
import signal
import sys
import time
from email.header import decode_header
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
ACCOUNTS_FILE        = "gmailaccounts_activated.txt"
CACHE_DIR            = "gmail_cache"
IMAP_HOST            = "imap.gmail.com"
IMAP_PORT            = 993
MAX_EMAILS           = 50      # emails to fetch per account per cycle
MAX_WORKERS          = 6       # parallel threads
CYCLE_DELAY          = 2      # seconds between full fetch cycles
WAIT_FILE_INTERVAL   = 5       # seconds between checks when accounts file missing
LOG_LEVEL            = logging.INFO

EXTRA_FOLDERS = [
    ("[Gmail]/Spam",      "Spam",   "Spam"),
    ("[Gmail]/Sent Mail", "Sent",   "Sent"),
    ("[Gmail]/Drafts",    "Drafts", "Drafts"),
]

INBOX_CATEGORIES = [
    ("",           "Primary"),
    ("Promotions", "Promotions"),
    ("Social",     "Social"),
    ("Updates",    "Updates"),
    ("Forums",     "Forums"),
]
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_stop = False


def handle_sigint(sig, frame):
    global _stop
    print()
    log.info("Ctrl+C received — finishing current cycle then exiting …")
    _stop = True


signal.signal(signal.SIGINT, handle_sigint)


# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_mime_words(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded).strip()


def parse_internaldate(imap_response_line: str):
    """
    Extract INTERNALDATE from an IMAP fetch response line.
    Format: INTERNALDATE "22-Apr-2026 09:30:00 +0000"
    Returns (human_label, unix_timestamp) or ("", None) on failure.
    This is the real server-received time — immune to header forgery.
    """
    m = re.search(r'INTERNALDATE\s+"([^"]+)"', imap_response_line, re.IGNORECASE)
    if not m:
        return ("", None)
    raw = m.group(1)
    try:
        # imaplib provides imaplib.Time2Internaldate but parsing is easier directly
        dt  = datetime.strptime(raw, "%d-%b-%Y %H:%M:%S %z")
        ts  = dt.timestamp()
        now = datetime.now(timezone.utc).timestamp()
        diff = now - ts
        if diff < 3600:
            label = f"{max(1, int(diff // 60))} min"
        elif diff < 86400:
            label = f"{int(diff // 3600)} h"
        else:
            label = f"{int(diff // 86400)} days"
        return (label, round(ts, 1))
    except Exception:
        return (raw, None)


def parse_from(raw) -> tuple[str, str]:
    # Force conversion to string to handle email.header.Header objects or None
    raw_str = str(raw or "")
    
    m = re.match(r'^(.*?)\s<(.+?)>\s*$', raw_str)
    if m:
        name = decode_mime_words(m.group(1)).strip().strip('"')
        addr = m.group(2).strip()
    else:
        name = ""
        addr = raw_str.strip()
    return name, addr


# ── Account file loading (waits if missing) ───────────────────────────────────

def wait_for_accounts_file() -> list:
    """
    Block until gmailaccounts.txt exists and has at least one valid account.
    Checks every WAIT_FILE_INTERVAL seconds.
    Returns the parsed account list.
    """
    warned = False
    while not _stop:
        if os.path.exists(ACCOUNTS_FILE):
            accounts = parse_accounts(ACCOUNTS_FILE)
            if accounts:
                if warned:
                    log.info("Found %s — resuming.", ACCOUNTS_FILE)
                return accounts
            else:
                if not warned:
                    log.warning("%s exists but contains no valid accounts. Waiting …",
                                ACCOUNTS_FILE)
                    warned = True
        else:
            if not warned:
                log.warning("%s not found. Waiting for it to appear …", ACCOUNTS_FILE)
                warned = True
        time.sleep(WAIT_FILE_INTERVAL)
    return []


def parse_accounts(filepath: str) -> list:
    accounts = []
    try:
        with open(filepath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    continue
                group    = parts[0]
                addr     = parts[1]
                password = parts[2].replace(" ", "")
                tag      = parts[3] if len(parts) > 3 else None
                accounts.append({
                    "group":    group,
                    "email":    addr,
                    "password": password,
                    "tag":      tag,
                })
    except Exception as exc:
        log.error("Could not read %s: %s", filepath, exc)
    return accounts


def cache_path(group: str, addr: str) -> str:
    return os.path.join(CACHE_DIR, f"{group}_{addr}.json")


# ── Folder discovery ──────────────────────────────────────────────────────────

def list_all_folders(conn: imaplib.IMAP4_SSL) -> set:
    try:
        status, data = conn.list()
        if status != "OK":
            return set()
    except Exception:
        return set()

    folders = set()
    for item in data:
        if not item:
            continue
        raw = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
        m = re.search(r'"([^"]+)"\s*$', raw)
        if m:
            folders.add(m.group(1).lower())
        else:
            parts = raw.rsplit(None, 1)
            if parts:
                folders.add(parts[-1].strip().lower())
    return folders


def discover_inbox_folders(conn: imaplib.IMAP4_SSL) -> list:
    """
    Returns list of (imap_path, folder_field, folder_name_field).
    folder_field      → JSON "folder"       e.g. "Inbox/Promotions"
    folder_name_field → JSON "folder_name"  e.g. "Inbox"
    """
    existing = list_all_folders(conn)
    result   = []

    for suffix, label in INBOX_CATEGORIES:
        if suffix == "":
            result.append(("INBOX", "Inbox", "Inbox"))
        else:
            for sep in ("/", "."):
                imap_path = f"INBOX{sep}{suffix}"
                if imap_path.lower() in existing:
                    result.append((imap_path, f"Inbox/{label}", "Inbox"))
                    break

    return result


# ── IMAP fetch ────────────────────────────────────────────────────────────────

def fetch_folder(conn: imaplib.IMAP4_SSL,
                 imap_path: str,
                 folder_field: str,
                 folder_name_field: str,
                 limit: int) -> list:
    try:
        status, _ = conn.select(f'"{imap_path}"', readonly=True)
        if status != "OK":
            return []
    except Exception:
        return []

    try:
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
    except Exception:
        return []

    uids = data[0].split()
    uids = list(reversed(uids[-limit:]))   # newest UID = most recently received

    if not uids:
        return []

    uid_set = b",".join(uids)
    try:
        # Fetch INTERNALDATE (real received time) + FROM + SUBJECT headers
        # We deliberately skip the "Date" header to avoid using forged dates
        status, msg_data = conn.uid(
            "FETCH", uid_set,
            "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
        )
        if status != "OK":
            return []
    except Exception:
        return []

    results = []
    for item in msg_data:
        if not isinstance(item, tuple):
            continue

        # item[0] contains INTERNALDATE and UID in its string representation
        raw_info = item[0].decode(errors="replace")

        uid_match = re.search(r"UID\s+(\d+)", raw_info)
        uid_str   = uid_match.group(1) if uid_match else "0"

        # ── Real received time from server — not the Date header ──────────
        date_label, date_ts = parse_internaldate(raw_info)

        msg                  = email.message_from_bytes(item[1])
        from_name, from_addr = parse_from(msg.get("From", ""))
        subject              = decode_mime_words(msg.get("Subject", "(no subject)"))

        results.append({
            "uid":            uid_str,
            "folder":         folder_field,
            "from_name":      from_name,
            "from_email":     from_addr,
            "subject":        subject,
            "date":           date_label,
            "date_timestamp": date_ts,
            "folder_name":    folder_name_field,
        })

    return results


def fetch_account(account: dict) -> dict:
    group    = account["group"]
    addr     = account["email"]
    password = account["password"]

    log.info("[%s] %-42s  connecting …", group, addr)

    emails    = []
    error_msg = None
    conn      = None

    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        conn.login(addr, password)

        inbox_folders = discover_inbox_folders(conn)
        log.debug("[%s] %s  inbox tabs: %s",
                  group, addr, [f[1] for f in inbox_folders])

        all_folders = inbox_folders + [
            (ip, ff, fn) for ip, ff, fn in EXTRA_FOLDERS
        ]

        # Fetch up to MAX_EMAILS candidates from EACH folder, then sort
        # globally by real received time and keep only the latest MAX_EMAILS.
        # This ensures e.g. 30 Inbox + 20 Spam can surface as the top 50
        # instead of Inbox alone consuming the entire budget.
        all_candidates = []
        for imap_path, folder_field, folder_name_field in all_folders:
            fetched = fetch_folder(conn, imap_path,
                                   folder_field, folder_name_field,
                                   MAX_EMAILS)
            all_candidates.extend(fetched)
            if fetched:
                log.debug("[%s] %s  %-32s  %d msg(s)",
                          group, addr, imap_path, len(fetched))

        # Sort descending by INTERNALDATE timestamp; emails with no timestamp
        # (parse failure) go to the bottom via fallback 0.
        all_candidates.sort(key=lambda e: e.get("date_timestamp") or 0, reverse=True)
        emails = all_candidates[:MAX_EMAILS]

        log.info("[%s] %-42s  ✓  %d emails (from %d candidates across %d folders)",
                 group, addr, len(emails), len(all_candidates), len(all_folders))

    except imaplib.IMAP4.error as exc:
        error_msg = f"IMAP error: {exc}"
        log.error("[%s] %-42s  ✗  %s", group, addr, error_msg)
    except OSError as exc:
        error_msg = f"Network error: {exc}"
        log.error("[%s] %-42s  ✗  %s", group, addr, error_msg)
    except Exception as exc:
        error_msg = f"Unexpected: {exc}"
        log.exception("[%s] %-42s  ✗  %s", group, addr, error_msg)
    finally:
        # Always release the IMAP socket. Without this, a single login or
        # fetch error would leak the SSL file descriptor — which over time
        # causes "OSError: [Errno 24] Too many open files" and brings the
        # whole Flask process down.
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                try:
                    conn.shutdown()
                except Exception:
                    pass
            try:
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    return {
        "account":     account,
        "emails":      emails,
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
        "email_count": len(emails),
        "status":      "idle" if not error_msg else "error",
        "error":       error_msg,
    }


def save_result(result: dict) -> None:
    acc  = result["account"]
    path = cache_path(acc["group"], acc["email"])

    payload = {
        "emails":      result["emails"],
        "fetched_at":  result["fetched_at"],
        "email_count": result["email_count"],
        "status":      result["status"],
        "error":       result["error"],
    }

    # Atomic write: write to a temp file in the same directory, then rename.
    # Prevents readers (app.py /fetch_emails, SSE loop) from ever seeing a
    # missing or partially-written JSON file, which previously caused
    # FileNotFoundError / JSONDecodeError and made the service look "broken".
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

    log.info("Saved  →  %s  (%d emails)", path, result["email_count"])


# ── Per-account loop (runs forever in its own thread) ─────────────────────────

def account_loop(account: dict, stop_evt=None) -> None:
    """
    Each account runs independently:
      fetch -> save immediately -> wait CYCLE_DELAY -> repeat.
    stop_evt: threading.Event — set it to cleanly stop just this account's thread.
    """
    import threading
    if stop_evt is None:
        stop_evt = threading.Event()

    addr = account["email"]
    while not _stop and not stop_evt.is_set():
        try:
            result = fetch_account(account)
            save_result(result)
        except Exception as exc:
            log.error("Thread crash for %s: %s", addr, exc)

        # Wait CYCLE_DELAY seconds before next fetch, checking stop every second
        for _ in range(CYCLE_DELAY):
            if _stop or stop_evt.is_set():
                return
            time.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import threading

    Path(CACHE_DIR).mkdir(exist_ok=True)
    log.info("Gmail Fetcher started — Ctrl+C to stop")
    log.info("Cache folder : %s/", CACHE_DIR)
    log.info("Inbox tabs   : Primary · Promotions · Social · Updates · Forums (auto-detected)")
    log.info("Date source  : INTERNALDATE (real server received time, not Date header)")
    log.info("Mode         : each account loops independently — saves immediately, waits %ds",
             CYCLE_DELAY)
    log.info("Hot-reload   : accounts file re-checked every %ds for additions/removals\n",
             CYCLE_DELAY)

    # active_threads: email -> {"future": Future, "stop": threading.Event}
    active_threads: dict = {}
    executor = ThreadPoolExecutor(max_workers=64)

    def start_account(acc: dict) -> None:
        stop_evt = threading.Event()
        future   = executor.submit(account_loop, acc, stop_evt)
        active_threads[acc["email"]] = {"future": future, "stop": stop_evt}
        log.info("▶  Started thread for %s", acc["email"])

    def stop_account(addr: str) -> None:
        entry = active_threads.pop(addr, None)
        if entry:
            entry["stop"].set()
            log.info("■  Stopped thread for %s", addr)

    try:
        while not _stop:
            current_accounts = parse_accounts(ACCOUNTS_FILE) if os.path.exists(ACCOUNTS_FILE) else []
            current_emails   = {acc["email"] for acc in current_accounts}
            running_emails   = set(active_threads.keys())

            # Start threads for newly added accounts
            for acc in current_accounts:
                if acc["email"] not in running_emails:
                    start_account(acc)

            # Stop threads for removed accounts
            for addr in list(running_emails - current_emails):
                stop_account(addr)

            if not os.path.exists(ACCOUNTS_FILE):
                log.warning("%s not found — keeping existing threads running", ACCOUNTS_FILE)
            elif not current_accounts and not active_threads:
                log.warning("%s has no valid accounts — waiting …", ACCOUNTS_FILE)

            time.sleep(CYCLE_DELAY)

    except KeyboardInterrupt:
        pass

    log.info("Shutting down …")
    for entry in list(active_threads.values()):
        entry["stop"].set()
    executor.shutdown(wait=False)
    log.info("Stopped cleanly. Goodbye.")


if __name__ == "__main__":
    main()
