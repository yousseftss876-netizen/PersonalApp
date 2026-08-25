import os
import imaplib
import email
import email.utils
from email.header import decode_header
from datetime import datetime, timezone, timedelta
import logging
import json
import re
import time
import urllib.parse
import dns.resolver
import mysql.connector
import tldextract
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from flask import Flask, render_template, request, flash, jsonify, redirect, url_for, session, Response, stream_with_context, make_response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_cors import CORS

# Custom logger to include username in werkzeug logs
class UserLogFilter(logging.Filter):
    def filter(self, record):
        try:
            from flask import has_request_context
            # Ensure remote_addr is always present to prevent KeyError in some environments
            if not hasattr(record, 'remote_addr'):
                record.remote_addr = '127.0.0.1'
                
            if has_request_context():
                from flask_login import current_user
                user = "anonymous"
                try:
                    if current_user and current_user.is_authenticated:
                        user = current_user.username
                except:
                    pass
                record.user = user
            else:
                record.user = "system"
        except:
            record.user = "unknown"
        return True

app = Flask(__name__)
CORS(app, resources={
    r"/check_email": {"origins": "*"},
    r"/count_emails": {"origins": "*"},
    r"/save_db": {"origins": "*"}
})
app.secret_key = os.environ.get("SESSION_SECRET")

import threading as _threading

# Recover interrupted processes after any restart — run in background so the
# worker can bind and serve immediately instead of blocking gunicorn startup.
# (domain_founder.recover_interrupted_processes() relaunches real DNS/SPF/
# Namecheap work for any process left running; doing that synchronously here
# was enough to blow past gunicorn's --timeout during boot.)
import subdomain_finder as _sf_mod
import domain_founder as _df_mod

def _run_recovery():
    with app.app_context():
        try:
            _sf_mod.recover_interrupted_processes()
        except Exception as _e:
            logging.error(f"subdomain_finder recovery error: {_e}")
        try:
            _df_mod.recover_interrupted_processes()
        except Exception as _e:
            logging.error(f"domain_founder recovery error: {_e}")

_threading.Thread(target=_run_recovery, daemon=True, name="startup-recovery").start()


DB_CONFIG = {
    'host': '203.161.41.70',
    'port': 3306,
    'database': 'tss_dashboard',
    'user': 'Tssdatabse',
    'password': 's7yA7Zes17aAAn'
}

API_PASSWORD = "PAsswOrdCheckker"


# Update werkzeug logging format
@app.before_request
def check_session_validity():
    if request.endpoint in ('login', 'static'):
        return
    if current_user.is_authenticated:
        login_time = session.get('login_time', 0)
        sc = load_sessions_control()
        if login_time < sc.get('force_all_relogin_at', 0):
            logout_user()
            flash('Your session has ended due to a system update. Please log in again to continue.', 'info')
            return redirect(url_for('login'))
        invalidated = sc.get('invalidated_users', {})
        if current_user.username in invalidated and login_time < invalidated[current_user.username]:
            logout_user()
            flash('Your session has been ended. Please log in again.', 'info')
            return redirect(url_for('login'))

@app.before_request
def setup_logging():
    if not hasattr(app, '_logging_setup_done'):
        # Using a safer format that doesn't rely on remote_addr or specific attributes
        # We include [user] which we populate in the Filter
        log_format = '[%(user)s] %(message)s'
        werkzeug_logger = logging.getLogger('werkzeug')
        
        # Remove existing handlers to avoid duplicates
        for handler in werkzeug_logger.handlers[:]:
            werkzeug_logger.removeHandler(handler)
        
        handler = logging.StreamHandler()
        handler.addFilter(UserLogFilter())
        handler.setFormatter(logging.Formatter(log_format))
        werkzeug_logger.addHandler(handler)
        werkzeug_logger.propagate = False
        app._logging_setup_done = True
import ip_checker

try:
    import resource as _resource
    _soft_fd, _hard_fd = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    _target_fd = min(65536, _hard_fd) if _hard_fd > 0 else 65536
    if _soft_fd < _target_fd:
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (_target_fd, _hard_fd))
        logging.info(f"Raised file descriptor soft limit from {_soft_fd} to {_target_fd}")
except Exception as _fd_err:
    logging.warning(f"Could not raise file descriptor limit: {_fd_err}")


GMAIL_ACCOUNTS_FILE = 'gmailaccounts.txt'
_gmail_accounts_cache = {'mtime': 0, 'accounts': {}, 'news_accounts': {}}
_gmail_accounts_lock = _threading.Lock()


def _load_gmail_accounts_from_file():
    """Read gmailaccounts.txt and return (accounts, news_accounts) dicts.

    Cached by file mtime — re-reads only when the file changes. No IMAP
    connections, no background threads, no per-account state held in memory.
    This replaces the previous EntityBasedGmailManager which kept long-lived
    IMAP sockets and health-monitor threads for every account (the source of
    the OOM when users clicked accounts in TSS Gmail Access).
    """
    try:
        mtime = os.path.getmtime(GMAIL_ACCOUNTS_FILE)
    except OSError:
        mtime = 0

    with _gmail_accounts_lock:
        if mtime == _gmail_accounts_cache['mtime'] and _gmail_accounts_cache['accounts']:
            return _gmail_accounts_cache['accounts'], _gmail_accounts_cache['news_accounts']

        accounts = {}
        news_accounts = {}
        try:
            with open(GMAIL_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    try:
                        parts = line.split(',')
                        if len(parts) < 3:
                            continue
                        entity = parts[0].strip().upper()
                        email_addr = parts[1].strip()
                        app_password = parts[2].strip()
                        is_news = len(parts) >= 4 and parts[3].strip().lower() == 'news'
                        account_key = f"{entity}_{email_addr}"
                        account_info = {
                            'entity': entity,
                            'email': email_addr,
                            'app_password': app_password,
                            'display_name': f"{entity} - {email_addr}",
                            'is_news': is_news,
                        }
                        if is_news:
                            news_accounts[account_key] = account_info
                        else:
                            accounts[account_key] = account_info
                    except Exception as e:
                        logging.error(f"Error parsing gmailaccounts.txt line {line_num}: {e}")
        except FileNotFoundError:
            logging.error("gmailaccounts.txt file not found")
        except Exception as e:
            logging.error(f"Error reading gmailaccounts.txt: {e}")

        _gmail_accounts_cache['mtime'] = mtime
        _gmail_accounts_cache['accounts'] = accounts
        _gmail_accounts_cache['news_accounts'] = news_accounts
        return accounts, news_accounts


def _invalidate_gmail_accounts_cache():
    """Force the next _load_gmail_accounts_from_file() call to re-read the file."""
    with _gmail_accounts_lock:
        _gmail_accounts_cache['mtime'] = 0


def _get_user_gmail_accounts(user_entity):
    """Return Gmail accounts visible to a user based on their entity (no IMAP)."""
    user_entity = (user_entity or '').upper()
    accounts, _ = _load_gmail_accounts_from_file()
    if user_entity == 'TSSW':
        return accounts
    return {k: a for k, a in accounts.items() if a['entity'] == user_entity}


def _get_user_news_accounts(user_entity):
    """Return news Gmail accounts visible to a user based on their entity."""
    user_entity = (user_entity or '').upper()
    _, news_accounts = _load_gmail_accounts_from_file()
    if user_entity == 'TSSW':
        return news_accounts
    return {k: a for k, a in news_accounts.items() if a['entity'] == user_entity}

# Gmail JSON cache directory
GMAIL_CACHE_DIR = 'gmail_cache'
os.makedirs(GMAIL_CACHE_DIR, exist_ok=True)

# Activated Gmail accounts file (same format as gmailaccounts.txt)
GMAIL_ACTIVATED_FILE = 'gmailaccounts_activated.txt'
_activated_lock = _threading.Lock()

def load_activated_keys():
    """Return a set of account_keys present in gmailaccounts_activated.txt."""
    keys = set()
    try:
        if os.path.exists(GMAIL_ACTIVATED_FILE):
            with open(GMAIL_ACTIVATED_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = line.split(',')
                        if len(parts) >= 2:
                            entity = parts[0].strip().upper()
                            email_addr = parts[1].strip()
                            keys.add(f"{entity}_{email_addr}")
    except Exception as e:
        logging.error(f"Error reading {GMAIL_ACTIVATED_FILE}: {e}")
    return keys

def _activate_account(account_key, account_info):
    """Append account to gmailaccounts_activated.txt (idempotent)."""
    with _activated_lock:
        if account_key in load_activated_keys():
            return
        line = f"{account_info['entity']},{account_info['email']},{account_info['app_password']}\n"
        with open(GMAIL_ACTIVATED_FILE, 'a', encoding='utf-8') as f:
            f.write(line)

def _deactivate_account(account_key):
    """Remove account from gmailaccounts_activated.txt."""
    with _activated_lock:
        if not os.path.exists(GMAIL_ACTIVATED_FILE):
            return
        with open(GMAIL_ACTIVATED_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                parts = stripped.split(',')
                if len(parts) >= 2:
                    key = f"{parts[0].strip().upper()}_{parts[1].strip()}"
                    if key == account_key:
                        continue
            new_lines.append(line)
        with open(GMAIL_ACTIVATED_FILE, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

import domain_founder
import email_founder
import subdomain_finder
import gzip as _gzip

# Root logger at INFO. DEBUG here would also make every imported library
# (dns.resolver, requests, imaplib, urllib3, ...) log through the root
# handler — Python's logging module serializes writes through one global
# lock, so heavy concurrent thread activity (e.g. domain_founder recovery)
# logging at DEBUG adds real contention on top of normal GIL contention.
# Enable DEBUG on a specific logger when you need to trace something, e.g.:
#   logging.getLogger('domain_founder').setLevel(logging.DEBUG)
logging.basicConfig(level=logging.INFO)

def _fmt_bytes(n):
    """Format a byte count into a human-readable string."""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MB"
    elif n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"

@app.after_request
def compress_and_log_response(response):
    """Gzip-compress JSON responses and log response size for every request."""
    # Skip streaming / passthrough responses (SSE, file downloads, etc.)
    if response.direct_passthrough:
        return response

    try:
        data = response.get_data()
    except Exception:
        return response

    original_size = len(data) if data else 0

    is_json = response.content_type and 'application/json' in response.content_type
    accept_gzip = 'gzip' in request.headers.get('Accept-Encoding', '')

    # --- Gzip compression ---
    compressed = False
    if is_json and accept_gzip and original_size > 512:
        try:
            gz_data = _gzip.compress(data, compresslevel=6)
            response.set_data(gz_data)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(gz_data))
            compressed = True
        except Exception:
            response.headers['Content-Length'] = str(original_size)
    elif original_size:
        response.headers.setdefault('Content-Length', str(original_size))

    # --- Size logging (skip tiny/static responses) ---
    if original_size > 0 and not request.path.startswith('/static'):
        wire_size = int(response.headers.get('Content-Length', original_size))
        if compressed:
            ratio = int((1 - wire_size / original_size) * 100)
            size_str = f"{_fmt_bytes(wire_size)} gz ({_fmt_bytes(original_size)} raw, -{ratio}% saved)"
        else:
            size_str = _fmt_bytes(wire_size)
        app.logger.info(f"[SIZE] {request.method} {request.path} → {size_str}")

    return response


if not app.secret_key:
    # No SESSION_SECRET env var — generate a stable random secret and persist it
    # so sessions survive app restarts without requiring manual configuration.
    _secret_file = os.path.join(os.path.dirname(__file__), '.session_secret')
    try:
        if os.path.exists(_secret_file):
            with open(_secret_file, 'r') as _f:
                app.secret_key = _f.read().strip()
        else:
            import secrets as _secrets
            app.secret_key = _secrets.token_hex(32)
            with open(_secret_file, 'w') as _f:
                _f.write(app.secret_key)
    except Exception:
        import secrets as _secrets
        app.secret_key = _secrets.token_hex(32)


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Please log in to access your emails.'
login_manager.login_message_category = 'info'

# DNS Resolver Configuration
resolver = dns.resolver.Resolver()
resolver.timeout = 5
resolver.lifetime = 10
resolver.retries = 3
resolver.nameservers = ['8.8.8.8', '1.1.1.1']

# Dedicated resolver for blacklist lookups (with retries for reliability)
blacklist_resolver = dns.resolver.Resolver()
blacklist_resolver.timeout = 5
blacklist_resolver.lifetime = 10
blacklist_resolver.retries = 3
blacklist_resolver.nameservers = ['8.8.8.8', '1.1.1.1']

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, username, entity, name=None, has_toggle_permission=False, has_news_permission=False, has_domain_checker_permission=False, has_find_news_permission=False, has_extract_emails_permission=False, has_tssw_report_permission=False, has_gmass_permission=False, has_blacklist_lookup_permission=False):
        self.id = username
        self.username = username
        self.entity = entity
        self.name = name or username
        self.has_toggle_permission = has_toggle_permission
        self.has_news_permission = has_news_permission
        self.has_domain_checker_permission = has_domain_checker_permission
        self.has_find_news_permission = has_find_news_permission
        self.has_extract_emails_permission = has_extract_emails_permission
        self.has_tssw_report_permission = has_tssw_report_permission
        self.has_gmass_permission = has_gmass_permission
        self.has_blacklist_lookup_permission = has_blacklist_lookup_permission
        self.has_download_extension_permission = False
        self.has_add_extensions_permission = False
        self.has_quality_helper_permission = False
        self.has_news_subscription_permission = False
        self.has_user_management_permission = False
        self.has_ips_cheker_permission = False
        self.has_add_ip_cheker_permission = False
        self.has_domain_founder_permission = False
        self.has_unlimited_domain_founder_permission = False
        self.has_email_founder_permission = False
        self.has_access_management_permission = False
        self.has_subdomain_finder_permission = False
        self.has_processes_management_permission = False
        self.has_warmup_lists_permission = False
        self.has_warmup_lists_admin_permission = False
        self.has_warmup_history_permission = False
        self.has_add_warmup_record_permission = False
        self.has_warmup_reports_permission = False
        self.has_warmup_sessions_permission = False
        self.has_display_extensions_users_permission = False
        self.max_processes = 1  # Default limit
        self.email_founder_max_processes = 10  # Default Email Founder limit
        self.domain_quota = 0   # Default quota (0 means unlimited or not set)
        self.domains_processed_this_month = 0
        self.sf_max_processes = 1   # SubDomain Finder: max concurrent processes
        self.sf_max_domains = 0     # SubDomain Finder: max domains per process (0=unlimited)
        self.sf_stop_at = 0         # SubDomain Finder: stop process when collected subdomains reach this (0=unlimited)
        self.df_max_processes = 1   # Domain Founder: max concurrent processes

@app.route('/save_db', methods=['POST'])
def save_db():
    try:
        data = request.get_json()

        # 🔐(password check)
        if not data or data.get("password") != API_PASSWORD:
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # =========================
        # 1. INSERT drops_details
        # =========================
        drop_data = data.get("drop_details", {})

        drop_query = """
        INSERT INTO drops_details
        (entity, drop_date, drop_time, total_in, total_out, list_type, tolerance, delivery_rate)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """

        cursor.execute(drop_query, (
            drop_data.get("entity"),
            drop_data.get("drop_date"),
            drop_data.get("drop_time"),
            drop_data.get("total_in"),
            drop_data.get("total_out"),
            drop_data.get("list_type"),
            drop_data.get("tolerance"),
            drop_data.get("delivery_rate")
        ))

        drop_id = cursor.lastrowid



        # =========================
        # 3. INSERT ips_send_details
        # =========================
        serveurs = data.get("serveurs", [])

        for srv in serveurs:
            ips = srv.get("ips", [])
        
            for ip in ips:
                ip_query = """
                INSERT INTO ips_send_details
                (drop_id, ip, total_in, total_out, delivery_rate)
                VALUES (%s,%s,%s,%s,%s)
                """
        
                cursor.execute(ip_query, (
                    drop_id,
                    ip.get("ip"),
                    ip.get("total_in"),
                    ip.get("total_out"),
                    ip.get("delivery_rate")
                ))

        # =========================
        # 4. INSERT emailcount_drop_details
        # =========================
        emails = data.get("emails", [])

        for em in emails:
            email_query = """
            INSERT INTO emailcount_drop_details
            (drop_id, gmail_account, From_email, subject_email, total_inbox, total_spam)
            VALUES (%s,%s,%s,%s,%s,%s)
            """

            cursor.execute(email_query, (
                drop_id,
                em.get("gmail_account"),
                em.get("From_email"),
                em.get("subject_email"),
                em.get("total_inbox"),
                em.get("total_spam")
            ))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"success": True, "drop_id": drop_id})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

def get_real_arrival_time(email_message):
    """Extract real delivery time from Received headers"""
    received_headers = email_message.get_all('Received', [])
    
    if not received_headers:
        return None
    
    # The first Received header (top-most) is Gmail's receipt time
    # This is the most accurate arrival time
    first_received = received_headers[0]
    
    # Extract date after semicolon
    date_match = re.search(r';\s+(.*)$', first_received)
    if date_match:
        try:
            return email.utils.parsedate_to_datetime(date_match.group(1))
        except:
            pass
    
    return None


# ── Bounded execution for public, synchronous CORS endpoints ───────────────
#
# /check_email and /count_emails are called synchronously by an external
# Chrome extension expecting an immediate JSON response — they can't be
# converted to the task_id/polling pattern used by /extract_emails without
# also updating that extension. But connect_to_gmail() has a legitimate
# worst case of several minutes (5 retries, up to 60s each, with growing
# backoff sleeps in between), and count_emails_in_folders() can loop over
# hundreds of messages on top of that. Left unbounded, a single slow/stuck
# Gmail account can occupy a request thread long enough for gunicorn to
# decide the whole worker is dead (WORKER TIMEOUT), taking every other
# in-flight request down with it.
#
# _run_bounded() runs the blocking call on a small dedicated thread pool and
# gives up after `timeout` seconds, returning a clean "timeout" error to the
# caller instead. The underlying call keeps running in the background until
# it finishes on its own (IMAP/DNS libraries have their own timeouts), but
# the request thread — and therefore the gunicorn worker — is never held
# hostage by it.
_BOUNDED_CALL_POOL = ThreadPoolExecutor(max_workers=20, thread_name_prefix="bounded-call")
_REQUEST_HARD_TIMEOUT_S = 100  # keep comfortably under gunicorn's --timeout 120


def _run_bounded(fn, *args, timeout=_REQUEST_HARD_TIMEOUT_S, **kwargs):
    """Run fn(*args, **kwargs) with a hard wall-clock timeout.

    Returns (True, result) on success, or (False, None) if it didn't finish
    in time. Never raises for a timeout — callers should treat (False, None)
    as "still working, try again" rather than a hard failure.
    """
    future = _BOUNDED_CALL_POOL.submit(fn, *args, **kwargs)
    try:
        return True, future.result(timeout=timeout)
    except FutureTimeoutError:
        return False, None


def count_emails_in_folders(email_addr, app_password, subject_filter=None, from_filter=None, received_after=None):
    """
    Count matching emails in INBOX and SPAM folders using REAL arrival time
    """
    result = {
        'inbox_count': 0,
        'spam_count': 0,
        'total_count': 0,
        'success': False,
        'error': None
    }
    
    # Parse received_after datetime if provided
    received_after_dt = None
    if received_after:
        try:
            # Handle ISO format with 'T' separator (from datetime-local input)
            if 'T' in received_after:
                received_after = received_after.replace('T', ' ')
            # Try different formats
            for fmt in ['%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                try:
                    received_after_dt = datetime.strptime(received_after, fmt)
                    # Make it timezone-aware (UTC)
                    received_after_dt = received_after_dt.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if not received_after_dt:
                logging.warning(f"Could not parse received_after date: {received_after}")
        except Exception as e:
            logging.warning(f"Error parsing received_after date: {e}")
    
    try:
        # Connect to Gmail
        mail = connect_to_gmail(email_addr, app_password)
        if not mail:
            result['error'] = 'Authentication failed or connection error'
            return result
        
        # Helper function to count matching emails in a folder
        def count_matching_emails(folder_name):
            try:
                mail.select(folder_name, readonly=True)
                result_code, message_ids = mail.search(None, 'ALL')
                
                if result_code != 'OK' or not message_ids[0]:
                    return 0
                
                uid_list = message_ids[0].split()
                if not uid_list:
                    return 0
                
                count = 0
                # Check last 300 emails for performance (adjust as needed)
                uid_list = uid_list[-300:] if len(uid_list) > 300 else uid_list
                
                for uid in uid_list:
                    try:
                        fetch_result, msg_data = mail.fetch(uid, '(BODY.PEEK[HEADER])')
                        
                        if fetch_result != 'OK' or not msg_data or not msg_data[0]:
                            continue
                        
                        # Parse header data
                        if isinstance(msg_data[0], tuple) and len(msg_data[0]) >= 2:
                            header_bytes = msg_data[0][1]
                            
                            if isinstance(header_bytes, bytes):
                                email_message = email.message_from_bytes(header_bytes)
                            else:
                                continue
                            
                            # --- MODIFIED: Use REAL arrival time from Received headers ---
                            if received_after_dt:
                                real_arrival = get_real_arrival_time(email_message)
                                
                                if real_arrival:
                                    # Ensure timezone-aware
                                    if real_arrival.tzinfo is None:
                                        real_arrival = real_arrival.replace(tzinfo=timezone.utc)
                                    
                                    # Skip if email arrived before received_after
                                    if real_arrival < received_after_dt:
                                        continue
                                else:
                                    # Fallback to Date header if Received parsing fails
                                    date_header = email_message.get('Date', '')
                                    if date_header:
                                        try:
                                            email_date = email.utils.parsedate_to_datetime(date_header)
                                            if email_date.tzinfo is None:
                                                email_date = email_date.replace(tzinfo=timezone.utc)
                                            if email_date < received_after_dt:
                                                continue
                                        except (TypeError, ValueError):
                                            pass
                            
                            # Apply subject filter if provided
                            if subject_filter:
                                email_subject = decode_mime_words(email_message.get('Subject', ''))
                                if subject_filter.lower() not in email_subject.lower():
                                    continue
                            
                            # Apply from filter if provided
                            if from_filter:
                                email_from = email_message.get('From', '')
                                if from_filter.lower() not in email_from.lower():
                                    continue
                            
                            # Email matches all filters
                            count += 1
                            
                    except Exception as e:
                        logging.debug(f"Error processing email: {e}")
                        continue
                
                return count
                
            except Exception as e:
                logging.error(f"Error counting emails in {folder_name}: {e}")
                return 0
        
        # Count emails in INBOX
        result['inbox_count'] = count_matching_emails('INBOX')
        
        # Count emails in SPAM
        result['spam_count'] = count_matching_emails('[Gmail]/Spam')
        
        # Calculate total
        result['total_count'] = result['inbox_count'] + result['spam_count']
        result['success'] = True
        
        # Logout
        try:
            mail.logout()
        except:
            pass
        
        return result
        
    except Exception as e:
        logging.error(f"Error in count_emails_in_folders: {e}")
        result['error'] = str(e)
        return result

@app.route('/count_emails', methods=['POST'])
def count_emails_endpoint():
    """
    API endpoint to count emails in INBOX and SPAM
    
    Expected JSON payload:
    {
        "email": "user@gmail.com",
        "app_password": "xxxx xxxx xxxx xxxx",
        "subject": "optional subject filter",
        "from": "optional from filter",
        "received_after": "optional datetime (e.g., 2024-01-15 10:30 or 2024-01-15T10:30)"
    }
    
    Returns:
    {
        "success": true,
        "inbox_count": 5,
        "spam_count": 2,
        "total_count": 7,
        "error": null
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'inbox_count': 0,
                'spam_count': 0,
                'total_count': 0,
                'error': 'No data provided'
            }), 200
        
        email_addr = data.get('email', '').strip()
        app_password = data.get('app_password', '').strip()
        subject_filter = data.get('subject', '').strip()
        from_filter = data.get('from', '').strip()
        received_after = data.get('received_after', '').strip()  # NEW
        
        # Validation
        if not email_addr or not app_password:
            return jsonify({
                'success': False,
                'inbox_count': 0,
                'spam_count': 0,
                'total_count': 0,
                'error': 'Email and app password are required'
            }), 200
        
        # Basic email format validation
        if '@' not in email_addr or '.' not in email_addr:
            return jsonify({
                'success': False,
                'inbox_count': 0,
                'spam_count': 0,
                'total_count': 0,
                'error': 'Invalid email format'
            }), 200
        
        # Call the counting function with received_after parameter, bounded so a
        # slow/stuck Gmail account can't hold this worker thread hostage.
        ok, result = _run_bounded(
            count_emails_in_folders,
            email_addr, app_password, subject_filter, from_filter,
            received_after if received_after else None,
        )
        if not ok:
            response = jsonify({
                'success': False,
                'inbox_count': 0,
                'spam_count': 0,
                'total_count': 0,
                'error': 'Timed out waiting for Gmail. Please try again.'
            })
            response.headers.add("Access-Control-Allow-Origin", "*")
            return response, 200
        
        # Add CORS headers for extension compatibility
        response = jsonify(result)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type, Accept")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        
        return response, 200
        
    except Exception as e:
        logging.error(f"Error in count_emails_endpoint: {e}")
        response = jsonify({
            'success': False,
            'inbox_count': 0,
            'spam_count': 0,
            'total_count': 0,
            'error': f'Server error - {str(e)}'
        })
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 200

# Also handle OPTIONS preflight for CORS
@app.route('/count_emails', methods=['OPTIONS'])
def count_emails_options():
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type, Accept")
    response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
    return response

@login_manager.user_loader
def load_user(user_id):
    """Load user from session"""
    users = load_users_from_file()
    for user_data in users:
        if user_data['username'] == user_id:
            user = User(
                user_data['username'], 
                user_data['entity'], 
                user_data['name'], 
                user_data['has_toggle_permission'], 
                user_data['has_news_permission'], 
                user_data['has_domain_checker_permission'], 
                user_data['has_find_news_permission'], 
                user_data['has_extract_emails_permission'], 
                user_data['has_tssw_report_permission'], 
                user_data['has_gmass_permission'], 
                user_data['has_blacklist_lookup_permission']
            )
            permissions = user_data.get('permissions', [])
            user.has_download_extension_permission = 'download_extension' in permissions
            user.has_add_extensions_permission = 'add_extensions' in permissions
            user.has_quality_helper_permission = 'quality_helper' in permissions
            user.has_news_subscription_permission = 'news_sign_subsctiption' in permissions
            user.has_user_management_permission = 'user_management' in permissions
            user.has_ips_cheker_permission = 'ips_cheker' in permissions
            user.has_add_ip_cheker_permission = 'add_ip_cheker' in permissions
            user.has_domain_founder_permission = 'domain_founder' in permissions
            user.has_unlimited_domain_founder_permission = 'unlimited_domain_founder' in permissions
            user.has_email_founder_permission = 'email_founder' in permissions
            user.has_access_management_permission = 'access_management' in permissions
            user.has_subdomain_finder_permission = 'subdomain_finder' in permissions
            user.has_processes_management_permission = 'processes_management' in permissions
            user.has_warmup_lists_permission = 'warmup_lists' in permissions
            user.has_warmup_lists_admin_permission = 'warmup_lists_admin' in permissions
            user.has_warmup_history_permission = 'Warmup_History' in permissions
            user.has_add_warmup_record_permission = 'add_warmup_record' in permissions
            user.has_warmup_reports_permission = 'warmup_reports' in permissions
            user.has_warmup_sessions_permission = 'warmup_sessions' in permissions
            user.has_display_extensions_users_permission = 'display_extensions_users' in permissions
            user.max_processes = user_data.get('max_processes', 1)
            user.email_founder_max_processes = user_data.get('email_founder_max_processes', 10)
            user.domain_quota = user_data.get('domain_quota', 0)
            user.domains_processed_this_month = user_data.get('domains_processed_this_month', 0)
            user.sf_max_processes = user_data.get('sf_max_processes', 1)
            user.sf_max_domains = user_data.get('sf_max_domains', 0)
            user.sf_stop_at = user_data.get('sf_stop_at', 0)
            user.df_max_processes = user_data.get('df_max_processes', 1)
            return user
    return None

def load_users_from_file():
    """Load users from users.txt file with format: entity,Name,username,password[,permissions]"""
    users = []
    quotas = load_user_quotas()
    try:
        with open('users.txt', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    try:
                        parts = line.split(',')
                        if len(parts) >= 4:
                            entity = parts[0].strip()
                            name = parts[1].strip()
                            username = parts[2].strip()
                            password = parts[3].strip()
                            permissions = [p.strip() for p in parts[4:]] if len(parts) > 4 else []
                            has_toggle = 'ok' in permissions
                            has_news = 'allow_add_gmail_of_news' in permissions
                            has_domain_checker = 'Domain_checker' in permissions
                            has_find_news = 'find_news' in permissions
                            has_extract_emails = 'Extract_emails' in permissions
                            has_tssw_report = 'tssw_report' in permissions
                            has_gmass = 'gmass' in permissions
                            has_blacklist_lookup = 'blacklist_lookup' in permissions
                            user_quota = quotas.get(username, {})
                            users.append({
                                'entity': entity,
                                'name': name,
                                'username': username,
                                'password': password,
                                'permissions': permissions,
                                'max_processes': user_quota.get('max_processes', 1),
                                'email_founder_max_processes': user_quota.get('email_founder_max_processes', 10),
                                'domain_quota': user_quota.get('domain_quota', 0),
                                'domains_processed_this_month': user_quota.get('domains_processed_this_month', 0),
                                'sf_max_processes': user_quota.get('sf_max_processes', 1),
                                'sf_max_domains': user_quota.get('sf_max_domains', 0),
                                'df_max_processes': user_quota.get('df_max_processes', 1),
                                'has_toggle_permission': has_toggle,
                                'has_news_permission': has_news,
                                'has_domain_checker_permission': has_domain_checker,
                                'has_find_news_permission': has_find_news,
                                'has_extract_emails_permission': has_extract_emails,
                                'has_tssw_report_permission': has_tssw_report,
                                'has_gmass_permission': has_gmass,
                                'has_blacklist_lookup_permission': has_blacklist_lookup
                            })
                        else:
                            logging.warning(f"Invalid format in users.txt line {line_num}: {line}")
                    except Exception as e:
                        logging.error(f"Error parsing users.txt line {line_num}: {e}")
    except FileNotFoundError:
        logging.error("users.txt file not found")
    except Exception as e:
        logging.error(f"Error reading users.txt: {e}")
    
    return users

USER_QUOTAS_FILE = 'user_quotas.json'

def _check_email_core(email_addr, app_password, subject_filter, from_filter):
    """Connect to Gmail and look for a matching email in INBOX then Spam.

    Returns a plain location string: 'inbox', 'spam', 'not_found', or
    'error: ...'. Runs on a worker thread via _run_bounded() so a stuck
    IMAP connection can't hold up the calling request indefinitely.
    """
    mail = connect_to_gmail(email_addr, app_password)
    if not mail:
        return 'error: Authentication failed or connection error'

    def find_matching_email(folder_name):
        try:
            mail.select(folder_name, readonly=True)
            result, message_ids = mail.search(None, 'ALL')
            if result != 'OK' or not message_ids[0]:
                return False

            uid_list = message_ids[0].split()
            # Limit search to last 100 emails for performance
            uid_list = uid_list[-100:] if len(uid_list) > 100 else uid_list

            for uid in uid_list:
                result, msg_data = mail.fetch(uid, '(BODY.PEEK[HEADER])')
                if result != 'OK' or not msg_data or not msg_data[0]:
                    continue

                # Parse header data
                if isinstance(msg_data[0], tuple) and len(msg_data[0]) >= 2:
                    header_bytes = msg_data[0][1]
                    if isinstance(header_bytes, bytes):
                        email_message = email.message_from_bytes(header_bytes)
                    else:
                        continue

                    # Apply subject filter if provided
                    if subject_filter:
                        email_subject = decode_mime_words(email_message.get('Subject', ''))
                        if subject_filter.lower() not in email_subject.lower():
                            continue

                    # Apply from filter if provided
                    if from_filter:
                        email_from = email_message.get('From', '')
                        if from_filter.lower() not in email_from.lower():
                            continue

                    # Found matching email!
                    return True
            return False
        except Exception:
            return False

    try:
        # Check INBOX first
        if find_matching_email('INBOX'):
            mail.logout()
            return 'inbox'

        # Check Spam folder
        if find_matching_email('[Gmail]/Spam'):
            mail.logout()
            return 'spam'

        # Email not found in either folder
        mail.logout()
        return 'not_found'

    except imaplib.IMAP4.error as e:
        try:
            mail.logout()
        except:
            pass
        error_msg = str(e).lower()
        if 'authentication' in error_msg or 'login' in error_msg:
            return 'error: Authentication failed. Use a valid Gmail App Password.'
        return f'error: IMAP error - {str(e)}'

    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        return f'error: {str(e)}'


@app.route('/check_email', methods=['POST', 'OPTIONS'])
def check_email_endpoint():
    # Handle OPTIONS preflight request
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type, Accept")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        return response
    
    # Your existing check_email_endpoint code...
    try:
        data = request.get_json()
        if not data:
            response = jsonify({'location': 'error: No data provided'})
            response.headers.add("Access-Control-Allow-Origin", "*")
            return response, 200
        
        email_addr = data.get('email', '').strip()
        app_password = data.get('app_password', '').strip()
        subject_filter = data.get('subject', '').strip()
        from_filter = data.get('from', '').strip()
        
        # Validation
        if not email_addr or not app_password:
            response = jsonify({'location': 'error: Email and app password are required'})
            response.headers.add("Access-Control-Allow-Origin", "*")
            return response, 200
        
        # Basic email format validation
        if '@' not in email_addr or '.' not in email_addr:
            response = jsonify({'location': 'error: Invalid email format'})
            response.headers.add("Access-Control-Allow-Origin", "*")
            return response, 200
        
        # Bounded so a slow/stuck Gmail account can't hold this worker thread
        # (and therefore the gunicorn worker) hostage past the safe window.
        ok, location = _run_bounded(_check_email_core, email_addr, app_password, subject_filter, from_filter)
        if not ok:
            response = jsonify({'location': 'error: Timed out waiting for Gmail. Please try again.'})
            response.headers.add("Access-Control-Allow-Origin", "*")
            return response, 200

        response = jsonify({'location': location})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 200

    except Exception as e:
        logging.error(f"Error in check_email_endpoint: {e}")
        response = jsonify({'location': f'error: Server error - {str(e)}'})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 200

def load_user_quotas():
    """Load user quotas from JSON file, reset monthly counters if needed"""
    try:
        if os.path.exists(USER_QUOTAS_FILE):
            with open(USER_QUOTAS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                current_month = datetime.now().strftime('%Y-%m')
                changed = False
                for username, q in data.items():
                    if q.get('quota_month', '') != current_month:
                        q['domains_processed_this_month'] = 0
                        q['quota_month'] = current_month
                        changed = True
                if changed:
                    save_user_quotas(data)
                return data
    except Exception as e:
        logging.error(f"Error loading user quotas: {e}")
    return {}

def save_user_quotas(quotas):
    """Save user quotas to JSON file"""
    try:
        with open(USER_QUOTAS_FILE, 'w', encoding='utf-8') as f:
            json.dump(quotas, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving user quotas: {e}")

def update_user_domains_processed(username, count):
    """Increment the domains processed count for a user"""
    quotas = load_user_quotas()
    current_month = datetime.now().strftime('%Y-%m')
    if username not in quotas:
        quotas[username] = {'max_processes': 1, 'domain_quota': 0, 'domains_processed_this_month': 0, 'quota_month': current_month}
    if quotas[username].get('quota_month', '') != current_month:
        quotas[username]['domains_processed_this_month'] = 0
        quotas[username]['quota_month'] = current_month
    quotas[username]['domains_processed_this_month'] += count
    save_user_quotas(quotas)
    return quotas[username]['domains_processed_this_month']

def get_user_remaining_domains(username):
    """Get remaining domain quota for a user. Returns -1 if unlimited."""
    quotas = load_user_quotas()
    user_quota = quotas.get(username, {})
    domain_quota = user_quota.get('domain_quota', 0)
    if domain_quota == 0:
        return -1
    current_month = datetime.now().strftime('%Y-%m')
    if user_quota.get('quota_month', '') != current_month:
        return domain_quota
    processed = user_quota.get('domains_processed_this_month', 0)
    return max(0, domain_quota - processed)

def save_users_to_file(users_list):
    """Save users list back to users.txt"""
    try:
        with open('users.txt', 'w', encoding='utf-8') as f:
            f.write('# Format: entity,Name,username,password[,permissions]\n')
            f.write('# Permissions can be: ok, allow_add_gmail_of_news (or both separated by comma)\n')
            for u in users_list:
                parts = [u['entity'], u['name'], u['username'], u['password']]
                if u.get('permissions'):
                    parts.extend(u['permissions'])
                f.write(','.join(parts) + '\n')
    except Exception as e:
        logging.error(f"Error saving users.txt: {e}")

def get_user_accounts(user_entity):
    """Get Gmail accounts accessible to a user based on their entity"""
    return _get_user_gmail_accounts(user_entity)

def authenticate_user(username, password):
    """Authenticate user against users.txt file and return user data dict"""
    users = load_users_from_file()
    for user_data in users:
        if user_data['username'] == username and user_data['password'] == password:
            return user_data
    return None

def connect_to_gmail(email_addr, password):
    """Connect to Gmail using IMAP with enhanced error handling, validation and retries"""
    if not email_addr or not password:
        logging.error("Email address and password are required")
        return None
    
    # Basic email validation
    if '@' not in email_addr or '.' not in email_addr:
        logging.error(f"Invalid email address format: {email_addr}")
        return None

    # Keywords that indicate a real auth failure — no point retrying these
    AUTH_FAILURE_HINTS = (
        'authentication failed', 'invalid credentials',
        'application-specific password required',
        '[authenticationfailed]', 'username and password not accepted',
    )

    max_retries = 5
    base_delay = 3   # seconds; doubles each attempt (3 → 6 → 12 …)

    for attempt in range(max_retries):
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com', 993, timeout=60)
            mail.login(email_addr, password)
            logging.info(f"Successfully connected to Gmail account: {email_addr}")
            return mail

        except Exception as e:
            error_msg = str(e).lower()
            logging.error(f"Connection attempt {attempt + 1}/{max_retries} failed for {email_addr}: {e}")

            # Hard auth failures — retrying won't help
            if any(hint in error_msg for hint in AUTH_FAILURE_HINTS):
                logging.error(f"Permanent authentication failure for {email_addr}: {e}")
                return None

            # Transient errors (EOF, socket reset, timeout, etc.) — retry with backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logging.info(f"Transient connection error for {email_addr}, retrying in {delay}s... ({attempt + 2}/{max_retries})")
                time.sleep(delay)
            else:
                logging.error(f"All {max_retries} connection attempts failed for {email_addr}")

    return None

def decode_mime_words(s):
    """Decode MIME encoded words"""
    if s is None:
        return ''
    
    decoded_parts = []
    for part, encoding in decode_header(s):
        if isinstance(part, bytes):
            if encoding:
                try:
                    decoded_parts.append(part.decode(encoding))
                except:
                    decoded_parts.append(part.decode('utf-8', errors='ignore'))
            else:
                decoded_parts.append(part.decode('utf-8', errors='ignore'))
        else:
            decoded_parts.append(str(part))
    
    return ''.join(decoded_parts)

def _format_time_ago(dt):
    """Convert datetime to relative time string."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return f"{seconds} sec"
    elif seconds < 3600:
        return f"{seconds // 60} min"
    elif seconds < 86400:
        return f"{seconds // 3600} h"
    else:
        days = seconds // 86400
        return f"{days} day{'s' if days > 1 else ''}"


def _get_gmail_cache_path(account_key):
    """Return the JSON cache file path for a given account_key."""
    safe_key = re.sub(r'[^a-zA-Z0-9_\-@.]', '_', account_key)
    return os.path.join(GMAIL_CACHE_DIR, f"{safe_key}.json")


_EMPTY_GMAIL_CACHE = {'emails': [], 'fetched_at': None, 'status': 'idle'}
_GMAIL_CACHE_MEM = {}
_GMAIL_CACHE_MEM_LOCK = _threading.Lock()
_GMAIL_CACHE_MEM_MAX = 500


def _read_gmail_cache(account_key):
    """Return cached emails for an account.

    Uses a process-wide in-memory cache keyed by the JSON file's mtime so
    repeated reads (especially from SSE loops) do NOT re-open the file or
    re-parse JSON on every poll. The file is opened only when its mtime
    actually changes — which is what eliminates the "Too many open files"
    error and the memory growth caused by many concurrent SSE clients each
    holding their own freshly-parsed copy of the cache.
    """
    path = _get_gmail_cache_path(account_key)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _EMPTY_GMAIL_CACHE

    with _GMAIL_CACHE_MEM_LOCK:
        entry = _GMAIL_CACHE_MEM.get(account_key)
        if entry is not None and entry[0] == mtime:
            return entry[1]

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except OSError as e:
        logging.error(f"Error reading gmail cache for {account_key}: {e}")
        return _EMPTY_GMAIL_CACHE
    except Exception as e:
        logging.error(f"Error parsing gmail cache for {account_key}: {e}")
        return _EMPTY_GMAIL_CACHE

    with _GMAIL_CACHE_MEM_LOCK:
        if len(_GMAIL_CACHE_MEM) >= _GMAIL_CACHE_MEM_MAX:
            _GMAIL_CACHE_MEM.pop(next(iter(_GMAIL_CACHE_MEM)), None)
        _GMAIL_CACHE_MEM[account_key] = (mtime, data)
    return data




def extract_and_analyze_emails(email_address, app_password, email_limit='all', folder_selection='all'):
    """Extract and analyze emails with SPF, DKIM, IP address, and categorization - Optimized for speed"""
    try:
        # Connect to Gmail
        mail = connect_to_gmail(email_address, app_password)
        if not mail:
            return None
        
        extracted_emails = []
        
        # Get folders to check based on user selection
        if folder_selection == 'inbox':
            folders_to_check = ['INBOX']
        elif folder_selection == 'spam':
            folders_to_check = ['[Gmail]/Spam']
        else:  # folder_selection == 'all'
            folders_to_check = ['INBOX', '[Gmail]/Spam']
        
        for folder in folders_to_check:
            try:
                mail.select(folder, readonly=True)  # Keep emails unread
                
                # Search for emails
                result, message_ids = mail.uid('search', 'ALL')
                if result != 'OK':
                    continue
                
                uid_list = message_ids[0].split()
                if not uid_list:
                    continue

                # Reverse so newest UIDs come first
                uid_list = list(reversed(uid_list))

                # Apply email limit based on user selection
                if email_limit != 'all':
                    try:
                        limit = int(email_limit)
                        uid_list = uid_list[:limit]
                    except (ValueError, TypeError):
                        uid_list = uid_list[:50]
                
                # Pre-cache Gmail categories for inbox emails (only if needed)
                category_cache = {}
                if folder == 'INBOX':
                    category_cache = _build_category_cache_fast(mail, uid_list)
                
                # BATCH OPTIMIZATION: Fetch emails in batches instead of one by one
                # This reduces network round trips dramatically (500 requests → ~10 requests)
                batch_size = 50  # Process 50 emails at once
                total_emails = len(uid_list)
                
                for batch_start in range(0, total_emails, batch_size):
                    # Check if the connection is still alive before each batch
                    try:
                        mail.noop()
                    except:
                        logging.error("IMAP connection lost during extraction, attempting to reconnect...")
                        mail = connect_to_gmail(email_address, app_password)
                        if not mail:
                            break
                        mail.select(folder, readonly=True)

                    batch_end = min(batch_start + batch_size, total_emails)
                    batch_uids = uid_list[batch_start:batch_end]
                    
                    # Create UID range string for batch fetch
                    # Decode bytes to string for IMAP command
                    decoded_uids = [uid.decode() if isinstance(uid, bytes) else uid for uid in batch_uids]
                    uid_range = ','.join(decoded_uids)
                    
                    try:
                        # Fetch entire batch in ONE network request
                        result, msg_data = mail.uid('fetch', uid_range, '(BODY.PEEK[HEADER])')
                        if result != 'OK' or not msg_data:
                            continue
                        
                        # Process all emails in this batch
                        # msg_data is a list where each email is represented by a tuple (metadata, header_bytes)
                        # with occasional trailing non-tuple items we can ignore
                        for item in msg_data:
                            # Skip non-tuple items (like closing parenthesis bytes)
                            if not isinstance(item, tuple) or len(item) < 2:
                                continue
                                
                            try:
                                # Each tuple is (metadata_bytes, header_bytes)
                                metadata = item[0]
                                header_bytes = item[1]
                                
                                # Parse the UID from metadata
                                # metadata looks like: b'123 (UID 456 BODY[HEADER] {1234}'
                                uid_match = re.search(rb'UID (\d+)', metadata) if isinstance(metadata, bytes) else None
                                current_uid_bytes = uid_match.group(1) if uid_match else None
                                
                                # Skip if we can't parse UID or no header data
                                if not current_uid_bytes or not header_bytes:
                                    continue
                                
                                # Parse email headers
                                email_message = email.message_from_bytes(header_bytes)
                                
                                # Extract basic info
                                subject = decode_mime_words(email_message.get('Subject', ''))
                                from_header = email_message.get('From', '')
                                date_header = email_message.get('Date', '')
                                
                                # Parse from header
                                from_name, from_email = email.utils.parseaddr(from_header)
                                from_email = from_email.lower()
                                from_domain_extracted = from_email.split('@')[-1] if '@' in from_email else ''

                                # Parse Return-Path header -> extract only the domain part
                                return_path_header = email_message.get('Return-Path', '')
                                _rp_name, return_path_full = email.utils.parseaddr(return_path_header)
                                return_path_full = (return_path_full or '').lower()
                                return_path_email = return_path_full.split('@')[-1] if '@' in return_path_full else return_path_full

                                # Extract security info from headers efficiently
                                ip_address = extract_sender_ip_fast(email_message)
                                spf_status = extract_spf_status(email_message)
                                dkim_status = extract_dkim_status(email_message)
                                dmarc_status = extract_dmarc_status(email_message)
                                
                                # Optimization: Small sleep to prevent CPU spiking during heavy concurrent usage
                                # and allow other threads to process
                                time.sleep(0.02)

                                # Determine email type and category
                                email_type = 'Spam' if folder == '[Gmail]/Spam' else 'Inbox'
                                # Keep UID as bytes to match category_cache keys
                                category = category_cache.get(current_uid_bytes, '') if folder == 'INBOX' else ''
                                
                                # Format date
                                try:
                                    parsed_date = email.utils.parsedate_to_datetime(date_header)
                                    formatted_date = parsed_date.strftime('%Y-%m-%d %H:%M')
                                except:
                                    formatted_date = date_header[:50] if date_header else 'Unknown'
                                
                                extracted_emails.append({
                                    'ip_address': ip_address,
                                    'spf_status': spf_status,
                                    'dkim_status': dkim_status,
                                    'dmarc_status': dmarc_status,
                                    'from_domain': from_domain_extracted,
                                    'return_path': return_path_email,
                                    'subject': subject[:100],
                                    'email_type': email_type,
                                    'category': category,
                                    'date': formatted_date
                                })
                                
                            except Exception as e:
                                logging.error(f"Error processing email in batch: {e}")
                                continue
                                
                    except Exception as e:
                        logging.error(f"Error fetching batch: {e}")
                        continue
                        
            except Exception as e:
                logging.error(f"Error accessing folder {folder}: {e}")
                continue
        
        mail.logout()
        # Sort newest first — date field is '%Y-%m-%d %H:%M', lexicographic order works
        extracted_emails.sort(key=lambda e: e.get('date', ''), reverse=True)
        return extracted_emails
        
    except Exception as e:
        logging.error(f"Error in extract_and_analyze_emails: {e}")
        return None

def _build_category_cache_fast(mail, uid_list):
    """Build Gmail category cache using batch queries for speed"""
    category_cache = {}
    categories = ['social', 'promotions', 'updates', 'forums']
    
    for cat_key in categories:
        try:
            result, data = mail.uid('search', 'X-GM-RAW', f'"category:{cat_key}"')
            if result == 'OK' and data[0]:
                cat_uids = set(data[0].split())
                for uid in uid_list:
                    if uid in cat_uids:
                        category_cache[uid] = cat_key.capitalize()
        except Exception as e:
            logging.debug(f"Error caching category {cat_key}: {e}")
    
    return category_cache

def extract_sender_ip_fast(email_message):
    """Optimized IP extraction - faster version"""
    try:
        # Check Received headers (most common location)
        received_headers = email_message.get_all('Received', [])
        
        # Fast IP pattern matching
        ip_pattern = re.compile(r'\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]')
        
        for received in received_headers[:3]:  # Only check first 3 headers for speed
            matches = ip_pattern.findall(received)
            if matches:
                # Return the first external IP (not private)
                for ip in matches:
                    if not ip.startswith(('10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.', '172.20.', '172.21.', '172.22.', '172.23.', '172.24.', '172.25.', '172.26.', '172.27.', '172.28.', '172.29.', '172.30.', '172.31.')):
                        return ip
                # If no external IP, return first IP
                return matches[0] if matches else None
        
        return None
    except:
        return None

def extract_sender_ip(email_message):
    """Extract sender IP address from email headers"""
    try:
        # Check various IP-containing headers
        received_headers = email_message.get_all('Received', [])
        
        for received in received_headers:
            # Look for IP addresses in Received headers

            ip_pattern = r'\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]'
            matches = re.findall(ip_pattern, received)
            if matches:
                # Return the first external IP (not private)
                for ip in matches:
                    if not ip.startswith(('10.', '192.168.', '172.')):
                        return ip
                # If no external IP, return first IP
                return matches[0] if matches else None
        
        return None
    except:
        return None

def extract_spf_status(email_message):
    """Extract SPF status from Authentication-Results header"""
    try:
        auth_results = email_message.get('Authentication-Results', '')
        if 'spf=pass' in auth_results.lower():
            return 'PASS'
        elif 'spf=fail' in auth_results.lower():
            return 'FAIL'
        elif 'spf=softfail' in auth_results.lower():
            return 'SOFTFAIL'
        elif 'spf=neutral' in auth_results.lower():
            return 'NEUTRAL'
        elif 'spf=none' in auth_results.lower():
            return 'NONE'
        return 'UNKNOWN'
    except:
        return 'UNKNOWN'

def extract_dkim_status(email_message):
    """Extract DKIM status from Authentication-Results header"""
    try:
        auth_results = email_message.get('Authentication-Results', '')
        if 'dkim=pass' in auth_results.lower():
            return 'PASS'
        elif 'dkim=fail' in auth_results.lower():
            return 'FAIL'
        elif 'dkim=neutral' in auth_results.lower():
            return 'NEUTRAL'
        elif 'dkim=none' in auth_results.lower():
            return 'NONE'
        return 'UNKNOWN'
    except:
        return 'UNKNOWN'

def extract_dmarc_status(email_message):
    """Extract DMARC status from Authentication-Results header"""
    try:
        auth_results = email_message.get('Authentication-Results', '')
        if 'dmarc=pass' in auth_results.lower():
            return 'PASS'
        elif 'dmarc=fail' in auth_results.lower():
            return 'FAIL'
        elif 'dmarc=none' in auth_results.lower():
            return 'NONE'
        elif 'dmarc=quarantine' in auth_results.lower():
            return 'QUARANTINE'
        elif 'dmarc=reject' in auth_results.lower():
            return 'REJECT'
        return 'UNKNOWN'
    except:
        return 'UNKNOWN'

def get_gmail_category(mail, uid):
    """Get Gmail category for an email"""
    try:
        result, msg_data = mail.uid('fetch', uid, '(X-GM-LABELS)')
        if result == 'OK' and msg_data and msg_data[0]:
            labels_info = msg_data[0][1].decode('utf-8', errors='ignore') if isinstance(msg_data[0][1], bytes) else str(msg_data[0][1])
            
            if '\\\\Category\\\\Promotions' in labels_info or 'Category/Promotions' in labels_info:
                return 'Promotions'
            elif '\\\\Category\\\\Social' in labels_info or 'Category/Social' in labels_info:
                return 'Social'
            elif '\\\\Category\\\\Updates' in labels_info or 'Category/Updates' in labels_info:
                return 'Updates'
            elif '\\\\Category\\\\Forums' in labels_info or 'Category/Forums' in labels_info:
                return 'Forums'
            else:
                return 'Primary'
        return 'Primary'
    except:
        return 'Primary'

def get_improved_gmail_category(mail, uid):
    """Get Gmail category with improved detection using multiple methods"""
    try:
        # Method 1: Try X-GM-LABELS first (most reliable)
        result, msg_data = mail.uid('fetch', uid, '(X-GM-LABELS)')
        if result == 'OK' and msg_data and msg_data[0]:
            labels_info = msg_data[0][1].decode('utf-8', errors='ignore') if isinstance(msg_data[0][1], bytes) else str(msg_data[0][1])
            
            # Check for various label formats
            labels_lower = labels_info.lower()
            if any(keyword in labels_lower for keyword in ['category\\\\promotions', 'category/promotions', '"\\\\category\\\\promotions"']):
                return 'Promotions'
            elif any(keyword in labels_lower for keyword in ['category\\\\social', 'category/social', '"\\\\category\\\\social"']):
                return 'Social'
            elif any(keyword in labels_lower for keyword in ['category\\\\updates', 'category/updates', '"\\\\category\\\\updates"']):
                return 'Updates'
            elif any(keyword in labels_lower for keyword in ['category\\\\forums', 'category/forums', '"\\\\category\\\\forums"']):
                return 'Forums'
        
        # Method 2: Try Gmail search queries for categories
        try:
            # Check if email is in Promotions category using search
            status, data = mail.uid('search', 'X-GM-RAW', f'"category:promotions"')
            if status == 'OK' and data[0] and uid in data[0].split():
                return 'Promotions'
            
            # Check Social category
            status, data = mail.uid('search', 'X-GM-RAW', f'"category:social"')
            if status == 'OK' and data[0] and uid in data[0].split():
                return 'Social'
            
            # Check Updates category
            status, data = mail.uid('search', 'X-GM-RAW', f'"category:updates"')
            if status == 'OK' and data[0] and uid in data[0].split():
                return 'Updates'
            
            # Check Forums category
            status, data = mail.uid('search', 'X-GM-RAW', f'"category:forums"')
            if status == 'OK' and data[0] and uid in data[0].split():
                return 'Forums'
            
        except Exception as e:
            logging.debug(f"Gmail search method failed for UID {uid}: {e}")
        
        # Method 3: Fall back to header analysis for common patterns
        try:
            result, msg_data = mail.uid('fetch', uid, '(BODY.PEEK[HEADER])')
            if result == 'OK' and msg_data and msg_data[0]:
                header_content = msg_data[0][1].decode('utf-8', errors='ignore').lower()
                
                # Look for promotional indicators
                if any(keyword in header_content for keyword in ['unsubscribe', 'promotional', 'marketing', 'offer', 'deal']):
                    return 'Promotions'
                
                # Look for social indicators
                social_domains = ['facebook', 'twitter', 'linkedin', 'instagram', 'youtube', 'github']
                if any(domain in header_content for domain in social_domains):
                    return 'Social'
                
                # Look for update indicators
                if any(keyword in header_content for keyword in ['newsletter', 'update', 'notification', 'alert']):
                    return 'Updates'
                
        except Exception as e:
            logging.debug(f"Header analysis failed for UID {uid}: {e}")
        
        return 'Primary'
        
    except Exception as e:
        logging.debug(f"Improved category detection failed for UID {uid}: {e}")
        return 'Primary'

def get_gmail_folder_type(mail, uid):
    """Determine Gmail folder type based only on authentic Gmail X-GM-LABELS"""
    try:
        # Only use Gmail's authentic X-GM-LABELS - no content analysis fallback
        result, msg_data = mail.uid('fetch', uid, '(X-GM-LABELS)')
        if result == 'OK' and msg_data and msg_data[0]:
            try:
                labels_info = msg_data[0][1].decode('utf-8', errors='ignore') if isinstance(msg_data[0][1], bytes) else str(msg_data[0][1])
                logging.debug(f"Gmail labels for UID {uid}: {labels_info}")
                
                # Check for Gmail category labels - use exact Gmail format
                if '\\\\Category\\\\Promotions' in labels_info or 'Category/Promotions' in labels_info:
                    return 'Inbox/Promotions'
                elif '\\\\Category\\\\Social' in labels_info or 'Category/Social' in labels_info:
                    return 'Inbox/Social'
                elif '\\\\Category\\\\Updates' in labels_info or 'Category/Updates' in labels_info:
                    return 'Inbox/Updates'
                elif '\\\\Category\\\\Forums' in labels_info or 'Category/Forums' in labels_info:
                    return 'Inbox/Forums'
                    
            except Exception as e:
                logging.debug(f"Error parsing labels for UID {uid}: {e}")
            
    except Exception as e:
        logging.debug(f"Error fetching labels for UID {uid}: {e}")
    
    # Default to Primary if no Gmail category labels found
    return 'Inbox/Primary'





def format_time_ago(dt):
    """Convert datetime to 'X sec/min/hour/day' (without 'ago')"""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    diff = now - dt
    seconds = int(diff.total_seconds())
    
    if seconds < 60:
        return f"{seconds} sec"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} h"
    else:
        days = seconds // 86400
        return f"{days} day{'s' if days > 1 else ''}"









def get_emails_from_folder(mail, folder, folder_name, limit=20):
    """Get emails with accurate Gmail category detection — only for Inbox"""
    emails = []
    
    try:
        # Select the folder
        result = mail.select(folder)
        if result[0] != 'OK':
            return emails
        
        # Search for all UIDs
        result, data = mail.uid('SEARCH', None, 'ALL')
        if result != 'OK' or not data[0]:
            return emails
        
        email_uids = data[0].split()
        if not email_uids:
            return emails
        
        # Take only the most recent ones
        recent_uids = email_uids[-limit:]
        recent_uids.reverse()

        # === ONLY DETECT CATEGORIES IF THIS IS THE INBOX FOLDER ===
        use_category_detection = folder_name.lower() == 'inbox'

        # Cache for category UIDs (only if needed)
        cat_uid_sets = {}
        if use_category_detection:
            categories = {
                'social': 'Inbox/Social',
                'promotions': 'Inbox/Promotions',
                'updates': 'Inbox/Updates',
                'forums': 'Inbox/Forums',
                'purchases': 'Inbox/Purchases',
                'reservations': 'Inbox/Reservations'
            }

            for cat_key in categories:
                status, data = mail.uid('SEARCH', 'X-GM-RAW', f'"category:{cat_key}"')
                cat_uid_sets[cat_key] = set(data[0].split()) if status == 'OK' and data[0] else set()
        else:
            # For non-Inbox folders, we don't need categories
            pass

        # === FETCH EMAILS ===
        for uid in recent_uids:
            try:
                # Fetch only headers
                result, msg_data = mail.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
                if result != 'OK' or not msg_data[0]:
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                from_header = msg.get('From', '')
                from_name, from_email = email.utils.parseaddr(from_header)
                from_name = decode_mime_words(from_name) if from_name else from_email

                subject = decode_mime_words(msg.get('Subject', 'No Subject'))

                date_header = msg.get('Date', '')
                try:
                    date_obj = email.utils.parsedate_to_datetime(date_header)
                    date_timestamp = date_obj.timestamp()
                    date_formatted = format_time_ago(date_obj)
                except:
                    date_timestamp = datetime.now().timestamp()
                    date_formatted = 'Unknown'

                # === DETERMINE FOLDER TYPE ===
                if use_category_detection:
                    # Only Inbox uses category tabs
                    detected_folder = 'Inbox/Primary'
                    for cat_key, folder_type_name in {
                        'social': 'Inbox/Social',
                        'promotions': 'Inbox/Promotions',
                        'updates': 'Inbox/Updates',
                        'forums': 'Inbox/Forums',
                        'purchases': 'Inbox/Purchases',
                        'reservations': 'Inbox/Reservations'
                    }.items():
                        if uid in cat_uid_sets[cat_key]:
                            detected_folder = folder_type_name
                            break
                else:
                    # Any other folder (Spam, Sent, etc.) → use folder name directly
                    detected_folder = folder_name  # e.g., "Spam", "Sent", etc.

                emails.append({
                    'folder': detected_folder,
                    'from_name': from_name,
                    'from_email': from_email,
                    'subject': subject,
                    'title': subject,
                    'date': date_timestamp,
                    'date_formatted': date_formatted
                })

            except Exception as e:
                logging.error(f"Error processing email UID {uid}: {e}")
                continue

    except Exception as e:
        logging.error(f"Error accessing folder {folder}: {e}")
    
    return emails

# Authentication Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('gmail_access'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            flash('Please enter both username and password.', 'error')
            return render_template('login.html')
        
        # Authenticate user
        user_data = authenticate_user(username, password)
        if user_data:
            # IP access check
            am = load_access_management()
            allowed_ips = am.get('allowed_ips', [])
            exempt_users = am.get('exempt_users', [])
            if allowed_ips and username not in exempt_users:
                client_ip = request.form.get('client_public_ip', '').strip() or get_client_ip()
                logging.info(f"IP check for '{username}': detected={client_ip!r}, allowed={allowed_ips}")
                if client_ip not in allowed_ips:
                    flash(f'Access denied: your IP address ({client_ip}) is not authorized to log in.', 'error')
                    return render_template('login.html')

            user_entity = user_data['entity']
            user_name = user_data['name']
            has_toggle = user_data['has_toggle_permission']
            has_news = user_data['has_news_permission']
            has_domain_checker = user_data['has_domain_checker_permission']
            has_find_news = user_data['has_find_news_permission']
            has_extract_emails = user_data['has_extract_emails_permission']
            
            user = User(username, user_entity, user_name, has_toggle, has_news, has_domain_checker, has_find_news, has_extract_emails)
            login_user(user, remember=True)
            session['login_time'] = int(time.time())
            
            flash(f'Welcome, {user_name}!', 'success')
            
            # Redirect to next page if requested, otherwise services
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('services'))
        else:
            flash('Invalid username or password. Please try again.', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    username = current_user.username
    logout_user()
    flash(f'You have been logged out successfully, {username}.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return redirect(url_for('services'))

@app.route('/services')
@login_required
def services():
    """Main services selection page"""
    return render_template('services.html', current_user=current_user)

@app.route('/extract_emails', methods=['GET', 'POST'])
@login_required
def extract_emails():
    """TSS Extract Emails service"""
    if not current_user.has_extract_emails_permission:
        flash('You do not have permission to access the Extract Emails service.', 'error')
        return redirect(url_for('services'))
    
    if request.method == 'GET':
        return render_template('extract_emails.html', current_user=current_user)

    # Handle POST — spawn background thread, return task_id immediately
    try:
        email_address    = request.form.get('email_address', '').strip()
        app_password     = request.form.get('app_password', '').strip()

        if not email_address or not app_password:
            return jsonify({'success': False, 'error': 'Email address and app password are required'})

        email_limit = request.form.get('email_limit', 'all').strip()
        if email_limit == 'limited':
            email_limit = request.form.get('custom_limit', '50').strip()

        folder_selection = request.form.get('folder_selection', 'all')

        task_id = f"{current_user.username}_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}"
        with _EXTRACT_TASKS_LOCK:
            _EXTRACT_TASKS[task_id] = {'status': 'running'}

        t = _threading.Thread(
            target=_extract_task_worker,
            args=(task_id, email_address, app_password, email_limit, folder_selection),
            daemon=True,
        )
        t.start()

        return jsonify({'success': True, 'task_id': task_id})

    except Exception as e:
        logging.error(f"Error starting extract_emails task: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/extract_emails/status/<task_id>', methods=['GET'])
@login_required
def extract_emails_status(task_id):
    if not current_user.has_extract_emails_permission:
        return jsonify({'error': 'Permission denied'}), 403
    if not task_id.startswith(current_user.username + '_'):
        return jsonify({'error': 'Not found'}), 404
    with _EXTRACT_TASKS_LOCK:
        task = _EXTRACT_TASKS.get(task_id)
    if not task:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(task)


@app.route('/gmail_access', methods=['GET'])
@login_required
def gmail_access():
    search_sender = request.args.get('search_sender', '').strip()[:100]
    search_subject = request.args.get('search_subject', '').strip()[:200]
    # Admins see all accounts; TSSW and other entities see their own
    if current_user.has_user_management_permission:
        all_accs, _ = _load_gmail_accounts_from_file()
        user_accounts = all_accs
    else:
        user_accounts = get_user_accounts(current_user.entity)
    activated_keys = list(load_activated_keys())
    return render_template('dashboard.html',
                           accounts=user_accounts,
                           activated_keys=activated_keys,
                           is_admin=current_user.has_user_management_permission,
                           search_sender=search_sender,
                           search_subject=search_subject,
                           email_limit=50,
                           current_user=current_user)


@app.route('/dashboard', methods=['GET'])
@login_required
def dashboard():
    return redirect(url_for('gmail_access'))


@app.route('/fetch_emails', methods=['POST'])
@login_required
def fetch_emails():
    """Return the latest emails for the selected Gmail account instantly.

    Reads ONLY from the JSON cache populated by the standalone
    gmail_fetcher.py process. The web app never opens an IMAP connection
    for this endpoint, which is what previously caused the OOM kills when
    multiple users clicked accounts at the same time.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided', 'emails': []})
        # Admins can fetch any account; others are restricted to their entity
        if current_user.has_user_management_permission:
            all_accs, _ = _load_gmail_accounts_from_file()
            user_accounts = all_accs
        else:
            user_accounts = get_user_accounts(current_user.entity)
        selected_account = str(data.get('account', '')).strip()
        if not selected_account or selected_account not in user_accounts:
            return jsonify({'error': 'Invalid account selected', 'emails': []})
        if selected_account not in load_activated_keys():
            return jsonify({'error': 'not_activated', 'emails': []})

        cache = _read_gmail_cache(selected_account)
        emails = sorted(
            cache.get('emails', []) or [],
            key=lambda e: e.get('date_timestamp', 0),
            reverse=True,
        )

        return jsonify({
            'error': '',
            'emails': emails,
            'email_count': len(emails),
            'fetched_at': cache.get('fetched_at'),
            'cache_status': cache.get('status', 'idle'),
        })
    except Exception as e:
        logging.error(f"Error in fetch_emails endpoint: {e}")
        return jsonify({'error': f'Server error: {str(e)}', 'emails': []})


_SSE_STREAMS = {}
_SSE_STREAMS_LOCK = _threading.Lock()

# ── Extract-Emails async task store ──────────────────────
_EXTRACT_TASKS = {}
_EXTRACT_TASKS_LOCK = _threading.Lock()

def _extract_task_worker(task_id, email_address, app_password, email_limit, folder_selection):
    try:
        result = extract_and_analyze_emails(email_address, app_password, email_limit, folder_selection)
        with _EXTRACT_TASKS_LOCK:
            if result is None:
                _EXTRACT_TASKS[task_id] = {'status': 'error', 'error': 'Failed to connect to Gmail. Check your credentials.'}
            else:
                _EXTRACT_TASKS[task_id] = {'status': 'done', 'data': result}
    except Exception as e:
        with _EXTRACT_TASKS_LOCK:
            _EXTRACT_TASKS[task_id] = {'status': 'error', 'error': str(e)}

_SSE_MAX_PER_USER = 2
_SSE_MAX_LIFETIME_S = 240
_SSE_POLL_INTERVAL_S = 3


@app.route('/events/<account_key>')
@login_required
def events(account_key):
    """SSE endpoint — pushes the cached emails when the cache file changes.

    Improvements that prevent the previous "Too many open files" + OOM:
      * Cap of _SSE_MAX_PER_USER concurrent streams per user — older streams
        for the same user are signalled to close so file descriptors and
        worker slots are recycled instead of accumulating for 10 minutes.
      * The polling loop only does a cheap os.stat() — it never opens the
        JSON file or parses it unless mtime actually changed (the JSON is
        served from the shared in-memory cache).
      * Hard lifetime cap of _SSE_MAX_LIFETIME_S so even sticky clients
        release the gunicorn worker after a few minutes.
    """
    user_accounts = get_user_accounts(current_user.entity)
    if account_key not in user_accounts:
        return Response("Unauthorized", status=403)

    cache_path = _get_gmail_cache_path(account_key)
    user_id = getattr(current_user, 'username', 'anonymous')
    stop_event = _threading.Event()
    stream_id = f"{user_id}:{account_key}:{time.time()}"

    with _SSE_STREAMS_LOCK:
        user_streams = _SSE_STREAMS.setdefault(user_id, [])
        # Drop dead entries first
        user_streams[:] = [s for s in user_streams if not s['stop'].is_set()]
        # Close oldest if over the cap
        while len(user_streams) >= _SSE_MAX_PER_USER:
            old = user_streams.pop(0)
            old['stop'].set()
        user_streams.append({'id': stream_id, 'stop': stop_event})

    def event_stream():
        started = time.time()
        last_mtime = -1.0
        try:
            while not stop_event.is_set():
                if time.time() - started > _SSE_MAX_LIFETIME_S:
                    break
                try:
                    mtime = os.path.getmtime(cache_path)
                except OSError:
                    mtime = 0.0

                if mtime != last_mtime:
                    last_mtime = mtime
                    cache = _read_gmail_cache(account_key)
                    payload = {
                        'emails': cache.get('emails', []),
                        'fetched_at': cache.get('fetched_at'),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield "data: {\"heartbeat\":true}\n\n"

                # Sleep in small slices so stop_event reacts fast
                slept = 0.0
                while slept < _SSE_POLL_INTERVAL_S and not stop_event.is_set():
                    time.sleep(0.5)
                    slept += 0.5
        except GeneratorExit:
            pass
        except Exception as e:
            logging.error(f"Error in event stream for {account_key}: {e}")
        finally:
            stop_event.set()
            with _SSE_STREAMS_LOCK:
                streams = _SSE_STREAMS.get(user_id)
                if streams is not None:
                    streams[:] = [s for s in streams if s['id'] != stream_id]
                    if not streams:
                        _SSE_STREAMS.pop(user_id, None)

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


@app.route('/api/gmail/activate/<account_key>', methods=['POST'])
@login_required
def api_gmail_activate(account_key):
    """Activate a Gmail account (admin only)."""
    if not current_user.has_user_management_permission:
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    user_accounts = get_user_accounts(current_user.entity)
    # TSSW admin can activate any account
    all_accounts = _get_user_gmail_accounts('TSSW')
    if account_key not in all_accounts:
        return jsonify({'success': False, 'error': 'Account not found'}), 404
    try:
        _activate_account(account_key, all_accounts[account_key])
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error activating account {account_key}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/gmail/deactivate/<account_key>', methods=['POST'])
@login_required
def api_gmail_deactivate(account_key):
    """Deactivate a Gmail account (admin only)."""
    if not current_user.has_user_management_permission:
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    try:
        _deactivate_account(account_key)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error deactivating account {account_key}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/find_news')
@login_required
def find_news():
    """Find News dashboard - displays news Gmail accounts"""
    if not current_user.has_find_news_permission:
        flash('You do not have permission to access the Find News service.', 'error')
        return redirect(url_for('services'))
    
    news_accounts = _get_user_news_accounts(current_user.entity)
    
    # Get list of entities for TSSW users (they can add accounts to any entity)
    entities = []
    if current_user.entity.upper() == 'TSSW':
        entities = ['TSS1', 'TSS2', 'TSS3', 'TSSF', 'TSSW']
    
    return render_template('find_news.html', 
                         accounts=news_accounts,
                         selected_account=None,
                         can_manage_news=current_user.has_news_permission,
                         entities=entities,
                         user_entity=current_user.entity.upper())

def load_news_accounts_for_management(user_entity):
    """Load news accounts that a user can manage (from their entity or all if TSSW)"""
    accounts = []
    try:
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'news':
                        entity = parts[0].strip().upper()
                        email_addr = parts[1].strip()
                        app_password = parts[2].strip()
                        
                        # TSSW can see all, others only their entity
                        if user_entity.upper() == 'TSSW' or entity == user_entity.upper():
                            accounts.append({
                                'entity': entity,
                                'email': email_addr,
                                'app_password': app_password,
                                'line_num': line_num
                            })
    except FileNotFoundError:
        logging.error("gmailaccounts.txt file not found")
    except Exception as e:
        logging.error(f"Error reading gmailaccounts.txt: {e}")
    return accounts

def save_news_account(entity, email, app_password):
    """Add a new news Gmail account to gmailaccounts.txt"""
    try:
        with open('gmailaccounts.txt', 'a', encoding='utf-8') as f:
            f.write(f"\n{entity},{email},{app_password},news")
        _invalidate_gmail_accounts_cache()
        return True
    except Exception as e:
        logging.error(f"Error saving news account: {e}")
        return False

def update_news_account(old_entity, old_email, new_entity, new_email, new_password):
    """Update an existing news Gmail account in gmailaccounts.txt"""
    try:
        lines = []
        found = False
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'news':
                        if parts[0].strip().upper() == old_entity.upper() and parts[1].strip() == old_email:
                            lines.append(f"{new_entity},{new_email},{new_password},news\n")
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('gmailaccounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
            _invalidate_gmail_accounts_cache()
        return found
    except Exception as e:
        logging.error(f"Error updating news account: {e}")
        return False

def delete_news_account(entity, email):
    """Delete a news Gmail account from gmailaccounts.txt"""
    try:
        lines = []
        found = False
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'news':
                        if parts[0].strip().upper() == entity.upper() and parts[1].strip() == email:
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('gmailaccounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
            _invalidate_gmail_accounts_cache()
        return found
    except Exception as e:
        logging.error(f"Error deleting news account: {e}")
        return False

def load_extraction_accounts():
    """Load Gmail accounts with allow_extraction flag for TSSW users (legacy - only for y.ouiguemane)"""
    accounts = []
    try:
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'allow_extraction':
                        entity = parts[0].strip().upper()
                        email_addr = parts[1].strip()
                        app_password = parts[2].strip()
                        accounts.append({
                            'entity': entity,
                            'email': email_addr,
                            'app_password': app_password,
                            'line_num': line_num,
                            'is_legacy': True
                        })
    except FileNotFoundError:
        logging.error("gmailaccounts.txt file not found")
    except Exception as e:
        logging.error(f"Error reading gmailaccounts.txt: {e}")
    return accounts

def load_user_extraction_accounts(username):
    """Load extraction accounts for a specific user from user_extraction_accounts.txt"""
    accounts = []
    try:
        with open('user_extraction_accounts.txt', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 3:
                        account_username = parts[0].strip()
                        if account_username == username:
                            email_addr = parts[1].strip()
                            app_password = parts[2].strip()
                            accounts.append({
                                'username': account_username,
                                'email': email_addr,
                                'app_password': app_password,
                                'line_num': line_num,
                                'is_legacy': False
                            })
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Error reading user_extraction_accounts.txt: {e}")
    return accounts


def load_all_users_extraction_accounts():
    """Load ALL extraction accounts from user_extraction_accounts.txt, enriched with user full names."""
    # Build username -> full name map from users.txt
    name_map = {}
    try:
        all_users = load_users_from_file()
        for u in all_users:
            name_map[u['username']] = u.get('name') or u['username']
    except Exception as e:
        logging.error(f"[extraction admin] load users error: {e}")

    accounts = []
    try:
        with open('user_extraction_accounts.txt', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 3:
                        uname      = parts[0].strip()
                        email_addr = parts[1].strip()
                        app_pwd    = parts[2].strip()
                        accounts.append({
                            'username':   uname,
                            'owner_name': name_map.get(uname, uname),
                            'email':      email_addr,
                            'app_password': app_pwd,
                            'line_num':   line_num,
                            'is_legacy':  False,
                        })
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Error reading user_extraction_accounts.txt (all): {e}")

    # Also add legacy accounts
    try:
        legacy = load_extraction_accounts()
        for acc in legacy:
            acc['owner_name'] = acc.get('entity', 'Legacy')
        accounts = legacy + accounts
    except Exception as e:
        logging.error(f"[extraction admin] load legacy error: {e}")

    return accounts

def save_user_extraction_account(username, email, app_password):
    """Add a new extraction Gmail account for a user to user_extraction_accounts.txt"""
    try:
        with open('user_extraction_accounts.txt', 'a', encoding='utf-8') as f:
            f.write(f"\n{username},{email},{app_password}")
        return True
    except Exception as e:
        logging.error(f"Error saving user extraction account: {e}")
        return False

def update_user_extraction_account(username, old_email, new_email, new_password):
    """Update an existing user extraction Gmail account in user_extraction_accounts.txt"""
    try:
        lines = []
        found = False
        with open('user_extraction_accounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 3:
                        account_username = parts[0].strip()
                        account_email = parts[1].strip()
                        if account_username == username and account_email == old_email:
                            lines.append(f"{username},{new_email},{new_password}\n")
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('user_extraction_accounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
        return found
    except Exception as e:
        logging.error(f"Error updating user extraction account: {e}")
        return False

def delete_user_extraction_account(username, email):
    """Delete a user extraction Gmail account from user_extraction_accounts.txt"""
    try:
        lines = []
        found = False
        with open('user_extraction_accounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 3:
                        account_username = parts[0].strip()
                        account_email = parts[1].strip()
                        if account_username == username and account_email == email:
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('user_extraction_accounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
        return found
    except Exception as e:
        logging.error(f"Error deleting user extraction account: {e}")
        return False

def save_extraction_account(email, app_password):
    """Add a new extraction Gmail account to gmailaccounts.txt (legacy)"""
    try:
        with open('gmailaccounts.txt', 'a', encoding='utf-8') as f:
            f.write(f"\nEXTRACTION,{email},{app_password},allow_extraction")
        _invalidate_gmail_accounts_cache()
        return True
    except Exception as e:
        logging.error(f"Error saving extraction account: {e}")
        return False

def update_extraction_account(old_email, new_email, new_password):
    """Update an existing extraction Gmail account in gmailaccounts.txt (legacy)"""
    try:
        lines = []
        found = False
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'allow_extraction':
                        if parts[1].strip() == old_email:
                            entity = parts[0].strip()
                            lines.append(f"{entity},{new_email},{new_password},allow_extraction\n")
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('gmailaccounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
            _invalidate_gmail_accounts_cache()
        return found
    except Exception as e:
        logging.error(f"Error updating extraction account: {e}")
        return False

def delete_extraction_account(email):
    """Delete an extraction Gmail account from gmailaccounts.txt (legacy)"""
    try:
        lines = []
        found = False
        with open('gmailaccounts.txt', 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    parts = stripped.split(',')
                    if len(parts) >= 4 and parts[3].strip().lower() == 'allow_extraction':
                        if parts[1].strip() == email:
                            found = True
                            continue
                lines.append(line if line.endswith('\n') else line + '\n')
        
        if found:
            with open('gmailaccounts.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)
            _invalidate_gmail_accounts_cache()
        return found
    except Exception as e:
        logging.error(f"Error deleting extraction account: {e}")
        return False

@app.route('/api/extraction_accounts', methods=['GET'])
@login_required
def get_extraction_accounts():
    """Get extraction accounts for the current user (or all users for admins)"""
    if not current_user.has_extract_emails_permission:
        return jsonify({'error': 'Permission denied'}), 403

    # Admins see every user's accounts grouped by owner
    if current_user.has_user_management_permission:
        all_accounts = load_all_users_extraction_accounts()
        return jsonify({'success': True, 'accounts': all_accounts, 'is_admin': True})

    # Regular users see only their own accounts
    user_accounts = load_user_extraction_accounts(current_user.username)

    # Legacy accounts still visible to y.ouiguemane
    if current_user.username == 'y.ouiguemane':
        legacy_accounts = load_extraction_accounts()
        user_accounts = user_accounts + legacy_accounts

    return jsonify({'success': True, 'accounts': user_accounts, 'is_admin': False})

@app.route('/api/extraction_accounts', methods=['POST'])
@login_required
def add_extraction_account():
    """Add a new extraction Gmail account for the current user"""
    if not current_user.has_extract_emails_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    email = data.get('email', '').strip()
    app_password = data.get('app_password', '').strip()
    
    if not email or not app_password:
        return jsonify({'error': 'Email and app password are required'}), 400
    
    if '@' not in email or '.' not in email:
        return jsonify({'error': 'Invalid email format'}), 400
    
    if save_user_extraction_account(current_user.username, email, app_password):
        return jsonify({'success': True, 'message': 'Account added successfully'})
    else:
        return jsonify({'error': 'Failed to add account'}), 500

@app.route('/api/extraction_accounts', methods=['PUT'])
@login_required
def update_extraction_account_route():
    """Update an existing extraction Gmail account"""
    if not current_user.has_extract_emails_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    old_email = data.get('old_email', '').strip()
    new_email = data.get('email', '').strip()
    new_password = data.get('app_password', '').strip()
    is_legacy = data.get('is_legacy', False)
    
    if not all([old_email, new_email, new_password]):
        return jsonify({'error': 'All fields are required'}), 400
    
    # Handle legacy accounts (only for y.ouiguemane)
    if is_legacy:
        if current_user.username != 'y.ouiguemane':
            return jsonify({'error': 'Permission denied for legacy accounts'}), 403
        if update_extraction_account(old_email, new_email, new_password):
            return jsonify({'success': True, 'message': 'Account updated successfully'})
        else:
            return jsonify({'error': 'Account not found or update failed'}), 404
    
    # Handle user-specific accounts
    if update_user_extraction_account(current_user.username, old_email, new_email, new_password):
        return jsonify({'success': True, 'message': 'Account updated successfully'})
    else:
        return jsonify({'error': 'Account not found or update failed'}), 404

@app.route('/api/extraction_accounts', methods=['DELETE'])
@login_required
def delete_extraction_account_route():
    """Delete an extraction Gmail account"""
    if not current_user.has_extract_emails_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    email = data.get('email', '').strip()
    is_legacy = data.get('is_legacy', False)
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    
    # Handle legacy accounts (only for y.ouiguemane)
    if is_legacy:
        if current_user.username != 'y.ouiguemane':
            return jsonify({'error': 'Permission denied for legacy accounts'}), 403
        if delete_extraction_account(email):
            return jsonify({'success': True, 'message': 'Account deleted successfully'})
        else:
            return jsonify({'error': 'Account not found or delete failed'}), 404
    
    # Handle user-specific accounts
    if delete_user_extraction_account(current_user.username, email):
        return jsonify({'success': True, 'message': 'Account deleted successfully'})
    else:
        return jsonify({'error': 'Account not found or delete failed'}), 404

@app.route('/api/news_accounts', methods=['GET'])
@login_required
def get_manageable_news_accounts():
    """Get news accounts that the current user can manage"""
    if not current_user.has_news_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    accounts = load_news_accounts_for_management(current_user.entity)
    return jsonify({'success': True, 'accounts': accounts})

@app.route('/api/news_accounts', methods=['POST'])
@login_required
def add_news_account():
    """Add a new news Gmail account"""
    if not current_user.has_news_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    entity = data.get('entity', '').strip().upper()
    email = data.get('email', '').strip()
    app_password = data.get('app_password', '').strip()
    
    if not entity or not email or not app_password:
        return jsonify({'error': 'All fields are required'}), 400
    
    # Non-TSSW users can only add to their own entity
    if current_user.entity.upper() != 'TSSW' and entity != current_user.entity.upper():
        return jsonify({'error': 'You can only add accounts to your own entity'}), 403
    
    # Validate email format
    if '@' not in email or '.' not in email:
        return jsonify({'error': 'Invalid email format'}), 400
    
    if save_news_account(entity, email, app_password):
        return jsonify({'success': True, 'message': 'Account added successfully'})
    else:
        return jsonify({'error': 'Failed to add account'}), 500

@app.route('/api/news_accounts', methods=['PUT'])
@login_required
def update_news_account_route():
    """Update an existing news Gmail account"""
    if not current_user.has_news_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    old_entity = data.get('old_entity', '').strip().upper()
    old_email = data.get('old_email', '').strip()
    new_entity = data.get('entity', '').strip().upper()
    new_email = data.get('email', '').strip()
    new_password = data.get('app_password', '').strip()
    
    if not all([old_entity, old_email, new_entity, new_email, new_password]):
        return jsonify({'error': 'All fields are required'}), 400
    
    # Check permissions
    if current_user.entity.upper() != 'TSSW':
        if old_entity != current_user.entity.upper() or new_entity != current_user.entity.upper():
            return jsonify({'error': 'You can only modify accounts in your own entity'}), 403
    
    if update_news_account(old_entity, old_email, new_entity, new_email, new_password):
        return jsonify({'success': True, 'message': 'Account updated successfully'})
    else:
        return jsonify({'error': 'Account not found or update failed'}), 404

@app.route('/api/news_accounts', methods=['DELETE'])
@login_required
def delete_news_account_route():
    """Delete a news Gmail account"""
    if not current_user.has_news_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    entity = data.get('entity', '').strip().upper()
    email = data.get('email', '').strip()
    
    if not entity or not email:
        return jsonify({'error': 'Entity and email are required'}), 400
    
    # Check permissions
    if current_user.entity.upper() != 'TSSW' and entity != current_user.entity.upper():
        return jsonify({'error': 'You can only delete accounts from your own entity'}), 403
    
    if delete_news_account(entity, email):
        return jsonify({'success': True, 'message': 'Account deleted successfully'})
    else:
        return jsonify({'error': 'Account not found or delete failed'}), 404

@app.route('/api/news_emails/<account_key>')
@login_required
def get_news_emails(account_key):
    """Fetch last 50 inbox emails for a news account"""
    try:
        news_accounts = _get_user_news_accounts(current_user.entity)
        if account_key not in news_accounts:
            return jsonify({'error': 'Account not found or unauthorized'}), 404
        
        account = news_accounts[account_key]
        emails = fetch_news_emails_fast(account['email'], account['app_password'], limit=50)
        
        return jsonify({
            'success': True,
            'emails': emails,
            'account_key': account_key
        })
    except Exception as e:
        logging.error(f"Error fetching news emails: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/email_source/<account_key>/<uid>')
@login_required
def get_email_source(account_key, uid):
    """Get full email source (headers, MIME parts) for copying"""
    try:
        news_accounts = _get_user_news_accounts(current_user.entity)
        if account_key not in news_accounts:
            return jsonify({'error': 'Account not found or unauthorized'}), 404
        
        account = news_accounts[account_key]
        source = fetch_email_source(account['email'], account['app_password'], uid)
        
        if source:
            return jsonify({
                'success': True,
                'source': source,
                'uid': uid
            })
        else:
            return jsonify({'error': 'Email not found'}), 404
    except Exception as e:
        logging.error(f"Error fetching email source: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/news_events/<account_key>')
@login_required
def news_events(account_key):
    """Server-Sent Events for real-time news email updates"""
    news_accounts = _get_user_news_accounts(current_user.entity)
    if account_key not in news_accounts:
        return Response("Unauthorized", status=403)
    
    account = news_accounts[account_key]
    
    def event_stream():
        try:
            last_check_time = 0
            check_interval = 10  # Check every 10 seconds for new emails
            
            while True:
                current_time = time.time()
                
                if current_time - last_check_time >= check_interval:
                    try:
                        emails = fetch_news_emails_fast(account['email'], account['app_password'], limit=50)
                        yield f"data: {json.dumps({'emails': emails, 'timestamp': current_time})}\n\n"
                        last_check_time = current_time
                    except Exception as e:
                        logging.error(f"Error fetching news emails in SSE: {e}")
                        yield f"data: {json.dumps({'error': str(e)})}\n\n"
                else:
                    # Send heartbeat
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                
                time.sleep(5)  # Sleep 5 seconds between checks
                
        except Exception as e:
            logging.error(f"Error in news event stream: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(event_stream(), mimetype='text/event-stream')

def fetch_news_emails_fast(email_addr, app_password, limit=50):
    """Fetch last N inbox emails quickly (excluding spam) with Gmail folder/category info"""
    emails = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(email_addr, app_password)
        mail.select('INBOX')
        
        result, data = mail.uid('SEARCH', None, 'ALL')
        if result != 'OK' or not data[0]:
            return emails
        
        email_uids = data[0].split()
        if not email_uids:
            return emails
        
        recent_uids = email_uids[-limit:]
        recent_uids.reverse()
        
        category_cache = {}
        categories = ['social', 'promotions', 'updates', 'forums']
        for cat_key in categories:
            try:
                result_cat, data_cat = mail.uid('search', 'X-GM-RAW', f'"category:{cat_key}"')
                if result_cat == 'OK' and data_cat[0]:
                    cat_uids = set(data_cat[0].split())
                    for uid in recent_uids:
                        if uid in cat_uids:
                            category_cache[uid] = cat_key.capitalize()
            except Exception as e:
                logging.debug(f"Error caching category {cat_key}: {e}")
        
        for uid in recent_uids:
            try:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                result, msg_data = mail.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
                if result != 'OK' or not msg_data[0]:
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                
                from_header = msg.get('From', '')
                from_name, from_email_addr = email.utils.parseaddr(from_header)
                from_name = decode_mime_words(from_name) if from_name else from_email_addr
                
                from_domain = ''
                if '@' in from_email_addr:
                    from_domain = from_email_addr.split('@')[1]
                
                subject = decode_mime_words(msg.get('Subject', 'No Subject'))
                
                date_header = msg.get('Date', '')
                try:
                    date_obj = email.utils.parsedate_to_datetime(date_header)
                    date_str = date_obj.strftime('%Y-%m-%d %H:%M')
                except:
                    date_str = date_header[:20] if date_header else 'Unknown'
                
                folder = category_cache.get(uid, 'Primary')
                
                emails.append({
                    'uid': uid_str,
                    'subject': subject[:100] if len(subject) > 100 else subject,
                    'from_name': from_name[:50] if len(from_name) > 50 else from_name,
                    'from_domain': from_domain,
                    'date': date_str,
                    'folder': folder
                })
                
            except Exception as e:
                logging.debug(f"Error processing email UID {uid}: {e}")
                continue
                
    except Exception as e:
        logging.error(f"Error in fetch_news_emails_fast: {e}")
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass
    
    return emails

def fetch_email_source(email_addr, app_password, uid):
    """Fetch full email source (headers + body + MIME parts)"""
    mail = None
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(email_addr, app_password)
        mail.select('INBOX')
        
        result, msg_data = mail.uid('fetch', uid, '(RFC822)')
        if result != 'OK' or not msg_data[0]:
            return None
        
        raw_email = msg_data[0][1]
        if isinstance(raw_email, bytes):
            return raw_email.decode('utf-8', errors='replace')
        return str(raw_email)
        
    except Exception as e:
        logging.error(f"Error fetching email source: {e}")
        return None
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

def get_dns_resolver():
    """Get a configured DNS resolver with timeout and reliable nameservers"""
    resolver = dns.resolver.Resolver()
    resolver.timeout = 2
    resolver.lifetime = 3
    resolver.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']
    return resolver

def lookup_dmarc(domain):
    """Lookup DMARC record for a domain"""
    try:
        resolver = get_dns_resolver()
        dmarc_domain = f"_dmarc.{domain}"
        answers = resolver.resolve(dmarc_domain, 'TXT')
        for rdata in answers:
            txt_parts = []
            for s in rdata.strings:
                if isinstance(s, bytes):
                    txt_parts.append(s.decode('utf-8', errors='replace'))
                else:
                    txt_parts.append(str(s))
            txt_value = ''.join(txt_parts)
            if txt_value.lower().startswith('v=dmarc1'):
                return txt_value
        return None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
        return None
    except Exception as e:
        logging.debug(f"DMARC lookup error for {domain}: {e}")
        return None

def lookup_mx(domain):
    """Lookup MX records for a domain"""
    try:
        resolver = get_dns_resolver()
        answers = resolver.resolve(domain, 'MX')
        mx_records = []
        for rdata in answers:
            mx_records.append(f"{rdata.preference} {rdata.exchange}")
        return mx_records
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
        return None
    except Exception as e:
        logging.debug(f"MX lookup error for {domain}: {e}")
        return None

def lookup_txt(domain):
    """Lookup TXT records for a domain"""
    try:
        resolver = get_dns_resolver()
        answers = resolver.resolve(domain, 'TXT')
        txt_records = []
        for rdata in answers:
            txt_parts = []
            for s in rdata.strings:
                if isinstance(s, bytes):
                    txt_parts.append(s.decode('utf-8', errors='replace'))
                else:
                    txt_parts.append(str(s))
            txt_records.append(''.join(txt_parts))
        return txt_records
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
        return None
    except Exception as e:
        logging.debug(f"TXT lookup error for {domain}: {e}")
        return None

def is_valid_ip(ip):
    """Check if a string is a valid IPv4 address"""
    pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if not re.match(pattern, ip):
        return False
    parts = ip.split('.')
    return all(0 <= int(part) <= 255 for part in parts)

@app.route('/domain_checker')
@login_required
def domain_checker():
    """Domain checker service - DNS lookup tools"""
    if not current_user.has_domain_checker_permission:
        flash('You do not have permission to access the Domain Checker service.', 'error')
        return redirect(url_for('services'))
    return render_template('domain_checker.html')

@app.route('/api/domain_checker/dmarc', methods=['POST'])
@login_required
def api_dmarc_lookup():
    """API endpoint for DMARC lookups with parallel processing"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    results = [None] * len(domains)
    max_workers = min(20, len(domains)) if domains else 1
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(lookup_dmarc, domain): (i, domain) for i, domain in enumerate(domains)}
        for future in as_completed(future_to_index):
            idx, domain = future_to_index[future]
            try:
                dmarc_record = future.result()
                results[idx] = {
                    'domain': domain,
                    'dmarc': dmarc_record if dmarc_record else 'Not Found'
                }
            except Exception:
                results[idx] = {
                    'domain': domain,
                    'dmarc': 'Not Found'
                }
    
    return jsonify({'results': results})

@app.route('/api/domain_checker/dmarc_stream', methods=['GET'])
@login_required
def api_dmarc_lookup_stream():
    """SSE endpoint for DMARC lookups with real-time progress"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    domains_text = request.args.get('domains', '')
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    def generate():
        total = len(domains)
        if total == 0:
            yield f"data: {json.dumps({'type': 'complete', 'results': []})}\n\n"
            return
        
        results = [None] * total
        completed = 0
        max_workers = min(20, total)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {executor.submit(lookup_dmarc, domain): (i, domain) for i, domain in enumerate(domains)}
            
            for future in as_completed(future_to_index):
                idx, domain = future_to_index[future]
                try:
                    dmarc_record = future.result()
                    results[idx] = {
                        'domain': domain,
                        'dmarc': dmarc_record if dmarc_record else 'Not Found'
                    }
                except Exception:
                    results[idx] = {
                        'domain': domain,
                        'dmarc': 'Not Found'
                    }
                
                completed += 1
                
                progress_data = {
                    'type': 'progress',
                    'current': completed,
                    'total': total,
                    'domain': domain
                }
                yield f"data: {json.dumps(progress_data)}\n\n"
        
        complete_data = {
            'type': 'complete',
            'results': results
        }
        yield f"data: {json.dumps(complete_data)}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/domain_checker/dmarc_download', methods=['POST'])
@login_required
def api_dmarc_download():
    """Generate download file for domains missing DMARC records"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    template = data.get('template', 'v=DMARC1; p=reject; rua=mailto:postmaster@[domain]; ruf=mailto:dmarc@[domain]; fo=1; pct=100')
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    lines = []
    for domain in domains:
        dmarc_record = lookup_dmarc(domain)
        if not dmarc_record:
            txt_value = template.replace('[domain]', domain)
            extracted = tldextract.extract(domain)
            root_domain = f"{extracted.domain}.{extracted.suffix}" if extracted.domain and extracted.suffix else domain
            lines.append(f"{root_domain},{domain},TXT,{txt_value}")
    
    return jsonify({'content': '\n'.join(lines), 'count': len(lines)})

@app.route('/api/domain_checker/spf_generate', methods=['POST'])
@login_required
def api_spf_generate():
    """Generate SPF records with support for IPs, A records, and Include records"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    prefixed_domains_text = data.get('prefixed_domains', '')
    spf_type = data.get('spf_type', 'ips')
    distribute = data.get('distribute', False)
    paired = data.get('paired', False)
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    prefixed_domains = [d.strip().lower() for d in prefixed_domains_text.strip().split('\n') if d.strip()]
    
    if not domains:
        return jsonify({'error': 'No valid domains provided'}), 400
    
    if spf_type == 'ips' and prefixed_domains and len(prefixed_domains) != len(domains):
        return jsonify({'error': f'Number of prefixed domains ({len(prefixed_domains)}) must match number of domains ({len(domains)})'}), 400
    
    warning = None
    lines = []
    
    if spf_type == 'ips':
        ips_text = data.get('ips', '')
        
        if paired:
            raw_ips = [ip.strip() for ip in ips_text.strip().split('\n') if ip.strip()]
            valid_ips = [ip for ip in raw_ips if is_valid_ip(ip)]
            if len(valid_ips) != len(domains):
                return jsonify({'error': f'Paired mode requires equal counts: {len(domains)} domains vs {len(valid_ips)} IPs'}), 400
            for i, domain in enumerate(domains):
                full_domain = prefixed_domains[i] if prefixed_domains else domain
                spf_record = f'v=spf1 ip4:{valid_ips[i]} -all'
                lines.append(f"{domain},{full_domain},TXT,{spf_record}")
            return jsonify({'content': '\n'.join(lines), 'count': len(lines), 'warning': warning})
        
        ips = [ip.strip() for ip in ips_text.strip().split('\n') if ip.strip() and is_valid_ip(ip.strip())]
        
        if not ips:
            return jsonify({'error': 'No valid IP addresses provided'}), 400
        
        if len(ips) > 50:
            warning = f"Warning: {len(ips)} IPs provided. This may exceed SPF lookup limits."
        
        if distribute:
            if len(ips) < len(domains):
                return jsonify({'error': f'Not enough IPs ({len(ips)}) to distribute among {len(domains)} domains'}), 400
            
            ips_per_domain = len(ips) // len(domains)
            extra_ips = len(ips) % len(domains)
            ip_index = 0
            
            for i, domain in enumerate(domains):
                count = ips_per_domain + (1 if i < extra_ips else 0)
                domain_ips = ips[ip_index:ip_index + count]
                ip_index += count
                
                ip_parts = ' '.join([f'ip4:{ip}' for ip in domain_ips])
                spf_record = f'v=spf1 {ip_parts} -all'
                
                full_domain = prefixed_domains[i] if prefixed_domains else domain
                lines.append(f"{domain},{full_domain},TXT,{spf_record}")
        else:
            ip_parts = ' '.join([f'ip4:{ip}' for ip in ips])
            spf_record = f'v=spf1 {ip_parts} -all'
            
            for i, domain in enumerate(domains):
                full_domain = prefixed_domains[i] if prefixed_domains else domain
                lines.append(f"{domain},{full_domain},TXT,{spf_record}")
    
    elif spf_type == 'a_records':
        a_subdomains_text = data.get('a_subdomains', '')
        a_subdomain_lines = [line.strip() for line in a_subdomains_text.strip().split('\n') if line.strip()]
        
        if not a_subdomain_lines:
            return jsonify({'error': 'No subdomains provided for A records'}), 400
        
        # Validate subdomain lines count: must be exactly 1 OR equal to number of domains
        if len(a_subdomain_lines) != 1 and len(a_subdomain_lines) != len(domains):
            return jsonify({'error': f'Number of subdomain lines ({len(a_subdomain_lines)}) must be either 1 (to apply to all domains) or exactly {len(domains)} (to match each domain)'}), 400
        
        for i, domain in enumerate(domains):
            if i < len(a_subdomain_lines):
                subdomains_for_domain = [s.strip() for s in a_subdomain_lines[i].split(';') if s.strip()]
            else:
                subdomains_for_domain = [s.strip() for s in a_subdomain_lines[-1].split(';') if s.strip()]
            
            # Get the prefix from prefixed_domains (format: prefix.domain or just domain)
            full_domain = prefixed_domains[i] if i < len(prefixed_domains) and prefixed_domains else domain
            
            # Build a: parts with subdomain.prefix.domain format
            # Check if full_domain has a prefix (ends with .domain)
            if full_domain.endswith('.' + domain) and full_domain != domain:
                # Has prefix - extract it (e.g., mail.example.com -> mail)
                prefix = full_domain[:-len('.' + domain)]
                a_parts = ' '.join([f'a:{sub}' for sub in subdomains_for_domain])
            else:
                # No prefix - just use subdomain.domain
                a_parts = ' '.join([f'a:{sub}' for sub in subdomains_for_domain])
            
            spf_record = f'v=spf1 {a_parts} -all'
            lines.append(f"{domain},{full_domain},TXT,{spf_record}")
    
    elif spf_type == 'includes':
        include_domains_text = data.get('include_domains', '')
        include_domain_lines = [line.strip() for line in include_domains_text.strip().split('\n') if line.strip()]
        
        if not include_domain_lines:
            return jsonify({'error': 'No include domains provided'}), 400
        
        for i, domain in enumerate(domains):
            if i < len(include_domain_lines):
                includes_for_domain = [s.strip() for s in include_domain_lines[i].split(';') if s.strip()]
            else:
                includes_for_domain = [s.strip() for s in include_domain_lines[-1].split(';') if s.strip()]
            
            include_parts = ' '.join([f'include:{inc}' for inc in includes_for_domain])
            spf_record = f'v=spf1 {include_parts} -all'
            
            # Format: _spf.domain (no prefix before _spf)
            spf_subdomain = f'_spf.{domain}'
            full_domain = prefixed_domains[i] if i < len(prefixed_domains) and prefixed_domains else domain
            lines.append(f"{domain},{full_domain},TXT,{spf_record}")
    
    else:
        return jsonify({'error': 'Invalid SPF type specified'}), 400
    
    return jsonify({'content': '\n'.join(lines), 'count': len(lines), 'warning': warning})

@app.route('/api/domain_checker/a_generate', methods=['POST'])
@login_required
def api_a_generate():
    """Generate A record entries"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    subdomain = data.get('subdomain', '').strip()
    ips_text = data.get('ips', '')
    paired = data.get('paired', False)
    
    distribute = data.get('distribute', False)
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    if not domains:
        return jsonify({'error': 'No valid domains provided'}), 400
    
    warning = None
    lines = []

    if paired:
        raw_ips = [ip.strip() for ip in ips_text.strip().split('\n') if ip.strip()]
        if len(domains) != len(raw_ips):
            return jsonify({'error': f'Paired mode requires equal counts: {len(domains)} domains vs {len(raw_ips)} IPs'}), 400
        invalid_ips = [ip for ip in raw_ips if not is_valid_ip(ip)]
        if invalid_ips:
            return jsonify({'error': f'Invalid IP address(es): {", ".join(invalid_ips[:5])}'}), 400
        for domain, ip in zip(domains, raw_ips):
            full_domain = f"{subdomain}.{domain}" if subdomain else domain
            parts = domain.split('.')
            base_domain = '.'.join(parts[-2:]) if len(parts) > 1 else domain
            lines.append(f"{base_domain},{full_domain},TXT,Arecords:{ip}")
    else:
        ips = [ip.strip() for ip in ips_text.strip().split('\n') if ip.strip() and is_valid_ip(ip.strip())]
        if not ips:
            return jsonify({'error': 'No valid IP addresses provided'}), 400
        if len(ips) > 50:
            warning = f"Warning: {len(ips)} IPs provided."
        if distribute:
            if len(ips) < len(domains):
                return jsonify({'error': f'Not enough IPs ({len(ips)}) to distribute among {len(domains)} domains'}), 400
            ips_per_domain = len(ips) // len(domains)
            extra_ips = len(ips) % len(domains)
            ip_index = 0
            for i, domain in enumerate(domains):
                count = ips_per_domain + (1 if i < extra_ips else 0)
                domain_ips = ips[ip_index:ip_index + count]
                ip_index += count
                full_domain = f"{subdomain}.{domain}" if subdomain else domain
                parts = domain.split('.')
                base_domain = '.'.join(parts[-2:]) if len(parts) > 1 else domain
                lines.append(f"{base_domain},{full_domain},TXT,Arecords:{';'.join(domain_ips)}")
        else:
            ips_str = ';'.join(ips)
            for domain in domains:
                full_domain = f"{subdomain}.{domain}" if subdomain else domain
                parts = domain.split('.')
                base_domain = '.'.join(parts[-2:]) if len(parts) > 1 else domain
                lines.append(f"{base_domain},{full_domain},TXT,Arecords:{ips_str}")
    
    return jsonify({'content': '\n'.join(lines), 'count': len(lines), 'warning': warning})

@app.route('/api/domain_checker/mx', methods=['POST'])
@login_required
def api_mx_lookup():
    """API endpoint for MX lookups"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    results = []
    for domain in domains:
        mx_records = lookup_mx(domain)
        results.append({
            'domain': domain,
            'mx': mx_records if mx_records else ['Not Found']
        })
    
    return jsonify({'results': results})

@app.route('/api/domain_checker/mx_stream')
@login_required
def api_mx_stream():
    """SSE endpoint for MX lookups with progress updates"""
    if not current_user.has_domain_checker_permission:
        return Response("Permission denied", status=403)
    
    domains_text = request.args.get('domains', '')
    domains_text = urllib.parse.unquote(domains_text)
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    def generate():
        total = len(domains)
        results = [None] * total
        completed = [0]
        
        max_workers = min(20, total) if total else 1
        
        def process_domain(idx, domain):
            mx_records = lookup_mx(domain)
            return idx, {
                'domain': domain,
                'mx': mx_records if mx_records else ['Not Found']
            }
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(process_domain, i, d): i for i, d in enumerate(domains)}
            for future in as_completed(future_to_idx):
                try:
                    idx, result = future.result()
                    results[idx] = result
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
                except Exception:
                    idx = future_to_idx[future]
                    results[idx] = {'domain': domains[idx], 'mx': ['Not Found']}
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
        
        yield f"data: {json.dumps({'type': 'complete', 'results': results})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/domain_checker/txt', methods=['POST'])
@login_required
def api_txt_lookup():
    """API endpoint for TXT lookups"""
    if not current_user.has_domain_checker_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    data = request.get_json()
    domains_text = data.get('domains', '')
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    results = []
    for domain in domains:
        txt_records = lookup_txt(domain)
        results.append({
            'domain': domain,
            'txt': txt_records if txt_records else ['Not Found']
        })
    
    return jsonify({'results': results})

@app.route('/api/domain_checker/txt_stream')
@login_required
def api_txt_stream():
    """SSE endpoint for TXT lookups with progress updates"""
    if not current_user.has_domain_checker_permission:
        return Response("Permission denied", status=403)
    
    domains_text = request.args.get('domains', '')
    domains_text = urllib.parse.unquote(domains_text)
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    def generate():
        total = len(domains)
        results = [None] * total
        completed = [0]
        
        max_workers = min(20, total) if total else 1
        
        def process_domain(idx, domain):
            txt_records = lookup_txt(domain)
            return idx, {
                'domain': domain,
                'txt': txt_records if txt_records else ['Not Found']
            }
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(process_domain, i, d): i for i, d in enumerate(domains)}
            for future in as_completed(future_to_idx):
                try:
                    idx, result = future.result()
                    results[idx] = result
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
                except Exception:
                    idx = future_to_idx[future]
                    results[idx] = {'domain': domains[idx], 'txt': ['Not Found']}
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
        
        yield f"data: {json.dumps({'type': 'complete', 'results': results})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/tssw_rapport')
@app.route('/tssw_rapport/')
@login_required
def tssw_rapport_index():
    """TSSW Rapport service index - only for users with tssw_report permission"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/index.html', current_user=current_user)

@app.route('/tssw_rapport/domain')
@login_required
def tssw_rapport_domain():
    """TSSW Rapport domain projects page"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/domain.html', current_user=current_user)

@app.route('/tssw_rapport/offer')
@login_required
def tssw_rapport_offer():
    """TSSW Rapport offers page"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/offer.html', current_user=current_user)

@app.route('/tssw_rapport/warmup')
@login_required
def tssw_rapport_warmup():
    """TSSW Rapport warmup page"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/warmup.html', current_user=current_user)

@app.route('/tssw_rapport/app')
@login_required
def tssw_rapport_app():
    """TSSW Rapport app development page"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/app_report.html', current_user=current_user)

@app.route('/tssw_rapport/chromprocess')
@login_required
def tssw_rapport_chromprocess():
    """TSSW Rapport Chrome extensions page"""
    if not current_user.has_tssw_report_permission:
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    return render_template('tssw_rapport/chromprocess.html', current_user=current_user)

@app.route('/api/domain_checker/unified_lookup')
@login_required
def api_unified_lookup():
    """SSE endpoint for unified domain lookup with MX, TXT, SPF, A IP records"""
    if not current_user.has_domain_checker_permission:
        return Response("Permission denied", status=403)
    
    domains_text = request.args.get('domains', '')
    domains_text = urllib.parse.unquote(domains_text)
    check_mx = request.args.get('check_mx', 'false') == 'true'
    check_txt = request.args.get('check_txt', 'false') == 'true'
    check_spf = request.args.get('check_spf', 'false') == 'true'
    check_a = request.args.get('check_a', 'false') == 'true'
    
    domains = [d.strip().lower() for d in domains_text.strip().split('\n') if d.strip()]
    
    def lookup_a_records(domain):
        """Lookup A records (IP addresses) for a domain"""
        try:
            resolver = get_dns_resolver()
            answers = resolver.resolve(domain, 'A')
            return [str(rdata) for rdata in answers]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
            return None
        except Exception as e:
            logging.debug(f"A lookup error for {domain}: {e}")
            return None
    
    def lookup_spf_record(domain):
        """Lookup SPF record for a domain"""
        try:
            resolver = get_dns_resolver()
            answers = resolver.resolve(domain, 'TXT')
            for rdata in answers:
                txt_parts = []
                for s in rdata.strings:
                    if isinstance(s, bytes):
                        txt_parts.append(s.decode('utf-8', errors='replace'))
                    else:
                        txt_parts.append(str(s))
                txt_value = ''.join(txt_parts)
                if txt_value.lower().startswith('v=spf1'):
                    return txt_value
            return None
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
            return None
        except Exception as e:
            logging.debug(f"SPF lookup error for {domain}: {e}")
            return None
    
    def process_domain(idx, domain):
        result = {'domain': domain}
        if check_mx:
            mx_records = lookup_mx(domain)
            result['mx'] = mx_records if mx_records else None
            result['mx_found'] = mx_records is not None and len(mx_records) > 0
        if check_txt:
            txt_records = lookup_txt(domain)
            result['txt'] = txt_records if txt_records else None
            result['txt_found'] = txt_records is not None and len(txt_records) > 0
        if check_spf:
            spf_record = lookup_spf_record(domain)
            result['spf'] = spf_record
            result['spf_found'] = spf_record is not None
        if check_a:
            a_records = lookup_a_records(domain)
            result['a'] = a_records if a_records else None
            result['a_found'] = a_records is not None and len(a_records) > 0
        return idx, result
    
    def generate():
        total = len(domains)
        results = [None] * total
        completed = [0]
        
        max_workers = min(20, total) if total else 1
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(process_domain, i, d): i for i, d in enumerate(domains)}
            for future in as_completed(future_to_idx):
                try:
                    idx, result = future.result()
                    results[idx] = result
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
                except Exception:
                    idx = future_to_idx[future]
                    result = {'domain': domains[idx]}
                    if check_mx:
                        result['mx'] = None
                        result['mx_found'] = False
                    if check_txt:
                        result['txt'] = None
                        result['txt_found'] = False
                    if check_spf:
                        result['spf'] = None
                        result['spf_found'] = False
                    if check_a:
                        result['a'] = None
                        result['a_found'] = False
                    results[idx] = result
                    completed[0] += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed[0], 'total': total})}\n\n"
        
        yield f"data: {json.dumps({'type': 'complete', 'results': results})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/blacklist_lookup')
@login_required
def blacklist_lookup():
    """Blacklist Lookup Service"""
    if not current_user.has_blacklist_lookup_permission:
        return redirect(url_for('services'))
    return render_template('blacklist_lookup.html')

# Spamhaus DQS Key Manager for load balancing
import threading

class DQSKeyManager:
    def __init__(self, keys_string):
        self.keys = [k.strip() for k in keys_string.split(',') if k.strip()]
        self.index = 0
        self.lock = threading.Lock()

    def get_key(self):
        with self.lock:
            if not self.keys:
                return "f3jqdoqpeyipweiizk7onufnlm" # Fallback
            key = self.keys[self.index]
            self.index = (self.index + 1) % len(self.keys)
            return key

dqs_keys_env = os.environ.get("DQS_KEYS", os.environ.get("DQS_KEY", "f3jqdoqpeyipweiizk7onufnlm,tfpurh2dwpwbxt4ylugrjtqexm"))
dqs_manager = DQSKeyManager(dqs_keys_env)

# Regex patterns for validation
import re
import ipaddress
DOMAIN_REGEX = re.compile(r"^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")

def is_ipv4(ip):
    """Check if string is a valid IPv4 address using ipaddress module"""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.version == 4
    except ValueError:
        return False

def is_ipv6(ip):
    """Check if string is a valid IPv6 address using ipaddress module (supports all formats including IPv4-mapped)"""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.version == 6
    except ValueError:
        return False

def is_valid_ip(ip):
    """Check if string is a valid IPv4 or IPv6 address using ipaddress module"""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False

def expand_ipv6(ip):
    """Expand an IPv6 address to full 32 hex characters format for DNS lookup"""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            return addr.exploded.replace(':', '')
        return None
    except ValueError:
        return None

def check_spamhaus_ip(ip, dqs_key):
    """Check IP against Spamhaus blocklists - supports both IPv4 and IPv6"""
    try:
        if is_ipv4(ip):
            rev = ".".join(ip.split(".")[::-1])
            query = f"{rev}.{dqs_key}.zen.dq.spamhaus.net"
        elif is_ipv6(ip):
            expanded = expand_ipv6(ip)
            if not expanded:
                return set()
            rev = ".".join(reversed(expanded))
            query = f"{rev}.{dqs_key}.zen.dq.spamhaus.net"
        else:
            return set()
        
        print(f"[DEBUG] Querying: {query}")
        answers = blacklist_resolver.resolve(query, "A")
        found = {r.to_text() for r in answers}
        print(f"[DEBUG] IP {ip} -> Answers: {found}")

        listings = set()
        if "127.0.0.3" in found:
            listings.add("css")
        if found.intersection({"127.0.0.2", "127.0.0.9"}):
            listings.add("sbl")
        if found.intersection({"127.0.0.4", "127.0.0.5", "127.0.0.6", "127.0.0.7"}):
            listings.add("xbl")
        if found.intersection({"127.0.0.10", "127.0.0.11"}):
            listings.add("pbl")
        
        return listings
    except dns.resolver.NXDOMAIN:
        print(f"[DEBUG] IP {ip} -> NXDOMAIN (clean)")
        return set()
    except Exception as e:
        print(f"[DEBUG] IP {ip} -> Exception: {e}")
        return set()

def check_barracuda(ip):
    """Check IP against Barracuda blocklist - supports both IPv4 and IPv6"""
    try:
        if is_ipv4(ip):
            rev = ".".join(ip.split(".")[::-1])
            query = f"{rev}.b.barracudacentral.org"
        elif is_ipv6(ip):
            expanded = expand_ipv6(ip)
            if not expanded:
                return False
            rev = ".".join(reversed(expanded))
            query = f"{rev}.b.barracudacentral.org"
        else:
            return False
        
        blacklist_resolver.resolve(query, "A")
        return True
    except dns.resolver.NXDOMAIN:
        return False
    except Exception:
        return False

def check_spamhaus_domain(domain, dqs_key):
    """Check domain against Spamhaus DBL - EXACT copy from ipchecker.py"""
    try:
        query = f"{domain}.{dqs_key}.dbl.dq.spamhaus.net"
        answers = blacklist_resolver.resolve(query, "A")
        if any(r.to_text().startswith("127.0.1.") for r in answers):
            return "dbl"
        return None
    except dns.resolver.NXDOMAIN:
        return "clean"
    except Exception:
        return None

def check_single_entry(entry_data, dqs_key):
    """Check a single IP/domain entry against all blacklists - for parallel processing
    Uses EXACT same logic as ipchecker.py process_item function"""
    idx, serveur, ip, domain, status = entry_data
    
    # Initialize result
    result = {
        'idx': idx,
        'serveur': serveur,
        'ip': ip if ip else '',
        'domain': domain if domain else '',
        'status': status,
        'css': 'Clean',
        'pbl': 'Clean',
        'xbl': 'Clean',
        'sbl': 'Clean',
        'barracuda': 'Clean',
        'dbl': 'Clean'
    }
    
    # Check IP blocklists only if IP is provided (same logic as ipchecker.py)
    if ip:
        spamhaus_results = check_spamhaus_ip(ip, dqs_key)
        barracuda = check_barracuda(ip)
        
        # Map spamhaus results to the correct columns (handles multiple)
        for listing in spamhaus_results:
            result[listing] = 'Listed'
        
        if barracuda:
            result['barracuda'] = 'Listed'
    
    # Check domain blocklist only if domain is provided
    if domain:
        dbl_result = check_spamhaus_domain(domain, dqs_key)
        if dbl_result == "dbl":
            result['dbl'] = 'Listed'
    
    return result

@app.route('/api/check_blacklists_stream', methods=['POST'])
@login_required
def check_blacklists_stream():
    """SSE streaming endpoint for blacklist checks with parallel processing"""
    if not current_user.has_blacklist_lookup_permission:
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        data = request.get_json()
        lines = data.get('lines', [])
        
        if not lines:
            return jsonify({'error': 'No data provided'}), 400
        
        # Parse and validate all lines using same logic as ipchecker.py
        valid_entries = []
        errors = []
        
        for idx, line in enumerate(lines):
            line = line.strip()
            
            if not line:
                continue
            
            # Split on semicolons - new format: SERVEUR;IP;DOMAIN;STATUS
            parts = line.split(";")
            
            serveur = ""
            ip = ""
            domain = ""
            status = ""
            
            if len(parts) == 4:
                # Format: SERVEUR;IP;DOMAIN;STATUS
                serveur = parts[0].strip()
                ip = parts[1].strip()
                domain = parts[2].strip()
                status = parts[3].strip()
            elif len(parts) == 3:
                # Format: SERVEUR;IP;DOMAIN
                serveur = parts[0].strip()
                ip = parts[1].strip()
                domain = parts[2].strip()
                status = ""
            elif len(parts) == 2:
                # Format: SERVEUR;VALUE (IP or Domain)
                serveur = parts[0].strip()
                value = parts[1].strip()
                if is_valid_ip(value):
                    ip = value
                    domain = ""
                elif DOMAIN_REGEX.match(value):
                    ip = ""
                    domain = value
                else:
                    errors.append(f"Line {idx + 1}: Invalid IP or domain '{value}'")
                    continue
            elif len(parts) == 1:
                # Format: Just IP or Domain
                value = parts[0].strip()
                if is_valid_ip(value):
                    serveur = "unknown"
                    ip = value
                    domain = ""
                elif DOMAIN_REGEX.match(value):
                    serveur = "unknown"
                    ip = ""
                    domain = value
                else:
                    errors.append(f"Line {idx + 1}: Invalid format")
                    continue
            else:
                errors.append(f"Line {idx + 1}: Too many semicolons in line")
                continue
            
            # Validate IP format if provided (supports both IPv4 and IPv6)
            if ip:
                if not is_valid_ip(ip):
                    errors.append(f"Line {idx + 1}: Invalid IP '{ip}'")
                    continue
            
            # Validate domain format if provided
            if domain:
                if not DOMAIN_REGEX.match(domain):
                    errors.append(f"Line {idx + 1}: Invalid domain '{domain}'")
                    continue
            
            # Need at least IP or domain
            if not ip and not domain:
                errors.append(f"Line {idx + 1}: No valid IP or domain found")
                continue
            
            valid_entries.append((idx, serveur, ip, domain, status))
        
        total = len(valid_entries)
        
        # Get a DQS key for THIS process/request
        request_dqs_key = dqs_manager.get_key()
        print(f"[BLACKLIST] Process starting with DQS_KEY: {request_dqs_key[:8]}...")
        
        def generate():
            yield f"data: {json.dumps({'type': 'start', 'total': total, 'errors': errors})}\n\n"
            
            results = []
            completed = 0
            
            # Use ThreadPoolExecutor for parallel processing (50 concurrent to avoid DNS rate limiting)
            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = {executor.submit(check_single_entry, entry, request_dqs_key): entry for entry in valid_entries}
                
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        logging.debug(f"Error processing entry: {e}")
                    
                    completed += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': completed, 'total': total})}\n\n"
            
            # Sort results by original index
            results.sort(key=lambda x: x['idx'])
            # Remove idx from results
            for r in results:
                del r['idx']
            
            yield f"data: {json.dumps({'type': 'complete', 'results': results})}\n\n"
        
        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive'
            }
        )
    
    except Exception as e:
        logging.error(f"Error in check_blacklists_stream: {e}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500

application = app

# --- Useful Extensions Service ---
def load_extensions():
    try:
        if os.path.exists('extensions.json'):
            with open('extensions.json', 'r') as f:
                content = f.read()
                return json.loads(content) if content else []
    except Exception as e:
        logging.error(f"Error loading extensions: {e}")
    return []

def save_extensions(extensions):
    try:
        with open('extensions.json', 'w') as f:
            json.dump(extensions, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving extensions: {e}")

EXTENSION_CONTROL_FILE = 'tssApi/extensions/extension_controlle.txt'

def load_extension_control():
    """Returns list of dicts: [{name, version, status}]"""
    entries = []
    try:
        if os.path.exists(EXTENSION_CONTROL_FILE):
            with open(EXTENSION_CONTROL_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(',', 2)
                    if len(parts) == 3:
                        entries.append({'name': parts[0], 'version': parts[1], 'status': parts[2]})
    except Exception as e:
        logging.error(f"Error loading extension control: {e}")
    return entries

def save_extension_control(entries):
    try:
        os.makedirs(os.path.dirname(EXTENSION_CONTROL_FILE), exist_ok=True)
        with open(EXTENSION_CONTROL_FILE, 'w') as f:
            for e in entries:
                f.write(f"{e['name']},{e['version']},{e['status']}\n")
    except Exception as e:
        logging.error(f"Error saving extension control: {e}")

def append_extension_control(ext_name, version):
    """Append a new disallow entry for ext_name + version."""
    entries = load_extension_control()
    entries.append({'name': ext_name, 'version': version, 'status': 'disallow'})
    save_extension_control(entries)

def get_versions_status(ext_name):
    """Returns dict {version_str: status} for a given extension name."""
    entries = load_extension_control()
    return {e['version']: e['status'] for e in entries if e['name'] == ext_name}

def get_extension_service_users():
    """Users who can access the Useful Extensions service (download or manage)."""
    users = load_users_from_file()
    return [
        u for u in users
        if 'download_extension' in u.get('permissions', []) or 'add_extensions' in u.get('permissions', [])
    ]

def get_user_entity_lookup():
    """Return entities indexed by the names used by the extensions activity feed.

    The activity feed normally identifies a user by ``Mailer``.  Supporting
    both the configured username and display name keeps the lookup working
    whether the extension reports either value.
    """
    lookup = {}
    for user in load_users_from_file():
        entity = user.get('entity', '').strip() or 'Unknown Entity'
        for identity in (user.get('username', ''), user.get('name', '')):
            identity = identity.strip().casefold()
            if identity:
                lookup[identity] = entity
    return lookup

def add_entities_to_extension_active_users(data):
    """Add the configured entity to each active-user record without credentials."""
    entity_lookup = get_user_entity_lookup()
    enriched = []
    for record in data if isinstance(data, list) else []:
        if not isinstance(record, dict):
            enriched.append(record)
            continue
        enriched_record = record.copy()
        identity = str(record.get('Mailer', '')).strip().casefold()
        enriched_record['Entity'] = entity_lookup.get(identity, 'Unknown Entity')
        enriched.append(enriched_record)
    return enriched

def user_can_download_extension(ext, username):
    """Whether `username` is allowed to download `ext`, based on its allowed_users setting.
    Missing/'all' means everyone with service access can download it (default for legacy entries)."""
    allowed = ext.get('allowed_users', 'all')
    if not allowed or allowed == 'all':
        return True
    if isinstance(allowed, list):
        return username in allowed
    return True

@app.route('/useful-extensions')
@login_required
def useful_extensions():
    if not (current_user.has_download_extension_permission or current_user.has_add_extensions_permission):
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    extensions = load_extensions()
    service_users = get_extension_service_users()
    return render_template('useful_extensions.html', extensions=extensions, service_users=service_users,
                            can_download_ext=lambda ext: user_can_download_extension(ext, current_user.username))

@app.route('/api/extensions', methods=['POST'])
@login_required
def add_extension():
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    name = request.form.get('name')
    browser = request.form.get('browser')
    ext_type = request.form.get('ext_type', 'normal')
    if ext_type not in ('normal', 'updated'):
        ext_type = 'normal'
    file = request.files.get('file')
    description = request.form.get('description', '')

    if not all([name, browser, file]):
        return jsonify({'error': 'All fields are required'}), 400

    download_access = request.form.get('download_access', 'all')
    if download_access == 'specific':
        allowed_users = [u.strip() for u in request.form.getlist('allowed_users') if u.strip()]
        if not allowed_users:
            return jsonify({'error': 'Select at least one user, or choose "All users"'}), 400
    else:
        allowed_users = 'all'

    extensions = load_extensions()
    existing_ids = {int(e['id']) for e in extensions if str(e.get('id', '')).isdigit()}
    ext_id = str(max(existing_ids, default=0) + 1)

    if ext_type == 'updated' and not description.strip():
        return jsonify({'error': 'Description is required for Updated Extensions'}), 400

    version = request.form.get('version', '').strip() if ext_type == 'updated' else ''
    if ext_type == 'updated' and not version:
        return jsonify({'error': 'Version is required for Updated Extensions'}), 400

    original_filename = file.filename or ''
    file_ext = os.path.splitext(original_filename)[1].lower()
    if file_ext not in ('.rar', '.zip'):
        return jsonify({'error': 'Only .rar and .zip files are allowed'}), 400
        
    filename = f"{ext_id}_{int(time.time())}{file_ext}"
    upload_path = os.path.join('static/downloads', filename)
    os.makedirs('static/downloads', exist_ok=True)
    file.save(upload_path)
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    new_ext = {
        'id': ext_id,
        'name': name,
        'browser': browser,
        'filename': filename,
        'date': now_str,
        'type': ext_type,
        'added_by': current_user.username,
        'allowed_users': allowed_users
    }
    if ext_type == 'updated':
        new_ext['versions'] = [{
            'filename': filename,
            'description': description.strip(),
            'version': version,
            'date': now_str,
            'uploaded_by': 'Tssw Team'
        }]
        append_extension_control(name, version)
    extensions.append(new_ext)
    save_extensions(extensions)
    
    return jsonify({'success': True})

@app.route('/api/extensions/<ext_id>/update', methods=['POST'])
@login_required
def update_extension(ext_id):
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403

    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        return jsonify({'error': 'Extension not found'}), 404
    if ext.get('type') != 'updated':
        return jsonify({'error': 'This extension does not support updates'}), 400

    file = request.files.get('file')
    description = request.form.get('description', '')
    version = request.form.get('version', '').strip()
    if not file:
        return jsonify({'error': 'File is required'}), 400
    if not description.strip():
        return jsonify({'error': 'Update description is required'}), 400
    if not version:
        return jsonify({'error': 'Version is required'}), 400

    original_filename = file.filename or ''
    file_ext = os.path.splitext(original_filename)[1].lower()
    if file_ext not in ('.rar', '.zip'):
        return jsonify({'error': 'Only .rar and .zip files are allowed'}), 400

    filename = f"{ext_id}_{int(time.time())}{file_ext}"
    upload_path = os.path.join('static/downloads', filename)
    os.makedirs('static/downloads', exist_ok=True)
    file.save(upload_path)

    downloadable = request.form.get('downloadable') == '1'

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    if 'versions' not in ext:
        ext['versions'] = []
    ext['versions'].append({
        'filename': filename,
        'description': description.strip(),
        'version': version,
        'date': now_str,
        'uploaded_by': 'Tssw Team',
        'downloadable': downloadable
    })
    ext['filename'] = filename
    ext['date'] = now_str

    save_extensions(extensions)
    append_extension_control(ext['name'], version)
    return jsonify({'success': True})

@app.route('/api/extensions/<ext_id>/versions/<int:ver_idx>/description', methods=['POST'])
@login_required
def edit_version_description(ext_id, ver_idx):
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        return jsonify({'error': 'Extension not found'}), 404
    versions = ext.get('versions', [])
    if ver_idx < 0 or ver_idx >= len(versions):
        return jsonify({'error': 'Version not found'}), 404
    data = request.get_json(silent=True) or {}
    description = data.get('description', '').strip()
    if not description:
        return jsonify({'error': 'Description cannot be empty'}), 400
    versions[ver_idx]['description'] = description
    save_extensions(extensions)
    return jsonify({'success': True})

@app.route('/api/extensions/<ext_id>/versions/<int:ver_idx>/status', methods=['POST'])
@login_required
def toggle_version_status(ext_id, ver_idx):
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        return jsonify({'error': 'Extension not found'}), 404
    versions = ext.get('versions', [])
    if ver_idx < 0 or ver_idx >= len(versions):
        return jsonify({'error': 'Version not found'}), 404
    ver = versions[ver_idx]
    version_str = ver.get('version', '').strip()
    if not version_str:
        return jsonify({'error': 'No version number declared for this entry — cannot toggle'}), 400
    entries = load_extension_control()
    found = False
    new_status = None
    for entry in entries:
        if entry['name'] == ext['name'] and entry['version'] == version_str:
            entry['status'] = 'allow' if entry['status'] == 'disallow' else 'disallow'
            new_status = entry['status']
            found = True
            break
    if not found:
        new_status = 'allow'
        entries.append({'name': ext['name'], 'version': version_str, 'status': new_status})
    save_extension_control(entries)
    return jsonify({'success': True, 'status': new_status})

@app.route('/api/extensions/<ext_id>/versions/<int:ver_idx>', methods=['DELETE'])
@login_required
def delete_version(ext_id, ver_idx):
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        return jsonify({'error': 'Extension not found'}), 404
    versions = ext.get('versions', [])
    if ver_idx < 0 or ver_idx >= len(versions):
        return jsonify({'error': 'Version not found'}), 404
    if len(versions) <= 1:
        return jsonify({'error': 'Cannot delete the only version. Delete the whole extension instead.'}), 400
    ver = versions[ver_idx]
    try:
        os.remove(os.path.join('static/downloads', ver.get('filename', '')))
    except Exception:
        pass
    versions.pop(ver_idx)
    ext['filename'] = versions[-1].get('filename', '')
    ext['date'] = versions[-1].get('date', '')
    save_extensions(extensions)
    return jsonify({'success': True})

@app.route('/extensions/<ext_id>/download')
@login_required
def download_extension_file(ext_id):
    from flask import send_from_directory
    if not (current_user.has_download_extension_permission or current_user.has_add_extensions_permission):
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        flash('Extension not found.', 'error')
        return redirect(url_for('useful_extensions'))
    if not current_user.has_add_extensions_permission and not user_can_download_extension(ext, current_user.username):
        flash('You do not have access to download this extension.', 'error')
        return redirect(url_for('useful_extensions'))
    filename = ext.get('filename')
    if not filename:
        flash('File not found.', 'error')
        return redirect(url_for('useful_extensions'))
    return send_from_directory('static/downloads', filename, as_attachment=True)

@app.route('/extensions/<ext_id>/versions/<int:ver_idx>/download')
@login_required
def download_extension_version_file(ext_id, ver_idx):
    from flask import send_from_directory
    if not (current_user.has_download_extension_permission or current_user.has_add_extensions_permission):
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        flash('Extension not found.', 'error')
        return redirect(url_for('useful_extensions'))
    versions = ext.get('versions', [])
    if ver_idx < 0 or ver_idx >= len(versions):
        flash('Version not found.', 'error')
        return redirect(url_for('extension_history', ext_id=ext_id))
    if not current_user.has_add_extensions_permission:
        if not user_can_download_extension(ext, current_user.username):
            flash('You do not have access to download this extension.', 'error')
            return redirect(url_for('extension_history', ext_id=ext_id))
        if not versions[ver_idx].get('downloadable', True):
            flash('This version is not available for download.', 'error')
            return redirect(url_for('extension_history', ext_id=ext_id))
    filename = versions[ver_idx].get('filename')
    if not filename:
        flash('File not found.', 'error')
        return redirect(url_for('extension_history', ext_id=ext_id))
    return send_from_directory('static/downloads', filename, as_attachment=True)

@app.route('/extension/<ext_id>/history')
@login_required
def extension_history(ext_id):
    if not (current_user.has_download_extension_permission or current_user.has_add_extensions_permission):
        flash('You do not have permission to access this service.', 'error')
        return redirect(url_for('services'))
    extensions = load_extensions()
    ext = next((e for e in extensions if e['id'] == ext_id), None)
    if not ext:
        flash('Extension not found.', 'error')
        return redirect(url_for('useful_extensions'))
    if 'versions' not in ext:
        ext['versions'] = []
    versions_status = get_versions_status(ext['name'])
    can_download_ext = user_can_download_extension(ext, current_user.username)
    return render_template('extension_history.html', ext=ext, versions_status=versions_status,
                            can_download_ext=can_download_ext,
                            can_see_ext_users=current_user.has_display_extensions_users_permission)

@app.route('/api/extensions/active-users')
@login_required
def get_extension_active_users():
    if not current_user.has_display_extensions_users_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    filepath = os.path.join('tssApi', 'extensions', 'active_users.json')
    try:
        mtime = os.path.getmtime(filepath)
        etag = f'"{int(mtime)}"'
    except OSError:
        etag = '"0"'
    if_none_match = request.headers.get('If-None-Match', '')
    if if_none_match == etag:
        return Response(status=304)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
    resp = jsonify({'success': True, 'data': add_entities_to_extension_active_users(data)})
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/extensions/<ext_id>', methods=['DELETE'])
@login_required
def delete_extension(ext_id):
    if not current_user.has_add_extensions_permission:
        return jsonify({'error': 'Unauthorized'}), 403
        
    extensions = load_extensions()
    updated_extensions = [e for e in extensions if e['id'] != ext_id]
    
    ext_to_delete = next((e for e in extensions if e['id'] == ext_id), None)
    if ext_to_delete:
        if ext_to_delete.get('type') == 'updated' and 'versions' in ext_to_delete:
            for ver in ext_to_delete['versions']:
                try:
                    os.remove(os.path.join('static/downloads', ver['filename']))
                except:
                    pass
        else:
            try:
                os.remove(os.path.join('static/downloads', ext_to_delete['filename']))
            except:
                pass
            
    save_extensions(updated_extensions)
    return jsonify({'success': True})

@app.route('/quality-helper')
@login_required
def quality_helper():
    if not current_user.has_quality_helper_permission:
        flash('You do not have permission to access Quality Seeds Helper.', 'error')
        return redirect(url_for('services'))
    return render_template('quality_helper.html')

@app.route('/api/quality-helper/status')
@login_required
def quality_helper_status():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_user_process_status, is_process_running, user_processes
    
    username = current_user.username
    
    running_status = user_processes.get(username, {})
    if running_status.get('running'):
        return jsonify({
            'has_process': True,
            'running': True,
            'progress': running_status.get('progress', 0),
            'total': running_status.get('total', 0),
            'status': running_status.get('status', 'Processing...'),
            'error': running_status.get('error')
        })
    
    saved_data = get_user_process_status(username)
    if saved_data:
        return jsonify({
            'has_process': True,
            'running': False,
            'data': saved_data
        })
    
    return jsonify({'has_process': False, 'running': False})

@app.route('/api/quality-helper/start', methods=['POST'])
@login_required
def quality_helper_start():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import is_process_running, get_user_process_status, run_image_generation
    import threading
    
    username = current_user.username
    
    if is_process_running(username):
        return jsonify({'error': 'A process is already running. Please wait for it to complete.'}), 400
    
    existing_process = get_user_process_status(username)
    if existing_process:
        return jsonify({'error': 'You have an existing process. Please delete it first before starting a new one.'}), 400
    
    data = request.get_json()
    keywords_text = data.get('keywords', '')
    image_count = data.get('image_count', 1)
    
    keywords = [k.strip() for k in keywords_text.strip().split('\n') if k.strip()]
    
    if not keywords:
        return jsonify({'error': 'Please enter at least one keyword.'}), 400
    
    try:
        image_count = int(image_count)
        if image_count < 1:
            image_count = 1
        if image_count > 50:
            image_count = 50
    except:
        image_count = 1
    
    if len(keywords) < image_count:
        return jsonify({'error': f'Not enough keywords ({len(keywords)}) for {image_count} images. Please add more keywords.'}), 400
    
    thread = threading.Thread(target=run_image_generation, args=(username, keywords, image_count))
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'Process started'})

@app.route('/api/quality-helper/delete', methods=['POST'])
@login_required
def quality_helper_delete():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import delete_user_process, is_process_running
    
    username = current_user.username
    
    if is_process_running(username):
        return jsonify({'error': 'Cannot delete while a process is running.'}), 400
    
    delete_user_process(username)
    return jsonify({'success': True, 'message': 'Process deleted successfully'})

@app.route('/api/quality-helper/image/<filename>')
@login_required
def quality_helper_image(filename):
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_user_images_dir
    import mimetypes
    
    username = current_user.username
    images_dir = get_user_images_dir(username)
    image_path = os.path.join(images_dir, filename)
    
    if not os.path.exists(image_path):
        return jsonify({'error': 'Image not found'}), 404
    
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = 'image/jpeg'
    
    with open(image_path, 'rb') as f:
        image_data = f.read()
    
    return Response(image_data, mimetype=mime_type)

@app.route('/api/quality-helper/download-zip')
@login_required
def quality_helper_download_zip():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_user_images_dir, get_user_process_status
    import zipfile
    import io
    
    username = current_user.username
    
    process_data = get_user_process_status(username)
    if not process_data:
        return jsonify({'error': 'No process data found'}), 404
    
    images_dir = get_user_images_dir(username)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for img in process_data.get('images', []):
            img_path = os.path.join(images_dir, img['filename'])
            if os.path.exists(img_path):
                zip_file.write(img_path, img['filename'])
    
    zip_buffer.seek(0)
    
    return Response(
        zip_buffer.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename=images_{username}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'}
    )


# ===================== PDF API ROUTES =====================

@app.route('/api/quality-helper/pdf/status')
@login_required
def quality_helper_pdf_status():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_pdf_user_process_status, is_pdf_process_running, pdf_user_processes
    
    username = current_user.username
    
    running_status = pdf_user_processes.get(username, {})
    if running_status.get('running'):
        return jsonify({
            'has_process': True,
            'running': True,
            'progress': running_status.get('progress', 0),
            'total': running_status.get('total', 0),
            'status': running_status.get('status', 'Processing...'),
            'error': running_status.get('error')
        })
    
    saved_data = get_pdf_user_process_status(username)
    if saved_data:
        return jsonify({
            'has_process': True,
            'running': False,
            'data': saved_data
        })
    
    return jsonify({'has_process': False, 'running': False})

@app.route('/api/quality-helper/pdf/start', methods=['POST'])
@login_required
def quality_helper_pdf_start():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import is_pdf_process_running, get_pdf_user_process_status, run_pdf_generation
    import threading
    
    username = current_user.username
    
    if is_pdf_process_running(username):
        return jsonify({'error': 'A PDF process is already running. Please wait for it to complete.'}), 400
    
    existing_process = get_pdf_user_process_status(username)
    if existing_process:
        return jsonify({'error': 'You have an existing PDF process. Please delete it first before starting a new one.'}), 400
    
    data = request.get_json()
    keywords_text = data.get('keywords', '')
    pdf_count = data.get('pdf_count', 1)
    
    keywords = [k.strip() for k in keywords_text.strip().split('\n') if k.strip()]
    
    if not keywords:
        return jsonify({'error': 'Please enter at least one keyword.'}), 400
    
    try:
        pdf_count = int(pdf_count)
        if pdf_count < 1:
            pdf_count = 1
        if pdf_count > 50:
            pdf_count = 50
    except:
        pdf_count = 1
    
    if len(keywords) < pdf_count:
        return jsonify({'error': f'Not enough keywords ({len(keywords)}) for {pdf_count} PDFs. Please add more keywords.'}), 400
    
    thread = threading.Thread(target=run_pdf_generation, args=(username, keywords, pdf_count))
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'PDF process started'})

@app.route('/api/quality-helper/pdf/delete', methods=['POST'])
@login_required
def quality_helper_pdf_delete():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import delete_pdf_user_process, is_pdf_process_running
    
    username = current_user.username
    
    if is_pdf_process_running(username):
        return jsonify({'error': 'Cannot delete while a PDF process is running.'}), 400
    
    delete_pdf_user_process(username)
    return jsonify({'success': True, 'message': 'PDF process deleted successfully'})

@app.route('/api/quality-helper/pdf/<filename>')
@login_required
def quality_helper_pdf(filename):
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_user_pdfs_dir
    
    username = current_user.username
    pdfs_dir = get_user_pdfs_dir(username)
    pdf_path = os.path.join(pdfs_dir, filename)
    
    if not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF not found'}), 404
    
    with open(pdf_path, 'rb') as f:
        pdf_data = f.read()
    
    return Response(pdf_data, mimetype='application/pdf')

@app.route('/api/quality-helper/pdf/download-zip')
@login_required
def quality_helper_pdf_download_zip():
    if not current_user.has_quality_helper_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from quality_helper import get_user_pdfs_dir, get_pdf_user_process_status
    import zipfile
    import io
    
    username = current_user.username
    
    process_data = get_pdf_user_process_status(username)
    if not process_data:
        return jsonify({'error': 'No PDF process data found'}), 404
    
    pdfs_dir = get_user_pdfs_dir(username)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for pdf in process_data.get('pdfs', []):
            pdf_path = os.path.join(pdfs_dir, pdf['filename'])
            if os.path.exists(pdf_path):
                zip_file.write(pdf_path, pdf['filename'])
    
    zip_buffer.seek(0)
    
    return Response(
        zip_buffer.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename=pdfs_{username}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'}
    )


# ===================== NEWS SUBSCRIPTION API ROUTES =====================

@app.route('/news-subscription')
@login_required
def news_subscription():
    if not current_user.has_news_subscription_permission:
        flash('You do not have permission to access News Subscription.', 'error')
        return redirect(url_for('services'))
    is_admin = current_user.has_user_management_permission
    return render_template('news_subscription.html', is_admin=is_admin)

@app.route('/api/news-subscription/status')
@login_required
def news_subscription_status():
    if not current_user.has_news_subscription_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from news_subscription import user_processes, load_process_state, run_subscription_process, is_infinity_process
    
    username = current_user.username
    infinity = is_infinity_process(username)
    
    # Auto-resume on refresh if state exists but not in memory
    import os
    user_data_dir = "news_subscription_data"
    user_dir = os.path.join(user_data_dir, username)
    if os.path.exists(user_dir):
        for f in os.listdir(user_dir):
            if f.startswith("state_") and f.endswith(".json"):
                process_id = f[6:-5]
                pid = f"{username}:{process_id}"
                if pid not in user_processes:
                    state = load_process_state(username, process_id)
                    # Check if the process was actually running or paused before resuming
                    if state and (state.get('running', True) or state.get('paused')):
                        run_subscription_process(username, state['email'], state['domains'], process_id, state)

    active_processes = []
    for pid, p in user_processes.items():
        if pid.startswith(f"{username}:") and (p.get('running') or p.get('paused')):
            active_processes.append({
                'id': p.get('id', 'default'),
                'running': p.get('running'),
                'paused': p.get('paused'),
                'progress': p.get('progress', 0),
                'total': p.get('total', 0),
                'status': p.get('status', 'Processing...'),
                'successful': p.get('successful', 0),
                'failed': p.get('failed', 0),
                'current_domains': p.get('current_domains', [])
            })
    
    quotas = load_user_quotas()
    user_quota = quotas.get(username, {})
    domain_quota = user_quota.get('domain_quota', 0)
    domains_processed = user_quota.get('domains_processed_this_month', 0)
    remaining = get_user_remaining_domains(username)
    max_procs = user_quota.get('max_processes', 1)
    
    return jsonify({
        'processes': active_processes,
        'infinity': infinity,
        'max_processes': max_procs,
        'domain_quota': domain_quota,
        'domains_processed': domains_processed,
        'domains_remaining': remaining
    })

@app.route('/api/news-subscription/start', methods=['POST'])
@login_required
def news_subscription_start():
    if not current_user.has_news_subscription_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from news_subscription import run_subscription_process, is_infinity_process, user_processes
    
    username = current_user.username
    infinity = is_infinity_process(username)
    
    active = [pid for pid, p in user_processes.items() if pid.startswith(f"{username}:") and p.get('running')]
    
    quotas = load_user_quotas()
    user_quota = quotas.get(username, {})
    max_procs = user_quota.get('max_processes', 1)
    
    if not infinity:
        if len(active) >= max_procs:
            return jsonify({'error': f'Process limit reached ({max_procs}). Stop a running process first.'}), 400
    
    data = request.get_json()
    email = data.get('email', '').strip()
    domains_text = data.get('domains', '')
    process_id = str(int(time.time())) if (infinity or max_procs > 1) else "default"
    
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email.'}), 400
    
    domains = [d.strip() for d in domains_text.strip().split('\n') if d.strip()]
    if not domains:
        return jsonify({'error': 'No domains.'}), 400
    
    domain_quota = user_quota.get('domain_quota', 0)
    if domain_quota > 0:
        remaining = get_user_remaining_domains(username)
        if remaining == 0:
            return jsonify({'error': 'Monthly domain quota exhausted. Contact your administrator.'}), 400
        if len(domains) > remaining:
            return jsonify({'error': f'You can only process {remaining} more domains this month. You submitted {len(domains)}.'}), 400
        update_user_domains_processed(username, len(domains))
    
    run_subscription_process(username, email, domains, process_id)
    return jsonify({'success': True, 'process_id': process_id})

@app.route('/api/news-subscription/pause', methods=['POST'])
@login_required
def news_subscription_pause():
    from news_subscription import pause_user_process
    data = request.get_json()
    pause_user_process(current_user.username, data.get('process_id', 'default'))
    return jsonify({'success': True})

@app.route('/api/news-subscription/resume', methods=['POST'])
@login_required
def news_subscription_resume():
    from news_subscription import resume_user_process
    data = request.get_json()
    resume_user_process(current_user.username, data.get('process_id', 'default'))
    return jsonify({'success': True})

@app.route('/api/news-subscription/history')
@login_required
def news_subscription_history():
    if not current_user.has_news_subscription_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from news_subscription import get_user_process_history
    username = current_user.username
    history = get_user_process_history(username)
    return jsonify({'history': history})

@app.route('/api/news-subscription/stop', methods=['POST'])
@login_required
def news_subscription_stop():
    from news_subscription import stop_user_process
    data = request.get_json()
    username = current_user.username
    process_id = data.get('process_id', 'default')
    stop_user_process(username, process_id)
    return jsonify({'success': True})

@app.route('/api/news-subscription/delete', methods=['POST'])
@login_required
def news_subscription_delete():
    if not current_user.has_news_subscription_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    from news_subscription import delete_process_from_history
    
    username = current_user.username
    data = request.get_json()
    process_id = data.get('process_id', '')
    
    if not process_id:
        return jsonify({'error': 'Process ID is required.'}), 400
    
    delete_process_from_history(username, process_id)
    return jsonify({'success': True, 'message': 'Process deleted successfully'})

@app.route('/api/news-subscription/successful-domains')
@login_required
def news_subscription_successful_domains():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    
    domains = []
    try:
        if os.path.exists('all_successfully_domain.txt'):
            with open('all_successfully_domain.txt', 'r', encoding='utf-8') as f:
                domains = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logging.error(f"Error reading successful domains: {e}")
        return jsonify({'error': 'Failed to read domains file.'}), 500
    
    return jsonify({'domains': domains, 'total': len(domains)})



@app.route('/api/ip-checker/ips/generate', methods=['POST'])
@login_required
def api_generate_ips():
    if not current_user.has_ips_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.json
    servers = data.get('servers', [])
    if not servers and data.get('server'):
        servers = [data.get('server')]
    
    success, msg, output = ip_checker.generate_random_ips(
        servers,
        data.get('cidrs', []),
        data.get('from', 1),
        data.get('to', 100),
        data.get('filter', 'all')
    )
    return jsonify({'success': success, 'message': msg, 'output': output})


@app.route('/ip-checker')
@login_required
def ip_checker_page():
    if not current_user.has_ips_cheker_permission:
        flash('You do not have permission to access IPs Checker.', 'error')
        return redirect(url_for('services'))
    can_manage = current_user.has_add_ip_cheker_permission
    return render_template('ip_checker.html', can_manage=can_manage)


@app.route('/api/ip-checker/servers')
@login_required
def ip_checker_servers():
    if not current_user.has_ips_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = ip_checker.load_servers()
    servers = {}
    for sname, sdata in data.items():
        classes = {}
        for cidr, cdata in sdata['classes'].items():
            class_status = ip_checker.get_latest_status(sname, cidr)
            
            # Efficiently fetch statuses for all IPs in this class
            ip_statuses = {}
            for ip in cdata['ips']:
                ip_status = ip_checker.get_latest_status(sname, cidr, ip)
                if ip_status:
                    ip_statuses[ip] = ip_status
                    
            classes[cidr] = {
                'ips': cdata['ips'],
                'ip_statuses': ip_statuses,
                'ip_count': len(cdata['ips']),
                'created_at': cdata.get('created_at', ''),
                'status': class_status
            }
        server_status = ip_checker.get_latest_status(sname)
        servers[sname] = {
            'classes': classes,
            'created_at': sdata.get('created_at', ''),
            'status': server_status
        }
    return jsonify({'servers': servers})


@app.route('/api/ip-checker/server/add', methods=['POST'])
@login_required
def ip_checker_add_server():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    name = data.get('name', '').strip()
    cidr = data.get('cidr', '').strip()
    ips_text = data.get('ips', '').strip()
    if not name or not cidr:
        return jsonify({'error': 'Server name and class (CIDR) are required.'}), 400
    ip_list = [ip.strip() for ip in ips_text.split('\n') if ip.strip()] if ips_text else []
    ok, msg, results = ip_checker.add_server_with_class(name, cidr, ip_list)
    return jsonify({'success': ok, 'message': msg, 'results': results}), 200 if ok else 400


@app.route('/api/ip-checker/server/delete', methods=['POST'])
@login_required
def ip_checker_delete_server():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Server name is required.'}), 400
    ok, msg = ip_checker.delete_server(name)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/server/rename', methods=['POST'])
@login_required
def ip_checker_rename_server():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    old_name = data.get('old_name', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_name or not new_name:
        return jsonify({'error': 'Both old and new names are required.'}), 400
    ok, msg = ip_checker.rename_server(old_name, new_name)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/class/add', methods=['POST'])
@login_required
def ip_checker_add_class():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    server = data.get('server', '').strip()
    cidr = data.get('cidr', '').strip()
    if not server or not cidr:
        return jsonify({'error': 'Server and CIDR are required.'}), 400
    ok, msg = ip_checker.add_class_to_server(server, cidr)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/class/delete', methods=['POST'])
@login_required
def ip_checker_delete_class():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    server = data.get('server', '').strip()
    cidr = data.get('cidr', '').strip()
    if not server or not cidr:
        return jsonify({'error': 'Server and CIDR are required.'}), 400
    ok, msg = ip_checker.delete_class_from_server(server, cidr)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/ips/add', methods=['POST'])
@login_required
def ip_checker_add_ips():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    server = data.get('server', '').strip()
    cidr = data.get('cidr', '').strip()
    ips_text = data.get('ips', '').strip()
    if not server or not cidr or not ips_text:
        return jsonify({'error': 'Server, class, and IPs are required.'}), 400
    ip_list = [ip.strip() for ip in ips_text.split('\n') if ip.strip()]
    if not ip_list:
        return jsonify({'error': 'No valid IPs provided.'}), 400
    ok, msg, results = ip_checker.add_ips_to_class(server, cidr, ip_list)
    return jsonify({'success': ok, 'message': msg, 'results': results})


@app.route('/api/ip-checker/ip/delete', methods=['POST'])
@login_required
def ip_checker_delete_ip():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    server = data.get('server', '').strip()
    cidr = data.get('cidr', '').strip()
    ip_str = data.get('ip', '').strip()
    if not server or not cidr or not ip_str:
        return jsonify({'error': 'Server, class, and IP are required.'}), 400
    ok, msg = ip_checker.delete_ip_from_class(server, cidr, ip_str)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/search', methods=['POST'])
@login_required
def ip_checker_search():
    if not current_user.has_ips_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    ip_str = data.get('ip', '').strip()
    if not ip_str:
        return jsonify({'error': 'IP address is required.'}), 400
    ok, msg, results = ip_checker.search_ip(ip_str)
    return jsonify({'success': ok, 'message': msg, 'results': results})


@app.route('/api/ip-checker/event/add', methods=['POST'])
@login_required
def ip_checker_add_event():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    server = data.get('server', '').strip()
    event_type = data.get('event_type', '').strip()
    scope = data.get('scope', '').strip()
    cidr = (data.get('cidr') or '').strip() or None
    ips = data.get('ips', [])
    if not server or not event_type or not scope:
        return jsonify({'error': 'Server, event type, and scope are required.'}), 400
    if scope not in ('server', 'class', 'ip'):
        return jsonify({'error': 'Invalid scope.'}), 400
    if scope == 'class' and not cidr:
        return jsonify({'error': 'Class (CIDR) is required for class scope.'}), 400
    if scope == 'ip' and not ips:
        return jsonify({'error': 'At least one IP is required for IP scope.'}), 400
    ok, msg, event = ip_checker.add_event(server, event_type, scope, cidr, ips, current_user.name)
    return jsonify({'success': ok, 'message': msg, 'event': event})


@app.route('/api/ip-checker/event/delete', methods=['POST'])
@login_required
def ip_checker_delete_event():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    event_id = data.get('event_id', '').strip()
    if not event_id:
        return jsonify({'error': 'Event ID is required.'}), 400
    ok, msg = ip_checker.delete_event(event_id)
    return jsonify({'success': ok, 'message': msg})


@app.route('/api/ip-checker/event-types')
@login_required
def ip_checker_event_types():
    if not current_user.has_ips_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    types = ip_checker.load_event_types()
    return jsonify({'event_types': types})


@app.route('/api/ip-checker/event-types/add', methods=['POST'])
@login_required
def ip_checker_add_event_type():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Event type name is required.'}), 400
    ok, msg = ip_checker.add_custom_event_type(name)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/event-types/delete', methods=['POST'])
@login_required
def ip_checker_delete_event_type():
    if not current_user.has_add_ip_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Event type name is required.'}), 400
    ok, msg = ip_checker.delete_event_type(name)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400


@app.route('/api/ip-checker/events')
@login_required
def ip_checker_get_events():
    if not current_user.has_ips_cheker_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    server = request.args.get('server', '').strip()
    events = ip_checker.load_events()
    if server:
        events = [e for e in events if e.get('server') == server]
    return jsonify({'events': events[:100]})


ACCESS_MANAGEMENT_FILE = 'access_management.json'

SESSIONS_CONTROL_FILE = 'sessions_control.json'

def load_sessions_control():
    try:
        if os.path.exists(SESSIONS_CONTROL_FILE):
            with open(SESSIONS_CONTROL_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading sessions control: {e}")
    return {'force_all_relogin_at': 0, 'invalidated_users': {}}

def save_sessions_control(data):
    try:
        with open(SESSIONS_CONTROL_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving sessions control: {e}")

def get_client_ip():
    for header in ('X-Real-IP', 'X-Forwarded-For', 'CF-Connecting-IP', 'True-Client-IP'):
        value = request.headers.get(header)
        if value:
            return value.split(',')[0].strip()
    return request.remote_addr or ''

def load_access_management():
    try:
        if os.path.exists(ACCESS_MANAGEMENT_FILE):
            with open(ACCESS_MANAGEMENT_FILE, 'r') as f:
                data = json.load(f)
                return data
    except Exception as e:
        logging.error(f"Error loading access management: {e}")
    return {'allowed_ips': [], 'exempt_users': []}

def save_access_management(data):
    try:
        with open(ACCESS_MANAGEMENT_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving access management: {e}")

ALL_PERMISSIONS = [
    ('ok', 'Toggle'),
    ('allow_add_gmail_of_news', 'Add Gmail of News'),
    ('Domain_checker', 'Domain Checker'),
    ('find_news', 'Find News'),
    ('Extract_emails', 'Extract Emails'),
    ('tssw_report', 'TSSW Report'),
    ('gmass', 'TSS Gmail Access'),
    ('blacklist_lookup', 'Blacklist Lookup'),
    ('download_extension', 'Download Extensions'),
    ('add_extensions', 'Add Extensions'),
    ('quality_helper', 'Quality Seeds Helper'),
    ('news_sign_subsctiption', 'News Subscription'),
    ('infinity-process', 'Unlimited Processes'),
    ('user_management', 'User Management'),
    ('ips_cheker', 'IPs Checker'),
    ('add_ip_cheker', 'Add/Manage IPs'),
    ('domain_founder', 'Domain Founder'),
    ('unlimited_domain_founder', 'Unlimited Domain Founder'),
    ('email_founder', 'Email Founder'),
    ('access_management', 'Access Management'),
    ('subdomain_finder', 'SubDomain Finder'),
    ('processes_management', 'Processes Management'),
    ('warmup_lists', 'Warmup Lists'),
    ('warmup_lists_admin', 'Warmup Lists Admin'),
    ('Warmup_History', 'Warmup History'),
    ('add_warmup_record', 'Add New Warmup Record'),
    ('warmup_reports', 'Warmup Reports'),
    ('warmup_sessions', 'Warmup Sessions'),
    ('display_extensions_users', 'Display Extensions Users'),
]

@app.route('/manage-users')
@login_required
def manage_users():
    if not current_user.has_user_management_permission:
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('services'))
    users = load_users_from_file()
    quotas = load_user_quotas()
    return render_template('manage_users.html', users=users, quotas=quotas, all_permissions=ALL_PERMISSIONS)

@app.route('/api/manage-users/list')
@login_required
def manage_users_list():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    users = load_users_from_file()
    quotas = load_user_quotas()
    users_data = []
    for u in users:
        uq = quotas.get(u['username'], {})
        users_data.append({
            'entity': u['entity'],
            'name': u['name'],
            'username': u['username'],
            'permissions': u['permissions'],
            'max_processes': uq.get('max_processes', 1),
            'email_founder_max_processes': uq.get('email_founder_max_processes', 10),
            'domain_quota': uq.get('domain_quota', 0),
            'domains_processed_this_month': uq.get('domains_processed_this_month', 0),
            'sf_max_processes': uq.get('sf_max_processes', 1),
            'sf_max_domains': uq.get('sf_max_domains', 0),
            'sf_stop_at': uq.get('sf_stop_at', 0),
            'df_max_processes': uq.get('df_max_processes', 1),
        })
    return jsonify({'users': users_data})

@app.route('/api/manage-users/add', methods=['POST'])
@login_required
def manage_users_add():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    entity = data.get('entity', '').strip()
    name = data.get('name', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    permissions = data.get('permissions', [])
    max_processes = int(data.get('max_processes', 1))
    email_founder_max_processes = int(data.get('email_founder_max_processes', 10))
    domain_quota = int(data.get('domain_quota', 0))
    sf_max_processes = int(data.get('sf_max_processes', 1))
    sf_max_domains = int(data.get('sf_max_domains', 0))
    sf_stop_at = int(data.get('sf_stop_at', 0))
    df_max_processes = int(data.get('df_max_processes', 1))

    if not entity or not name or not username or not password:
        return jsonify({'error': 'Entity, Name, Username, and Password are required.'}), 400

    for field_name, field_val in [('Entity', entity), ('Name', name), ('Username', username), ('Password', password)]:
        if ',' in field_val:
            return jsonify({'error': f'{field_name} must not contain commas.'}), 400

    users = load_users_from_file()
    for u in users:
        if u['username'] == username:
            return jsonify({'error': f'Username "{username}" already exists.'}), 400

    with open('users.txt', 'a', encoding='utf-8') as f:
        parts = [entity, name, username, password] + permissions
        f.write(','.join(parts) + '\n')

    quotas = load_user_quotas()
    quotas[username] = {
        'max_processes': max_processes,
        'email_founder_max_processes': email_founder_max_processes,
        'domain_quota': domain_quota,
        'domains_processed_this_month': 0,
        'quota_month': datetime.now().strftime('%Y-%m'),
        'sf_max_processes': sf_max_processes,
        'sf_max_domains': sf_max_domains,
        'sf_stop_at': sf_stop_at,
        'df_max_processes': df_max_processes,
    }
    save_user_quotas(quotas)
    return jsonify({'success': True, 'message': f'User "{name}" added successfully.'})

@app.route('/api/manage-users/update', methods=['POST'])
@login_required
def manage_users_update():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    target_username = data.get('target_username', '').strip()
    entity = data.get('entity', '').strip()
    name = data.get('name', '').strip()
    new_username = data.get('new_username', '').strip()
    password = data.get('password', '').strip()
    permissions = data.get('permissions', [])
    max_processes = int(data.get('max_processes', 1))
    email_founder_max_processes = int(data.get('email_founder_max_processes', 10))
    domain_quota = int(data.get('domain_quota', 0))
    sf_max_processes = int(data.get('sf_max_processes', 1))
    sf_max_domains = int(data.get('sf_max_domains', 0))
    sf_stop_at = int(data.get('sf_stop_at', 0))
    df_max_processes = int(data.get('df_max_processes', 1))

    if not entity or not name or not new_username:
        return jsonify({'error': 'Entity, Name, and Username are required.'}), 400

    for field_name, field_val in [('Entity', entity), ('Name', name), ('Username', new_username)]:
        if ',' in field_val:
            return jsonify({'error': f'{field_name} must not contain commas.'}), 400
    if password and ',' in password:
        return jsonify({'error': 'Password must not contain commas.'}), 400

    users = load_users_from_file()
    found = False
    existing_password = ''
    for u in users:
        if u['username'] == target_username:
            existing_password = u['password']
            if new_username != target_username:
                for other in users:
                    if other['username'] == new_username and other['username'] != target_username:
                        return jsonify({'error': f'Username "{new_username}" already exists.'}), 400
            u['entity'] = entity
            u['name'] = name
            u['username'] = new_username
            u['password'] = password if password else existing_password
            u['permissions'] = permissions
            found = True
            break
    if not found:
        return jsonify({'error': 'User not found.'}), 404

    save_users_to_file(users)

    quotas = load_user_quotas()
    old_quota = quotas.pop(target_username, {})
    quotas[new_username] = {
        'max_processes': max_processes,
        'email_founder_max_processes': email_founder_max_processes,
        'domain_quota': domain_quota,
        'domains_processed_this_month': old_quota.get('domains_processed_this_month', 0),
        'quota_month': old_quota.get('quota_month', datetime.now().strftime('%Y-%m')),
        'sf_max_processes': sf_max_processes,
        'sf_max_domains': sf_max_domains,
        'sf_stop_at': sf_stop_at,
        'df_max_processes': df_max_processes,
    }
    save_user_quotas(quotas)
    return jsonify({'success': True, 'message': f'User "{name}" updated successfully.'})

@app.route('/api/manage-users/delete', methods=['POST'])
@login_required
def manage_users_delete():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    target_username = data.get('username', '').strip()

    if not target_username:
        return jsonify({'error': 'Username is required.'}), 400
    if target_username == current_user.username:
        return jsonify({'error': 'You cannot delete yourself.'}), 400

    users = load_users_from_file()
    new_users = [u for u in users if u['username'] != target_username]
    if len(new_users) == len(users):
        return jsonify({'error': 'User not found.'}), 404
    
    save_users_to_file(new_users)

    quotas = load_user_quotas()
    quotas.pop(target_username, None)
    save_user_quotas(quotas)
    return jsonify({'success': True, 'message': f'User "{target_username}" deleted.'})

@app.route('/api/manage-users/reset-quota', methods=['POST'])
@login_required
def manage_users_reset_quota():
    if not current_user.has_user_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    target_username = data.get('username', '').strip()
    if not target_username:
        return jsonify({'error': 'Username is required.'}), 400
    quotas = load_user_quotas()
    if target_username in quotas:
        quotas[target_username]['domains_processed_this_month'] = 0
        quotas[target_username]['quota_month'] = datetime.now().strftime('%Y-%m')
        save_user_quotas(quotas)
    return jsonify({'success': True, 'message': f'Quota reset for "{target_username}".'})

@app.route('/access-management')
@login_required
def access_management():
    if not current_user.has_access_management_permission:
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('services'))
    am = load_access_management()
    users = load_users_from_file()
    usernames = [u['username'] for u in users]
    return render_template('access_management.html', am=am, usernames=usernames)

@app.route('/api/access-management/my-ip')
@login_required
def access_management_my_ip():
    return jsonify({'ip': get_client_ip()})

@app.route('/api/access-management/add-ip', methods=['POST'])
@login_required
def access_management_add_ip():
    if not current_user.has_access_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    ip = data.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'IP address is required'}), 400
    am = load_access_management()
    if ip in am['allowed_ips']:
        return jsonify({'error': 'IP already exists'}), 400
    am['allowed_ips'].append(ip)
    save_access_management(am)
    return jsonify({'success': True})

@app.route('/api/access-management/remove-ip', methods=['POST'])
@login_required
def access_management_remove_ip():
    if not current_user.has_access_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    ip = data.get('ip', '').strip()
    am = load_access_management()
    if ip not in am['allowed_ips']:
        return jsonify({'error': 'IP not found'}), 404
    am['allowed_ips'].remove(ip)
    save_access_management(am)
    return jsonify({'success': True})

@app.route('/api/access-management/add-exempt', methods=['POST'])
@login_required
def access_management_add_exempt():
    if not current_user.has_access_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'error': 'Username is required'}), 400
    am = load_access_management()
    if username in am['exempt_users']:
        return jsonify({'error': 'User already exempt'}), 400
    am['exempt_users'].append(username)
    save_access_management(am)
    return jsonify({'success': True})

@app.route('/api/access-management/remove-exempt', methods=['POST'])
@login_required
def access_management_remove_exempt():
    if not current_user.has_access_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    username = data.get('username', '').strip()
    am = load_access_management()
    if username not in am['exempt_users']:
        return jsonify({'error': 'User not found in exempt list'}), 404
    am['exempt_users'].remove(username)
    save_access_management(am)
    sc = load_sessions_control()
    sc['invalidated_users'][username] = int(time.time())
    save_sessions_control(sc)
    return jsonify({'success': True})

@app.route('/api/access-management/end-all-sessions', methods=['POST'])
@login_required
def access_management_end_all_sessions():
    if not current_user.has_access_management_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    sc = load_sessions_control()
    sc['force_all_relogin_at'] = int(time.time())
    save_sessions_control(sc)
    return jsonify({'success': True})

@app.route('/domain-founder')
@login_required
def domain_founder_page():
    if not current_user.has_domain_founder_permission:
        flash('You do not have permission to access the Domain Founder service.', 'error')
        return redirect(url_for('services'))
    return render_template('domain_founder.html')

@app.route('/api/domain-founder/domains', methods=['GET'])
@login_required
def domain_founder_get_domains():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    domains = domain_founder.load_user_domains(current_user.username)
    return jsonify({'domains': domains})

@app.route('/api/domain-founder/domains', methods=['POST'])
@login_required
def domain_founder_save_domains():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    domains = data.get('domains', [])
    domain_founder.save_user_domains(current_user.username, domains)
    return jsonify({'success': True, 'count': len(domains)})

@app.route('/api/domain-founder/start', methods=['POST'])
@login_required
def domain_founder_start():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    process_type = data.get('type', '')
    params = data.get('params', {})
    domains = domain_founder.load_user_domains(current_user.username)
    has_unlimited = current_user.has_unlimited_domain_founder_permission
    key, error = domain_founder.start_process(current_user.username, process_type, domains, has_unlimited, params, current_user.df_max_processes)
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'success': True, 'key': key})

@app.route('/api/domain-founder/processes', methods=['GET'])
@login_required
def domain_founder_processes():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    processes = domain_founder.get_user_processes(current_user.username)
    return jsonify({'processes': processes})

@app.route('/api/domain-founder/stop', methods=['POST'])
@login_required
def domain_founder_stop():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    key = data.get('key', '')
    if domain_founder.stop_process(current_user.username, key):
        return jsonify({'success': True})
    return jsonify({'error': 'Process not found'}), 404

@app.route('/api/domain-founder/delete', methods=['POST'])
@login_required
def domain_founder_delete():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    key = data.get('key', '')
    if domain_founder.delete_process(current_user.username, key):
        return jsonify({'success': True})
    return jsonify({'error': 'Process not found'}), 404

@app.route('/api/domain-founder/resume', methods=['POST'])
@login_required
def domain_founder_resume():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    key = data.get('key', '')
    has_unlimited = current_user.has_unlimited_domain_founder_permission
    ok, err = domain_founder.resume_process(current_user.username, key, has_unlimited)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

@app.route('/api/domain-founder/check-availability', methods=['POST'])
@login_required
def domain_founder_check_availability():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json() or {}
    key = data.get('key', '')
    ok, err = domain_founder.start_availability_check(current_user.username, key)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

@app.route('/api/domain-founder/download-available')
@login_required
def domain_founder_download_available():
    if not current_user.has_domain_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    key = request.args.get('key', '')
    csv_text, info = domain_founder.get_availability_csv(current_user.username, key)
    if csv_text is None:
        return jsonify({'error': info}), 400
    return Response(
        csv_text,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{info}"'}
    )

@app.route('/email-founder')
@login_required
def email_founder_page():
    if not current_user.has_email_founder_permission:
        flash('You do not have permission to access the Email Founder service.', 'error')
        return redirect(url_for('services'))
    ef_max = current_user.email_founder_max_processes
    ef_used = len(email_founder.load_process_history(current_user.username))
    return render_template('email_founder.html', ef_max=ef_max, ef_used=ef_used)

@app.route('/api/email-founder/accounts', methods=['GET'])
@login_required
def email_founder_get_accounts():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    accounts = email_founder.load_user_accounts(current_user.username)
    safe_accounts = [{'email': a['email']} for a in accounts]
    return jsonify({'accounts': safe_accounts})

@app.route('/api/email-founder/accounts', methods=['POST'])
@login_required
def email_founder_save_account():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    gmail_email = data.get('email', '').strip()
    app_password = data.get('password', '').strip()
    if not gmail_email or not app_password:
        return jsonify({'error': 'Email and password are required'}), 400
    email_founder.save_user_account(current_user.username, gmail_email, app_password)
    return jsonify({'success': True})

@app.route('/api/email-founder/accounts', methods=['DELETE'])
@login_required
def email_founder_delete_account():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    gmail_email = data.get('email', '').strip()
    if not gmail_email:
        return jsonify({'error': 'Email is required'}), 400
    email_founder.delete_user_account(current_user.username, gmail_email)
    return jsonify({'success': True})

@app.route('/api/email-founder/labels', methods=['POST'])
@login_required
def email_founder_get_labels():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    gmail_email = data.get('email', '').strip()
    if not gmail_email:
        return jsonify({'error': 'Email is required'}), 400
    accounts = email_founder.load_user_accounts(current_user.username)
    account = next((a for a in accounts if a['email'] == gmail_email), None)
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    labels, error = email_founder.list_labels(gmail_email, account['password'])
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'labels': labels})

@app.route('/api/email-founder/start', methods=['POST'])
@login_required
def email_founder_start():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    gmail_email = data.get('email', '').strip()
    folder = data.get('folder', 'inbox')
    custom_label = data.get('custom_label')
    range_from = int(data.get('range_from', 1))
    range_to = int(data.get('range_to', 10))
    mode = data.get('mode', '1')

    accounts = email_founder.load_user_accounts(current_user.username)
    account = next((a for a in accounts if a['email'] == gmail_email), None)
    if not account:
        return jsonify({'error': 'Account not found. Please add it first.'}), 404

    ef_max = current_user.email_founder_max_processes
    key, error = email_founder.start_extraction(
        current_user.username, gmail_email, account['password'],
        folder, custom_label, range_from, range_to, mode, max_history=ef_max
    )
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'success': True, 'key': key})

@app.route('/api/email-founder/processes', methods=['GET'])
@login_required
def email_founder_processes():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    processes = email_founder.get_user_processes(current_user.username)
    return jsonify({'processes': processes})

@app.route('/api/email-founder/history', methods=['GET'])
@login_required
def email_founder_history():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    history = email_founder.load_process_history(current_user.username)
    return jsonify({'history': history})

@app.route('/api/email-founder/stop', methods=['POST'])
@login_required
def email_founder_stop():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    key = data.get('key', '')
    if email_founder.stop_process(current_user.username, key):
        return jsonify({'success': True})
    return jsonify({'error': 'Process not found'}), 404

@app.route('/api/email-founder/delete', methods=['POST'])
@login_required
def email_founder_delete():
    if not current_user.has_email_founder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    key = data.get('key', '')
    email_founder.delete_history_entry(current_user.username, key)
    if key in email_founder.user_processes:
        email_founder.delete_process(current_user.username, key)
    return jsonify({'success': True})

@app.route('/api/email-founder/download/<filename>')
@login_required
def email_founder_download(filename):
    if not current_user.has_email_founder_permission:
        flash('Permission denied.', 'error')
        return redirect(url_for('services'))
    filepath = email_founder.get_download_path(current_user.username, filename)
    if not filepath:
        flash('File not found.', 'error')
        return redirect(url_for('email_founder_page'))
    from flask import send_file
    return send_file(filepath, as_attachment=True, download_name=filename)


# ── SubDomain Finder Routes ──────────────────────────────────────────────────

@app.route('/subdomain-finder')
@login_required
def subdomain_finder_page():
    if not current_user.has_subdomain_finder_permission:
        flash('You do not have permission to access the SubDomain Finder service.', 'error')
        return redirect(url_for('services'))
    return render_template(
        'subdomain_finder.html',
        sf_max_processes=current_user.sf_max_processes,
        sf_max_domains=current_user.sf_max_domains,
        sf_stop_at=current_user.sf_stop_at,
    )

@app.route('/api/subdomain-finder/domains', methods=['GET'])
@login_required
def sf_get_domains():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    domains = subdomain_finder.load_user_domains(current_user.username)
    return jsonify({'domains': domains})

@app.route('/api/subdomain-finder/domains', methods=['POST'])
@login_required
def sf_save_domains():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    domains = data.get('domains', [])
    subdomain_finder.save_user_domains(current_user.username, domains)
    return jsonify({'success': True, 'count': len(domains)})

@app.route('/api/subdomain-finder/start', methods=['POST'])
@login_required
def sf_start():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json()
    process_name = data.get('name', '').strip()
    domains = data.get('domains', [])
    if not isinstance(domains, list):
        return jsonify({'error': 'Invalid domains format.'}), 400
    domains = [d.strip() for d in domains if str(d).strip()]
    key, error = subdomain_finder.start_process(
        username=current_user.username,
        process_name=process_name,
        domains=domains,
        max_allowed=current_user.sf_max_processes,
        max_domains_per_process=current_user.sf_max_domains,
        stop_at=current_user.sf_stop_at,
    )
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'success': True, 'key': key})

@app.route('/api/subdomain-finder/processes', methods=['GET'])
@login_required
def sf_processes():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    processes = subdomain_finder.get_user_processes(current_user.username)
    return jsonify({'processes': processes})

@app.route('/api/subdomain-finder/pause', methods=['POST'])
@login_required
def sf_pause():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    key = request.get_json().get('key', '')
    ok, err = subdomain_finder.pause_process(current_user.username, key)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

@app.route('/api/subdomain-finder/resume', methods=['POST'])
@login_required
def sf_resume():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    key = request.get_json().get('key', '')
    ok, err = subdomain_finder.resume_process_with_limit(current_user.username, key, current_user.sf_max_processes)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

@app.route('/api/subdomain-finder/stop', methods=['POST'])
@login_required
def sf_stop():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    key = request.get_json().get('key', '')
    ok, err = subdomain_finder.stop_process(current_user.username, key)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

@app.route('/api/subdomain-finder/delete', methods=['POST'])
@login_required
def sf_delete():
    if not current_user.has_subdomain_finder_permission:
        return jsonify({'error': 'Permission denied'}), 403
    key = request.get_json().get('key', '')
    ok, err = subdomain_finder.delete_process(current_user.username, key)
    if not ok:
        return jsonify({'error': err}), 400
    return jsonify({'success': True})

# ── Processes Management (admin) ─────────────────────────────────────────────

@app.route('/processes-management')
@login_required
def processes_management_page():
    if not current_user.has_processes_management_permission:
        flash('You do not have permission to access Processes Management.', 'error')
        return redirect(url_for('services'))
    return render_template('processes_management.html')


@app.route('/api/processes-management/all')
@login_required
def processes_management_all():
    if not current_user.has_processes_management_permission:
        return jsonify({'error': 'Permission denied'}), 403

    try:
        users_list = load_users_from_file()
        name_map = {u.get('username', ''): (u.get('name') or u.get('username', '')) for u in users_list}
    except Exception:
        name_map = {}

    def _full(uname):
        return name_map.get(uname or '', uname or 'unknown')

    df_items = []
    for p in domain_founder.get_all_processes_admin():
        uname = p.get('username', '')
        df_items.append({
            'service': 'domain_founder',
            'service_label': 'Domain Founder',
            'id': p.get('key'),
            'username': uname,
            'user_full_name': _full(uname),
            'name': p.get('type', '') or 'Process',
            'status': p.get('status', ''),
            'progress': p.get('progress', 0),
            'total': p.get('total', 0),
            'started_at': p.get('started_at', ''),
            'finished_at': p.get('finished_at', ''),
            'deleted': bool(p.get('deleted', False)),
            'deleted_at': p.get('deleted_at', ''),
            'extra': {
                'type': p.get('type', ''),
                'params': p.get('params', {}),
                'results_count': len(p.get('results', []) or []),
            },
        })

    sf_items = []
    for p in subdomain_finder.get_all_processes_admin():
        results = p.get('results', {}) or {}
        sub_count = sum(len(v) for v in results.values()) if isinstance(results, dict) else 0
        uname = p.get('username', '')
        sf_items.append({
            'service': 'subdomain_finder',
            'service_label': 'SubDomain Finder',
            'id': p.get('key'),
            'username': uname,
            'user_full_name': _full(uname),
            'name': p.get('name') or 'Process',
            'status': p.get('status', ''),
            'progress': p.get('progress', 0),
            'total': p.get('total', 0),
            'started_at': p.get('started_at', ''),
            'finished_at': p.get('finished_at', ''),
            'deleted': bool(p.get('deleted', False)),
            'deleted_at': p.get('deleted_at', ''),
            'extra': {
                'subdomains_found': sub_count,
                'domains_count': len(p.get('domains') or []),
            },
        })

    ns_items = []
    from news_subscription import get_all_processes_admin as ns_get_all
    for p in ns_get_all():
        uname = p.get('username', '')
        ns_items.append({
            'service': 'news_subscription',
            'service_label': 'News Subscription',
            'id': p.get('id'),
            'username': uname,
            'user_full_name': _full(uname),
            'name': p.get('email_used') or 'Subscription Run',
            'status': p.get('status', ''),
            'progress': p.get('progress', p.get('total_domains_processed', 0)),
            'total': p.get('total', p.get('total_domains_processed', 0)),
            'started_at': p.get('start_time') or p.get('created_at', ''),
            'finished_at': p.get('end_time') or '',
            'deleted': bool(p.get('deleted', False)),
            'deleted_at': p.get('deleted_at', ''),
            'extra': {
                'email_used': p.get('email_used', ''),
                'successful': p.get('successful_registrations', 0),
                'failed': p.get('failed_registrations', 0),
                'success_rate': p.get('success_rate', 0),
                'duration': p.get('duration', ''),
            },
        })

    all_items = df_items + sf_items + ns_items
    all_items.sort(key=lambda x: (x.get('started_at') or ''), reverse=True)
    return jsonify({'processes': all_items})


@app.route('/api/processes-management/stop', methods=['POST'])
@login_required
def processes_management_stop():
    if not current_user.has_processes_management_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json() or {}
    service = data.get('service', '')
    pid = data.get('id', '')
    if not pid:
        return jsonify({'error': 'Missing process id'}), 400

    if service == 'domain_founder':
        if domain_founder.admin_stop_process(pid):
            return jsonify({'success': True})
        return jsonify({'error': 'Process not running.'}), 404
    if service == 'subdomain_finder':
        ok, err = subdomain_finder.admin_stop_process(pid)
        if ok:
            return jsonify({'success': True})
        return jsonify({'error': err or 'Process not running.'}), 404
    if service == 'news_subscription':
        from news_subscription import admin_stop_process as ns_stop
        if ns_stop(pid):
            return jsonify({'success': True})
        return jsonify({'error': 'Process not running.'}), 404
    return jsonify({'error': 'Unknown service'}), 400


@app.route('/api/processes-management/hard-delete', methods=['POST'])
@login_required
def processes_management_hard_delete():
    if not current_user.has_processes_management_permission:
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json() or {}
    service = data.get('service', '')
    pid = data.get('id', '')
    if not pid:
        return jsonify({'error': 'Missing process id'}), 400

    if service == 'domain_founder':
        if domain_founder.admin_hard_delete_process(pid):
            return jsonify({'success': True})
        return jsonify({'error': 'Process not found.'}), 404
    if service == 'subdomain_finder':
        ok, err = subdomain_finder.admin_hard_delete_process(pid)
        if ok:
            return jsonify({'success': True})
        return jsonify({'error': err or 'Process not found.'}), 404
    if service == 'news_subscription':
        from news_subscription import admin_hard_delete_process as ns_hard_del
        if ns_hard_del(pid):
            return jsonify({'success': True})
        return jsonify({'error': 'Process not found.'}), 404
    return jsonify({'error': 'Unknown service'}), 400


@app.route('/api/processes-management/csv/<service>/<path:pid>')
@login_required
def processes_management_csv(service, pid):
    if not current_user.has_processes_management_permission:
        return jsonify({'error': 'Permission denied'}), 403
    from flask import Response
    if service == 'domain_founder':
        # Build CSV inline using snapshot
        target = next((p for p in domain_founder.get_all_processes_admin() if p.get('key') == pid), None)
        if not target:
            return jsonify({'error': 'Process not found.'}), 404
        import csv as _csv
        from io import StringIO
        buf = StringIO()
        w = _csv.writer(buf)
        w.writerow(['username', 'type', 'status', 'progress', 'total', 'started_at', 'finished_at', 'deleted', 'result'])
        results = target.get('results', []) or []
        if not results:
            w.writerow([target.get('username', ''), target.get('type', ''), target.get('status', ''),
                        target.get('progress', 0), target.get('total', 0),
                        target.get('started_at', ''), target.get('finished_at', ''),
                        target.get('deleted', False), ''])
        else:
            for r in results:
                w.writerow([target.get('username', ''), target.get('type', ''), target.get('status', ''),
                            target.get('progress', 0), target.get('total', 0),
                            target.get('started_at', ''), target.get('finished_at', ''),
                            target.get('deleted', False), str(r)])
        return Response(buf.getvalue().encode('utf-8'), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename=domain_founder_{pid}.csv'})
    if service == 'subdomain_finder':
        # Use existing helper with admin=True; username arg can be empty
        csv_bytes, err = subdomain_finder.get_process_csv('', pid, admin=True)
        if err:
            return jsonify({'error': err}), 404
        return Response(csv_bytes, mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename=subdomain_finder_{pid}.csv'})
    if service == 'news_subscription':
        from news_subscription import get_process_csv as ns_csv
        csv_bytes, err = ns_csv(pid)
        if err:
            return jsonify({'error': err}), 404
        return Response(csv_bytes, mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename=news_subscription_{pid}.csv'})
    return jsonify({'error': 'Unknown service'}), 400


@app.route('/api/subdomain-finder/csv/<key>')
@login_required
def sf_csv(key):
    is_pm = current_user.has_processes_management_permission
    if not current_user.has_subdomain_finder_permission and not is_pm:
        return jsonify({'error': 'Permission denied'}), 403
    csv_bytes, err = subdomain_finder.get_process_csv(current_user.username, key, admin=is_pm)
    if err:
        return jsonify({'error': err}), 404
    from flask import Response
    return Response(
        csv_bytes,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=subdomains_{key}.csv'}
    )

# ─── Warmup Lists ───────────────────────────────────────────────────────────

WARMUP_DIR        = os.path.join('tssApi', 'extensions')
WARMUP_LISTS_FILE = os.path.join(WARMUP_DIR, 'Limit_lists.txt')
WARMUP_TRACKER    = os.path.join(WARMUP_DIR, 'tracker.json')
WARMUP_DISABLED_FILE = os.path.join(WARMUP_DIR, 'disabled_lists.json')

def _warmup_load():
    """Return (lists: list[dict], tracker: dict)"""
    lists = []
    try:
        if os.path.exists(WARMUP_LISTS_FILE):
            with open(WARMUP_LISTS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.rsplit(',', 1)
                    if len(parts) == 2:
                        name  = parts[0].strip()
                        try:
                            total = int(parts[1].strip())
                        except ValueError:
                            total = 0
                        lists.append({'name': name, 'total': total})
    except Exception as e:
        logging.error(f"[warmup] load lists error: {e}")

    tracker = {}
    try:
        if os.path.exists(WARMUP_TRACKER):
            with open(WARMUP_TRACKER, 'r', encoding='utf-8') as f:
                tracker = json.load(f)
    except Exception as e:
        logging.error(f"[warmup] load tracker error: {e}")

    return lists, tracker


def _warmup_save_lists(lists):
    os.makedirs(WARMUP_DIR, exist_ok=True)
    with open(WARMUP_LISTS_FILE, 'w', encoding='utf-8') as f:
        for item in lists:
            f.write(f"{item['name']}, {item['total']}\n")


def _warmup_load_disabled():
    """Return a set of disabled list names."""
    try:
        if os.path.exists(WARMUP_DISABLED_FILE):
            with open(WARMUP_DISABLED_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def _warmup_save_disabled(disabled_set):
    os.makedirs(WARMUP_DIR, exist_ok=True)
    with open(WARMUP_DISABLED_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(disabled_set), f, ensure_ascii=False, indent=2)


@app.route('/warmup-lists')
@login_required
def warmup_lists_page():
    if not current_user.has_warmup_lists_permission:
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('services'))
    lists, tracker = _warmup_load()
    enriched = []
    for item in lists:
        used      = tracker.get(item['name'], 0)
        remaining = max(0, item['total'] - used)
        enriched.append({'name': item['name'], 'total': item['total'],
                         'used': used, 'remaining': remaining})
    return render_template('warmup_lists.html', lists=enriched)


@app.route('/api/warmup-lists', methods=['GET'])
@login_required
def api_warmup_lists_get():
    if not current_user.has_warmup_lists_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    lists, tracker = _warmup_load()
    is_tssw = current_user.entity.upper() == 'TSSW'
    user_entity = current_user.entity.upper()
    disabled_set = _warmup_load_disabled()
    enriched = []
    for item in lists:
        # Non-TSSW users only see their entity's lists
        if not is_tssw:
            if not item['name'].upper().startswith(user_entity):
                continue
        used      = tracker.get(item['name'], 0)
        remaining = max(0, item['total'] - used)
        enriched.append({'name': item['name'], 'total': item['total'],
                         'used': used, 'remaining': remaining,
                         'disabled': item['name'] in disabled_set})
    return jsonify({
        'lists': enriched,
        'is_admin': current_user.has_warmup_lists_admin_permission,
        'is_tssw': is_tssw,
        'user_entity': user_entity,
        'has_history': current_user.has_warmup_history_permission,
        'has_add_record': current_user.has_add_warmup_record_permission or current_user.has_warmup_lists_admin_permission,
    })


@app.route('/api/warmup-lists/<path:name>/add-record', methods=['POST'])
@login_required
def api_warmup_add_record(name):
    if not (current_user.has_add_warmup_record_permission or current_user.has_warmup_lists_admin_permission):
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json() or {}
    try:
        amount = int(data.get('amount', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Amount must be a valid integer'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be greater than 0'}), 400

    lists, tracker = _warmup_load()
    if not any(item['name'] == name for item in lists):
        return jsonify({'error': 'List not found'}), 404

    # Block records on disabled lists
    disabled_set = _warmup_load_disabled()
    if name in disabled_set:
        return jsonify({'error': 'This list is disabled. No new records can be added.'}), 403

    # Update tracker
    current_val = tracker.get(name, 0)
    tracker[name] = current_val + amount
    os.makedirs(WARMUP_DIR, exist_ok=True)
    with open(WARMUP_TRACKER, 'w', encoding='utf-8') as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)

    # Append to History.json
    now = datetime.now(timezone(timedelta(hours=1))).replace(tzinfo=None)
    record = {
        'list_name':   name,
        'number_sent': amount,
        'date':        now.strftime('%Y-%m-%d'),
        'time':        now.strftime('%H:%M:%S'),
        'datetime':    now.strftime('%Y-%m-%d %H:%M:%S'),
        'added_by':      current_user.username,
        'added_by_name': current_user.name,
        'type':          'Manual',
    }
    history = []
    if os.path.exists(WARMUP_HISTORY_FILE):
        try:
            with open(WARMUP_HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(record)
    with open(WARMUP_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    return jsonify({'success': True, 'new_total': tracker[name]})


@app.route('/api/warmup-lists', methods=['POST'])
@login_required
def api_warmup_lists_add():
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data  = request.get_json() or {}
    name  = (data.get('name') or '').strip()
    total = data.get('total')
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if total is None:
        return jsonify({'error': 'Total is required'}), 400
    try:
        total = int(total)
        if total < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({'error': 'Total must be a non-negative integer'}), 400
    if ',' in name:
        return jsonify({'error': 'List name must not contain commas'}), 400
    lists, _ = _warmup_load()
    if any(item['name'] == name for item in lists):
        return jsonify({'error': 'A list with this name already exists'}), 409
    lists.append({'name': name, 'total': total})
    _warmup_save_lists(lists)
    return jsonify({'success': True})


@app.route('/api/warmup-lists/<path:name>', methods=['PUT'])
@login_required
def api_warmup_lists_update(name):
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data      = request.get_json() or {}
    new_name  = (data.get('name') or '').strip()
    new_total = data.get('total')
    if not new_name:
        return jsonify({'error': 'Name is required'}), 400
    if new_total is None:
        return jsonify({'error': 'Total is required'}), 400
    try:
        new_total = int(new_total)
        if new_total < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({'error': 'Total must be a non-negative integer'}), 400
    if ',' in new_name:
        return jsonify({'error': 'List name must not contain commas'}), 400
    lists, tracker = _warmup_load()
    idx = next((i for i, item in enumerate(lists) if item['name'] == name), None)
    if idx is None:
        return jsonify({'error': 'List not found'}), 404
    if new_name != name and any(item['name'] == new_name for item in lists):
        return jsonify({'error': 'A list with this name already exists'}), 409
    lists[idx] = {'name': new_name, 'total': new_total}
    _warmup_save_lists(lists)
    # rename key in tracker if name changed
    if new_name != name and name in tracker:
        tracker[new_name] = tracker.pop(name)
        with open(WARMUP_TRACKER, 'w', encoding='utf-8') as f:
            json.dump(tracker, f, indent=2, ensure_ascii=False)
    return jsonify({'success': True})


@app.route('/api/warmup-lists/<path:name>', methods=['DELETE'])
@login_required
def api_warmup_lists_delete(name):
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    lists, _ = _warmup_load()
    new_lists = [item for item in lists if item['name'] != name]
    if len(new_lists) == len(lists):
        return jsonify({'error': 'List not found'}), 404
    _warmup_save_lists(new_lists)
    # Also remove from disabled set if present
    disabled_set = _warmup_load_disabled()
    if name in disabled_set:
        disabled_set.discard(name)
        _warmup_save_disabled(disabled_set)
    return jsonify({'success': True})


@app.route('/api/warmup-lists/<path:name>/toggle-disable', methods=['POST'])
@login_required
def api_warmup_toggle_disable(name):
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    data    = request.get_json() or {}
    disable = data.get('disable')  # True = disable the list, False = re-enable it

    lists, _ = _warmup_load()
    idx = next((i for i, item in enumerate(lists) if item['name'] == name), None)
    if idx is None:
        return jsonify({'error': 'List not found'}), 404

    disabled_set = _warmup_load_disabled()

    if disable:
        # Disabling: set total to 0 in Limit_lists.txt
        lists[idx]['total'] = 0
        _warmup_save_lists(lists)
        disabled_set.add(name)
        _warmup_save_disabled(disabled_set)
        return jsonify({'success': True})
    else:
        # Enabling: require new_total > 0
        new_total = data.get('new_total')
        if new_total is None:
            return jsonify({'error': 'Total is required when enabling a list'}), 400
        try:
            new_total = int(new_total)
            if new_total <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            return jsonify({'error': 'Maximum emails must be greater than 0 to enable the list'}), 400
        lists[idx]['total'] = new_total
        _warmup_save_lists(lists)
        disabled_set.discard(name)
        _warmup_save_disabled(disabled_set)
        return jsonify({'success': True})


WARMUP_HISTORY_FILE = os.path.join(WARMUP_DIR, 'History.json')

@app.route('/api/warmup-history', methods=['GET'])
@login_required
def api_warmup_history():
    if not current_user.has_warmup_lists_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    if not current_user.has_warmup_history_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        history = []
        if os.path.exists(WARMUP_HISTORY_FILE):
            with open(WARMUP_HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
    except Exception as e:
        logging.error(f"[warmup history] load error: {e}")
        return jsonify({'error': 'Failed to load history'}), 500

    is_tssw = current_user.entity.upper() == 'TSSW'
    is_admin = current_user.has_warmup_lists_admin_permission
    user_entity = current_user.entity.upper()

    # Filter history by entity unless TSSW admin
    if not (is_tssw or is_admin):
        history = [h for h in history if h.get('list_name', '').upper().startswith(user_entity)]

    # Optional date/time range filtering
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    time_from = request.args.get('time_from')
    time_to   = request.args.get('time_to')

    if date_from:
        history = [h for h in history if h.get('date', '') >= date_from]
    if date_to:
        history = [h for h in history if h.get('date', '') <= date_to]
    if time_from and date_from == date_to and date_from:
        history = [h for h in history if h.get('time', '') >= time_from]
    if time_to and date_from == date_to and date_to:
        history = [h for h in history if h.get('time', '') <= time_to]

    return jsonify({'history': list(reversed(history)), 'is_admin': is_admin or is_tssw})


@app.route('/api/warmup-history/delete', methods=['POST'])
@login_required
def api_warmup_history_delete():
    if not current_user.has_warmup_lists_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json(silent=True) or {}
    list_name   = data.get('list_name')
    number_sent = data.get('number_sent')
    dt          = data.get('datetime')

    if not list_name or number_sent is None or not dt:
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        history = []
        if os.path.exists(WARMUP_HISTORY_FILE):
            with open(WARMUP_HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
    except Exception as e:
        return jsonify({'error': 'Failed to load history'}), 500

    # Find and remove the first matching record
    idx_to_remove = None
    for i, rec in enumerate(history):
        if (rec.get('list_name') == list_name and
                rec.get('number_sent') == number_sent and
                rec.get('datetime') == dt):
            idx_to_remove = i
            break

    if idx_to_remove is None:
        return jsonify({'error': 'Record not found'}), 404

    history.pop(idx_to_remove)

    try:
        with open(WARMUP_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({'error': 'Failed to save history'}), 500

    # Subtract from tracker.json
    try:
        _, tracker = _warmup_load()
        current_val = tracker.get(list_name, 0)
        tracker[list_name] = max(0, current_val - number_sent)
        os.makedirs(WARMUP_DIR, exist_ok=True)
        with open(WARMUP_TRACKER, 'w', encoding='utf-8') as f:
            json.dump(tracker, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"[warmup history delete] tracker update error: {e}")

    return jsonify({'success': True})


LATEST_MESSAGE_FILE = os.path.join('tssApi', 'latest-message.txt')

@app.route('/api/warmup-lists/latest-message', methods=['GET'])
@login_required
def api_warmup_latest_message_get():
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        if os.path.exists(LATEST_MESSAGE_FILE):
            with open(LATEST_MESSAGE_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = ''
        return jsonify({'content': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/warmup-lists/latest-message', methods=['POST'])
@login_required
def api_warmup_latest_message_save():
    if not current_user.has_warmup_lists_admin_permission:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        data = request.get_json(silent=True) or {}
        content = data.get('content', '')
        os.makedirs('tssApi', exist_ok=True)
        with open(LATEST_MESSAGE_FILE, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/warmup-reports')
@login_required
def warmup_reports():
    if not current_user.has_warmup_reports_permission:
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('services'))
    import glob as _glob
    json_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jsonfiles')
    files = _glob.glob(os.path.join(json_dir, 'drop_*.json'))
    available_hours = []
    for f in files:
        basename = os.path.basename(f)
        parts = basename.replace('.json', '').split('_')
        if len(parts) == 5:
            try:
                hour = int(parts[1])
                day = int(parts[2])
                month = int(parts[3])
                year = int(parts[4])
                from datetime import datetime as _dt
                dt = _dt(year, month, day, hour)
                available_hours.append({
                    'hour': f"{hour:02d}",
                    'date': f"{parts[2]}_{parts[3]}_{parts[4]}",
                    'filename': basename,
                    'ts': dt.timestamp(),
                    'display': f"{hour:02d}:00  •  {day:02d}/{month:02d}/{year}"
                })
            except (ValueError, IndexError):
                pass
    available_hours.sort(key=lambda x: x['ts'], reverse=True)
    from datetime import datetime as _dt2, timezone as _tz, timedelta as _td
    now = _dt2.now(_tz(_td(hours=1)))
    live_hour = f"{now.hour:02d}"
    live_date = now.strftime('%d_%m_%Y')
    return render_template('warmup_reports.html',
        available_hours=available_hours,
        live_hour=live_hour,
        live_date=live_date,
        page_title='Warmup Reports',
        page_icon='ph-chart-line-up',
        has_sessions_tab=current_user.has_warmup_sessions_permission
    )


@app.route('/api/warmup-reports/hours')
@login_required
def api_warmup_reports_hours():
    """Return current available hours list and live hour — used by frontend auto-refresh"""
    if not current_user.has_warmup_reports_permission:
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    import glob as _glob
    json_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jsonfiles')
    files = _glob.glob(os.path.join(json_dir, 'drop_*.json'))
    available_hours = []
    for f in files:
        basename = os.path.basename(f)
        parts = basename.replace('.json', '').split('_')
        if len(parts) == 5:
            try:
                hour = int(parts[1])
                day = int(parts[2])
                month = int(parts[3])
                year = int(parts[4])
                from datetime import datetime as _dt
                dt = _dt(year, month, day, hour)
                available_hours.append({
                    'hour': f"{hour:02d}",
                    'date': f"{parts[2]}_{parts[3]}_{parts[4]}",
                    'ts': dt.timestamp(),
                    'display': f"{hour:02d}:00  •  {day:02d}/{month:02d}/{year}"
                })
            except (ValueError, IndexError):
                pass
    available_hours.sort(key=lambda x: x['ts'], reverse=True)
    from datetime import datetime as _dt2, timezone as _tz, timedelta as _td
    now = _dt2.now(_tz(_td(hours=1)))
    live_hour = f"{now.hour:02d}"
    live_date = now.strftime('%d_%m_%Y')
    return jsonify({
        'success': True,
        'live_hour': live_hour,
        'live_date': live_date,
        'available_hours': available_hours
    })


# In-memory cache for warmup report data: filepath → (mtime, etag, response_bytes)
_warmup_data_cache = {}
_warmup_data_cache_lock = _threading.Lock()

@app.route('/api/warmup-reports/data')
@login_required
def api_warmup_reports_data():
    if not current_user.has_warmup_reports_permission:
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    hour = request.args.get('hour', '').zfill(2)
    date = request.args.get('date', '')
    if not hour or not date:
        return jsonify({'success': False, 'error': 'Missing params'}), 400
    filename = f"drop_{hour}_{date}.json"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jsonfiles', filename)
    if not os.path.exists(filepath):
        return jsonify({'success': True, 'empty': True, 'data': None})
    try:
        current_mtime = os.path.getmtime(filepath)
        etag = f'"{filename}-{current_mtime}"'

        # Return 304 if client already has the current version
        if request.headers.get('If-None-Match') == etag:
            return '', 304

        # Serve from in-memory cache if file hasn't changed
        with _warmup_data_cache_lock:
            cached = _warmup_data_cache.get(filepath)
            if cached and cached[0] == current_mtime:
                body_bytes = cached[1]
            else:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not data:
                    payload = {'success': True, 'empty': True, 'data': None}
                else:
                    payload = {'success': True, 'empty': False, 'data': data}
                import json as _json
                body_bytes = _json.dumps(payload).encode('utf-8')
                _warmup_data_cache[filepath] = (current_mtime, body_bytes)

        resp = make_response(body_bytes)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


if __name__ == '__main__':
    import subprocess, sys

    # gthread instead of the default sync worker class: sync gives each
    # worker exactly one request at a time, so a single slow IMAP/DNS call
    # (this app does plenty of both) occupies the ENTIRE worker until it
    # finishes or gunicorn kills it on --timeout — taking every other
    # in-flight request down with it. gthread lets each worker process
    # handle several requests concurrently on separate threads, all sharing
    # the same in-process state (user_processes dicts, _EXTRACT_TASKS, etc.)
    # that this app relies on — unlike bumping --workers with sync, which
    # would split that state across separate processes that can't see each
    # other's data.
    #
    # Tune via env vars if needed; defaults are conservative.
    gunicorn_workers = os.environ.get('GUNICORN_WORKERS', '2')
    gunicorn_threads = os.environ.get('GUNICORN_THREADS', '4')

    subprocess.run([
        sys.executable, '-m', 'gunicorn',
        '--bind', '0.0.0.0:5000',
        '--worker-class', 'gthread',
        '--workers', gunicorn_workers,
        '--threads', gunicorn_threads,
        '--timeout', '120',
        '--reuse-port',
        'main:app'
    ])
