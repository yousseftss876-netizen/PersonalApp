import os
import json
import logging
import threading
import subprocess
import uuid
import re
import time
import csv
import io
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger('subdomain_finder')

DATA_DIR = 'subdomain_finder_data'
os.makedirs(DATA_DIR, exist_ok=True)

SUBFINDER_BIN = '/root/go/bin/subfinder'
SUBFINDER_THREADS = 30

GLOBAL_MAX_CONCURRENT = 20
_global_semaphore = threading.Semaphore(GLOBAL_MAX_CONCURRENT)

# All live (in-memory) processes: process_key -> process dict
user_processes = {}
process_lock = threading.Lock()

ADJECTIVES = [
    'swift', 'silent', 'golden', 'dark', 'bright', 'cold', 'rapid',
    'mighty', 'ancient', 'cosmic', 'electric', 'frozen', 'hidden',
    'iron', 'jade', 'lunar', 'mystic', 'neon', 'onyx', 'polar',
    'quantum', 'rustic', 'solar', 'tidal', 'ultra', 'vast', 'wild',
    'xenon', 'yellow', 'zenith'
]
NOUNS = [
    'falcon', 'storm', 'hawk', 'wolf', 'tiger', 'raven', 'phoenix',
    'dragon', 'cobra', 'eagle', 'frost', 'ghost', 'hunter', 'jaguar',
    'knight', 'lynx', 'mantis', 'nebula', 'orbit', 'panther',
    'quest', 'raptor', 'scout', 'titan', 'ultra', 'viper', 'wizard',
    'xenon', 'yeti', 'zephyr'
]

def generate_random_name():
    import random
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(100, 999)}"


# ── Data helpers ───────────────────────────────────────────────────────────────

def get_user_domains_file(username):
    return os.path.join(DATA_DIR, f'{username}_domains.json')

def get_user_processes_file(username):
    return os.path.join(DATA_DIR, f'{username}_processes.json')


def load_user_domains(username):
    f = get_user_domains_file(username)
    if os.path.exists(f):
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                return json.load(fp)
        except:
            return []
    return []


def save_user_domains(username, domains):
    f = get_user_domains_file(username)
    with open(f, 'w', encoding='utf-8') as fp:
        json.dump(domains, fp, ensure_ascii=False)


def load_persisted_processes(username):
    """Load all persisted processes for a user from disk."""
    f = get_user_processes_file(username)
    if os.path.exists(f):
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                return json.load(fp)
        except:
            return []
    return []


def save_persisted_processes(username, processes):
    f = get_user_processes_file(username)
    with open(f, 'w', encoding='utf-8') as fp:
        json.dump(processes, fp, ensure_ascii=False)


def persist_process(username, proc_data):
    """Upsert a process snapshot to the user's disk history."""
    history = load_persisted_processes(username)
    for i, p in enumerate(history):
        if p['key'] == proc_data['key']:
            history[i] = proc_data
            save_persisted_processes(username, history)
            return
    history.insert(0, proc_data)
    save_persisted_processes(username, history)


def hard_delete_persisted_process(username, key):
    """Permanently remove a process from the user's disk history."""
    history = load_persisted_processes(username)
    history = [p for p in history if p['key'] != key]
    save_persisted_processes(username, history)


# ── Process helpers ────────────────────────────────────────────────────────────

def _make_proc_snapshot(key, proc):
    """Return a safe serialisable snapshot of a live process dict."""
    completed = proc.get('completed_domains', [])
    if isinstance(completed, set):
        completed = list(completed)
    return {
        'key': key,
        'name': proc.get('name', key),
        'username': proc.get('username', ''),
        'status': proc.get('status', 'unknown'),
        'progress': proc.get('progress', 0),
        'total': proc.get('total', 0),
        'started_at': proc.get('started_at', ''),
        'finished_at': proc.get('finished_at', ''),
        'domains': proc.get('domains', []),
        'results': proc.get('results', {}),
        'completed_domains': completed,
        'deleted': proc.get('deleted', False),
        'deleted_at': proc.get('deleted_at', ''),
        'stop_at': proc.get('stop_at', 0),
        'stop_reason': proc.get('stop_reason', ''),
    }


def get_user_processes(username):
    """Return all non-deleted processes (live + persisted) for a user."""
    live = []
    with process_lock:
        for key, proc in user_processes.items():
            if proc.get('username') == username:
                live.append(_make_proc_snapshot(key, proc))

    live_keys = {p['key'] for p in live}

    history = load_persisted_processes(username)
    for p in history:
        if p['key'] not in live_keys and not p.get('deleted', False):
            live.append(p)

    live.sort(key=lambda x: x.get('started_at', ''), reverse=True)
    return live


def count_active_processes(username):
    with process_lock:
        return sum(
            1 for proc in user_processes.values()
            if proc.get('username') == username
            and proc.get('status') in ('running', 'starting', 'paused')
        )


def can_start_process(username, max_allowed):
    if max_allowed == 0:
        return False
    return count_active_processes(username) < max_allowed


# ── Recovery on startup ────────────────────────────────────────────────────────

def recover_interrupted_processes():
    """
    Called once at app startup. Scans all persisted process files and marks
    any process that was running/starting/paused as 'interrupted' so the
    user knows it stopped and can resume.
    """
    if not os.path.exists(DATA_DIR):
        return
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_processes.json'):
            continue
        username = fname[:-len('_processes.json')]
        try:
            procs = load_persisted_processes(username)
            changed = False
            for p in procs:
                if p.get('status') in ('running', 'starting', 'paused', 'stopping'):
                    p['status'] = 'interrupted'
                    p['finished_at'] = p.get('finished_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    changed = True
            if changed:
                save_persisted_processes(username, procs)
        except Exception as e:
            logger.error('Recovery error for %s: %s', username, e)


# ── Subfinder runner ───────────────────────────────────────────────────────────

DOMAIN_RE = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
)

def is_valid_domain(d):
    return bool(DOMAIN_RE.match(d.strip()))


def run_subfinder_for_domain(domain):
    acquired = _global_semaphore.acquire(timeout=600)
    if not acquired:
        logger.warning("Global semaphore timeout for domain %s", domain)
        return []
    try:
        cmd = [SUBFINDER_BIN, '-d', domain, '-silent', '-t', str(SUBFINDER_THREADS)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        return lines
    except subprocess.TimeoutExpired:
        logger.warning("subfinder timeout for domain %s", domain)
        return []
    except FileNotFoundError:
        logger.error("subfinder binary not found at %s", SUBFINDER_BIN)
        return []
    except Exception as e:
        logger.error("subfinder error for %s: %s", domain, e)
        return []
    finally:
        _global_semaphore.release()


# ── Process runner ─────────────────────────────────────────────────────────────

def _count_collected_subdomains(proc):
    """Return the total number of subdomains collected so far in this process."""
    return sum(len(v) for v in proc.get('results', {}).values())


def run_process(process_key, domains):
    """
    Main worker. Iterates over domains using a thread pool.
    Saves state to disk after each domain completes so restarts can resume.
    """
    proc = user_processes.get(process_key)
    if not proc:
        return

    username = proc.get('username', '')
    stop_at = proc.get('stop_at', 0)  # 0 = unlimited
    proc['status'] = 'running'
    proc['total'] = proc.get('total', len(domains))  # preserve total from resume

    MAX_PARALLEL_PER_PROCESS = 3
    _last_persist = [time.time()]

    def process_domain(domain):
        while proc.get('pause_flag') and not proc.get('stop_flag'):
            time.sleep(0.5)

        if proc.get('stop_flag'):
            return

        subdomains = run_subfinder_for_domain(domain)

        with process_lock:
            if process_key in user_processes:
                user_processes[process_key]['results'][domain] = subdomains
                user_processes[process_key]['progress'] += 1
                cd = user_processes[process_key].setdefault('completed_domains', set())
                if isinstance(cd, list):
                    cd = set(cd)
                    user_processes[process_key]['completed_domains'] = cd
                cd.add(domain)

                # Check stop_at limit after updating results
                if stop_at and stop_at > 0:
                    total_collected = _count_collected_subdomains(user_processes[process_key])
                    if total_collected >= stop_at:
                        user_processes[process_key]['stop_flag'] = True
                        user_processes[process_key]['stop_reason'] = 'limit_reached'

        # Persist after each domain or every 30 seconds
        now = time.time()
        if now - _last_persist[0] >= 30:
            _last_persist[0] = now
            snapshot = _make_proc_snapshot(process_key, user_processes[process_key])
            snapshot['status'] = 'running'
            persist_process(username, snapshot)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PER_PROCESS) as executor:
        futures = {executor.submit(process_domain, d): d for d in domains}
        for future in as_completed(futures):
            if proc.get('stop_flag'):
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                future.result()
            except Exception as e:
                logger.error("Error processing domain: %s", e)

    with process_lock:
        if process_key in user_processes:
            limit_reached = user_processes[process_key].get('stop_reason') == 'limit_reached'
            if limit_reached:
                final_status = 'limit_reached'
            elif proc.get('stop_flag'):
                final_status = 'stopped'
            else:
                final_status = 'completed'
            user_processes[process_key]['status'] = final_status
            user_processes[process_key]['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            snapshot = _make_proc_snapshot(process_key, user_processes[process_key])

    persist_process(username, snapshot)


# ── Public API ─────────────────────────────────────────────────────────────────

def start_process(username, process_name, domains, max_allowed, max_domains_per_process, stop_at=0):
    if not can_start_process(username, max_allowed):
        return None, f'You have reached your process limit ({max_allowed}). Stop an existing process first.'

    if not domains:
        return None, 'No domains provided.'

    if max_domains_per_process and max_domains_per_process > 0:
        if len(domains) > max_domains_per_process:
            return None, f'Too many domains. Your limit is {max_domains_per_process} domains per process.'

    valid_domains = [d for d in domains if is_valid_domain(d)]
    if not valid_domains:
        return None, 'No valid domains found in the list.'

    process_key = f"{username}_{uuid.uuid4().hex[:10]}"
    name = process_name.strip() if process_name and process_name.strip() else generate_random_name()

    with process_lock:
        user_processes[process_key] = {
            'key': process_key,
            'name': name,
            'username': username,
            'status': 'starting',
            'progress': 0,
            'total': len(valid_domains),
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': '',
            'domains': valid_domains,
            'results': {},
            'completed_domains': set(),
            'stop_flag': False,
            'pause_flag': False,
            'deleted': False,
            'deleted_at': '',
            'stop_at': int(stop_at) if stop_at else 0,
            'stop_reason': '',
        }

    thread = threading.Thread(
        target=run_process,
        args=(process_key, valid_domains),
        daemon=True
    )
    thread.start()
    return process_key, None


def pause_process(username, key):
    with process_lock:
        proc = user_processes.get(key)
        if not proc or proc.get('username') != username:
            return False, 'Process not found.'
        if proc['status'] != 'running':
            return False, 'Process is not running.'
        proc['pause_flag'] = True
        proc['status'] = 'paused'
    return True, None


def resume_process(username, key):
    """
    Resume a paused OR interrupted process.
    For 'paused': just unpause the live thread.
    For 'interrupted': reload from disk and re-launch remaining domains.
    """
    with process_lock:
        proc = user_processes.get(key)
        if proc and proc.get('username') == username:
            # Live process, just unpause
            if proc['status'] == 'paused':
                proc['pause_flag'] = False
                proc['status'] = 'running'
                return True, None
            return False, 'Process is not paused.'

    # Not in memory — look in persisted history (interrupted)
    history = load_persisted_processes(username)
    snapshot = next((p for p in history if p['key'] == key), None)
    if not snapshot:
        return False, 'Process not found.'
    if snapshot.get('deleted', False):
        return False, 'Process has been deleted.'
    if snapshot['status'] != 'interrupted':
        return False, f"Cannot resume a '{snapshot['status']}' process."

    # Check process limit before relaunching
    if not can_start_process(username, 999):  # we'll check limit in caller
        return False, 'Too many active processes.'

    completed = set(snapshot.get('completed_domains', []))
    all_domains = snapshot.get('domains', [])
    remaining = [d for d in all_domains if d not in completed]

    if not remaining:
        # All domains were already done — mark as completed
        snapshot['status'] = 'completed'
        snapshot['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, snapshot)
        return False, 'All domains already processed. Process is now marked completed.'

    # Reload into memory
    with process_lock:
        user_processes[key] = {
            'key': key,
            'name': snapshot.get('name', key),
            'username': username,
            'status': 'starting',
            'progress': snapshot.get('progress', 0),
            'total': snapshot.get('total', len(all_domains)),
            'started_at': snapshot.get('started_at', ''),
            'finished_at': '',
            'domains': all_domains,
            'results': snapshot.get('results', {}),
            'completed_domains': completed,
            'stop_flag': False,
            'pause_flag': False,
            'deleted': False,
            'deleted_at': '',
        }

    thread = threading.Thread(
        target=run_process,
        args=(key, remaining),
        daemon=True
    )
    thread.start()
    return True, None


def resume_process_with_limit(username, key, max_allowed):
    """Wrapper that checks process limit before resuming an interrupted process."""
    # If it's a live paused process, no limit concern
    with process_lock:
        proc = user_processes.get(key)
        if proc and proc.get('username') == username and proc.get('status') == 'paused':
            proc['pause_flag'] = False
            proc['status'] = 'running'
            return True, None

    if not can_start_process(username, max_allowed):
        return False, f'You have reached your process limit ({max_allowed}).'
    return resume_process(username, key)


def stop_process(username, key):
    with process_lock:
        proc = user_processes.get(key)
        if not proc or proc.get('username') != username:
            return False, 'Process not found.'
        if proc.get('status') not in ('running', 'paused', 'starting'):
            return False, 'Process is not active.'
        proc['stop_flag'] = True
        proc['status'] = 'stopping'
    return True, None


def delete_process(username, key):
    """
    Soft-delete: removes from user's view but keeps record on disk for admins.
    """
    # Check if live in memory
    with process_lock:
        proc = user_processes.get(key)
        if proc:
            if proc.get('username') != username:
                return False, 'Permission denied.'
            if proc.get('status') in ('running', 'paused', 'starting'):
                return False, 'Stop the process before deleting it.'
            # Snapshot it before removing from memory
            snapshot = _make_proc_snapshot(key, proc)
            del user_processes[key]
        else:
            snapshot = None

    # Mark as deleted in persisted storage
    history = load_persisted_processes(username)
    found = False
    for p in history:
        if p['key'] == key:
            p['deleted'] = True
            p['deleted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            found = True
            break

    if not found and snapshot:
        snapshot['deleted'] = True
        snapshot['deleted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history.insert(0, snapshot)

    if not found and not snapshot:
        # Try to find and soft-delete only from persisted
        return False, 'Process not found.'

    save_persisted_processes(username, history)
    return True, None


def admin_stop_process(key):
    """Admin can stop any active process regardless of owner."""
    with process_lock:
        proc = user_processes.get(key)
        if not proc:
            return False, 'Process not running.'
        if proc.get('status') not in ('running', 'paused', 'starting'):
            return False, 'Process is not active.'
        proc['stop_flag'] = True
        proc['status'] = 'stopping'
    return True, None


def admin_hard_delete_process(key):
    """
    Hard-delete by admin: permanently removes from ALL users' persisted files.
    Also removes from memory if live.
    """
    # Remove from memory if live
    with process_lock:
        if key in user_processes:
            del user_processes[key]

    # Find which user owns it and remove from disk
    if not os.path.exists(DATA_DIR):
        return True, None
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_processes.json'):
            continue
        username = fname[:-len('_processes.json')]
        procs = load_persisted_processes(username)
        new_procs = [p for p in procs if p['key'] != key]
        if len(new_procs) != len(procs):
            save_persisted_processes(username, new_procs)
            return True, None
    return False, 'Process not found.'


def get_process_csv(username, key, admin=False):
    # Try live memory first
    with process_lock:
        proc = user_processes.get(key)
        if proc and (admin or proc.get('username') == username):
            snapshot = _make_proc_snapshot(key, proc)
        else:
            snapshot = None

    if not snapshot:
        if admin:
            # Scan all persisted user files to find the process
            if os.path.exists(DATA_DIR):
                for fname in os.listdir(DATA_DIR):
                    if fname.endswith('_processes.json'):
                        uname = fname[:-len('_processes.json')]
                        history = load_persisted_processes(uname)
                        found = next((p for p in history if p['key'] == key), None)
                        if found:
                            snapshot = found
                            break
        else:
            history = load_persisted_processes(username)
            snapshot = next((p for p in history if p['key'] == key), None)

    if not snapshot:
        return None, 'Process not found.'

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Domain', 'Subdomain'])

    for domain, subdomains in snapshot.get('results', {}).items():
        if subdomains:
            for sub in subdomains:
                writer.writerow([domain, sub])
        else:
            writer.writerow([domain, ''])

    return output.getvalue().encode('utf-8'), None


# ── Admin helpers ──────────────────────────────────────────────────────────────

def get_all_processes_admin():
    """
    Return ALL processes from ALL users, including deleted ones (for archiving).
    Merges live in-memory processes with all persisted files.
    """
    all_procs = {}

    # First pass: live in-memory
    with process_lock:
        for key, proc in user_processes.items():
            all_procs[key] = _make_proc_snapshot(key, proc)

    # Second pass: all persisted files
    if os.path.exists(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith('_processes.json'):
                continue
            username = fname[:-len('_processes.json')]
            try:
                procs = load_persisted_processes(username)
                for p in procs:
                    k = p['key']
                    if k not in all_procs:
                        all_procs[k] = p
                    else:
                        # Keep live version but ensure deleted flag is from disk if newer
                        if p.get('deleted') and not all_procs[k].get('deleted'):
                            all_procs[k]['deleted'] = True
                            all_procs[k]['deleted_at'] = p.get('deleted_at', '')
            except Exception as e:
                logger.error('Admin scan error for %s: %s', username, e)

    result = sorted(all_procs.values(), key=lambda x: x.get('started_at', ''), reverse=True)
    return result


# Legacy alias kept for backward compat
def get_all_active_processes():
    with process_lock:
        return [
            _make_proc_snapshot(k, v) for k, v in user_processes.items()
            if v.get('status') in ('running', 'paused', 'starting', 'stopping')
        ]
