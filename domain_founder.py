import os
import json
import logging
import threading
import time
import dns.resolver
import ipaddress
import socket
import re
import csv
import io
import xml.etree.ElementTree as ET
import spf
import requests
import tldextract
from tld import get_tld
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logger = logging.getLogger('domain_founder')

DATA_DIR = 'domain_founder_data'
os.makedirs(DATA_DIR, exist_ok=True)

user_processes = {}
process_lock = threading.Lock()

# ── Namecheap availability checker config ──────────────────────────────────────
NAMECHEAP_API_USER = "redonetssw"
NAMECHEAP_API_KEY = "9b4eeb3240cf4f21ac92bfb390e27a8c"
NAMECHEAP_API_URL = "https://api.namecheap.com/xml.response"
NAMECHEAP_BATCH_SIZE = 50
NAMECHEAP_DELAY_SECONDS = 3
NAMECHEAP_NS = {'nc': 'http://api.namecheap.com/xml.response'}

_tld_extractor = tldextract.TLDExtract(suffix_list_urls=None)
_public_ip_cache = {'ip': None, 'fetched_at': 0}
_public_ip_lock = threading.Lock()

resolver = dns.resolver.Resolver()
resolver.timeout = 5
resolver.lifetime = 10
resolver.retries = 2
resolver.nameservers = ['8.8.8.8', '1.1.1.1', '8.8.4.4']

MAX_DEPTH = 10


# ── Domain file helpers ────────────────────────────────────────────────────────

def get_user_file(username):
    return os.path.join(DATA_DIR, f'{username}_domains.json')

def get_user_processes_file(username):
    return os.path.join(DATA_DIR, f'{username}_processes.json')


def load_user_domains(username):
    filepath = get_user_file(username)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []


def save_user_domains(username, domains):
    filepath = get_user_file(username)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(domains, f, ensure_ascii=False)


# ── Process persistence helpers ────────────────────────────────────────────────

def load_persisted_processes(username):
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
    tmp = f + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(processes, fp, ensure_ascii=False)
    os.replace(tmp, f)


def persist_process(username, proc_data):
    """Upsert a process snapshot into the user's disk history."""
    history = load_persisted_processes(username)
    for i, p in enumerate(history):
        if p['key'] == proc_data['key']:
            history[i] = proc_data
            save_persisted_processes(username, history)
            return
    history.insert(0, proc_data)
    save_persisted_processes(username, history)


def hard_delete_persisted_process_by_key(key):
    """Permanently remove a process from ALL users' persisted files."""
    if not os.path.exists(DATA_DIR):
        return False
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_processes.json'):
            continue
        username = fname[:-len('_processes.json')]
        procs = load_persisted_processes(username)
        new_procs = [p for p in procs if p['key'] != key]
        if len(new_procs) != len(procs):
            save_persisted_processes(username, new_procs)
            return True
    return False


# ── Snapshot helper ────────────────────────────────────────────────────────────

def _make_snapshot(key, proc):
    completed = proc.get('completed_domains', [])
    if isinstance(completed, set):
        completed = list(completed)
    availability = proc.get('availability') or {}
    return {
        'key': key,
        'type': proc.get('type', ''),
        'username': proc.get('username', ''),
        'status': proc.get('status', ''),
        'progress': proc.get('progress', 0),
        'total': proc.get('total', 0),
        'results': list(proc.get('results', [])),
        'started_at': proc.get('started_at', ''),
        'finished_at': proc.get('finished_at', ''),
        'params': proc.get('params', {}),
        'domains_input': proc.get('domains_input', []),
        'completed_domains': completed,
        'deleted': proc.get('deleted', False),
        'deleted_at': proc.get('deleted_at', ''),
        'availability': {
            'status': availability.get('status', 'idle'),
            'progress': availability.get('progress', 0),
            'total': availability.get('total', 0),
            'started_at': availability.get('started_at', ''),
            'finished_at': availability.get('finished_at', ''),
            'error': availability.get('error', ''),
            'available_count': availability.get('available_count', 0),
            'pairs': list(availability.get('pairs', [])),
        },
    }


# ── Recovery on startup ────────────────────────────────────────────────────────

def _launch_process_thread(process_key, process_type, remaining, params):
    """Start the correct background thread for a process type. Returns the thread or None."""
    if process_type == 'include':
        return threading.Thread(target=run_include_process, args=(process_key, remaining), daemon=True)
    elif process_type == 'a_records':
        return threading.Thread(target=run_a_records_process, args=(process_key, remaining), daemon=True)
    elif process_type == 'ip':
        target_ips = params.get('ips', [])
        return threading.Thread(target=run_ip_process, args=(process_key, remaining, target_ips), daemon=True)
    elif process_type == 'query':
        queries = params.get('queries', [])
        if isinstance(queries, str):
            queries = [queries]
        if not queries and params.get('query'):
            queries = [params['query']]
        queries = [q.strip() for q in queries if q.strip()]
        return threading.Thread(target=run_query_process, args=(process_key, remaining, queries), daemon=True)
    elif process_type == 'cname':
        return threading.Thread(target=run_cname_process, args=(process_key, remaining), daemon=True)
    return None


def recover_interrupted_processes():
    """
    Called once at app startup. Marks any running/starting processes as
    'interrupted' and immediately auto-resumes them so they continue from
    where they left off without any user interaction.
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
            to_resume = []
            for p in procs:
                if p.get('status') in ('running', 'starting', 'stopping'):
                    p['status'] = 'interrupted'
                    p['finished_at'] = p.get('finished_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    changed = True
                # Queue for auto-resume: anything interrupted (newly or already on disk)
                if p.get('status') == 'interrupted' and not p.get('deleted', False):
                    to_resume.append(dict(p))
                avail = p.get('availability')
                if avail and avail.get('status') == 'running':
                    avail['status'] = 'error'
                    avail['error'] = 'Service restarted before check completed'
                    avail['finished_at'] = avail.get('finished_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    changed = True
            if changed:
                save_persisted_processes(username, procs)

            # Auto-resume every interrupted process without waiting for the user
            for snapshot in to_resume:
                try:
                    process_key = snapshot['key']
                    process_type = snapshot.get('type', '')
                    params = snapshot.get('params', {})
                    completed = set(snapshot.get('completed_domains', []))
                    all_domains = snapshot.get('domains_input', [])
                    remaining = [d for d in all_domains if d not in completed]

                    if not remaining:
                        # Nothing left — just mark completed on disk
                        snapshot['status'] = 'completed'
                        snapshot['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        persist_process(username, snapshot)
                        continue

                    with process_lock:
                        user_processes[process_key] = {
                            'key': process_key,
                            'type': process_type,
                            'username': username,
                            'status': 'starting',
                            'progress': snapshot.get('progress', 0),
                            'total': snapshot.get('total', len(all_domains)),
                            'results': list(snapshot.get('results', [])),
                            'started_at': snapshot.get('started_at', ''),
                            'finished_at': '',
                            'stop_flag': False,
                            'params': params,
                            'domains_input': all_domains,
                            'completed_domains': completed,
                            'deleted': False,
                            'deleted_at': '',
                        }

                    thread = _launch_process_thread(process_key, process_type, remaining, params)
                    if thread:
                        thread.start()
                        logger.info('Auto-resumed %s process %s for %s (%d domains remaining)',
                                    process_type, process_key, username, len(remaining))
                    else:
                        with process_lock:
                            if process_key in user_processes:
                                del user_processes[process_key]
                        logger.warning('Unknown process type %s for key %s — left as interrupted',
                                       process_type, process_key)
                except Exception as e:
                    logger.error('Auto-resume failed for process %s (user %s): %s',
                                 snapshot.get('key', '?'), username, e)
        except Exception as e:
            logger.error('Recovery error for %s: %s', username, e)


# ── SPF / DNS helpers ──────────────────────────────────────────────────────────

def get_spf_record(domain):
    try:
        answers = resolver.resolve(domain, 'TXT')
        for rdata in answers:
            txt = b''.join(rdata.strings).decode()
            if txt.lower().startswith('v=spf1'):
                return txt
    except:
        pass
    return None


def is_macro_pattern(domain):
    return '%{' in domain


def extract_base_domain_for_macro(macro_domain):
    parts = macro_domain.split('.')
    clean_parts = [p for p in parts if '%{' not in p]
    if clean_parts:
        return '.'.join(clean_parts)
    return None


def is_ip_address(value):
    """Return True if value is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def has_subdomain(domain):
    """Return True only if the domain has a subdomain component (uses tld library)."""
    try:
        res = get_tld(domain, as_object=True)
        return bool(res.subdomain)
    except:
        return False


def get_main_domain(domain):
    # Never try to extract a root domain from an IP address
    if is_ip_address(domain):
        return None
    try:
        if is_macro_pattern(domain):
            base = extract_base_domain_for_macro(domain)
            if base:
                if is_ip_address(base):
                    return None
                res = get_tld(base, as_object=True)
                return f"{res.domain}.{res.suffix}"
            return None
        res = get_tld(domain, as_object=True)
        return f"{res.domain}.{res.suffix}"
    except:
        # Fallback only for non-IP values
        if is_ip_address(domain):
            return None
        parts = domain.split('.')
        if len(parts) >= 2:
            return '.'.join(parts[-2:])
        return domain


def extract_includes(spf_record):
    includes = []
    parts = spf_record.split()
    for part in parts:
        if part.startswith("include:"):
            includes.append(part.split("include:")[1])
        elif part.startswith("redirect="):
            includes.append(part.split("redirect=")[1])
    return includes


def check_includes_recursive(domain, visited=None, depth=0, path=None, root_domain=None):
    if visited is None:
        visited = set()
    if path is None:
        path = [domain]
    if root_domain is None:
        root_domain = domain
    if depth > MAX_DEPTH:
        return []
    if domain in visited:
        return []
    visited.add(domain)

    spf_record = get_spf_record(domain)
    if not spf_record:
        return []

    broken = []
    includes = extract_includes(spf_record)

    for inc in includes:
        # Skip raw IP addresses – they are not domains and cannot be recursed into
        if is_ip_address(inc):
            continue

        if is_macro_pattern(inc):
            base_domain = extract_base_domain_for_macro(inc)
            if base_domain:
                if is_ip_address(base_domain):
                    continue
                main_inc = get_main_domain(base_domain)
                if main_inc:
                    inc_spf = get_spf_record(main_inc)
                    current_path = path + [inc, main_inc]
                    if not inc_spf:
                        broken.append({'domain': main_inc, 'path': list(current_path), 'source': root_domain})
                    else:
                        broken.extend(check_includes_recursive(main_inc, visited, depth + 1, path + [inc, main_inc], root_domain))
            else:
                broken.extend(check_includes_recursive(inc, visited, depth + 1, path + [inc], root_domain))
        else:
            # Use tld library to properly detect subdomains (handles multi-part TLDs like .com.br)
            is_subdomain = has_subdomain(inc)

            if is_subdomain:
                subdomain_spf = get_spf_record(inc)
                main_inc = get_main_domain(inc)
                if not main_inc:
                    continue
                main_spf = get_spf_record(main_inc)

                if not subdomain_spf and not main_spf:
                    current_path = path + [inc, main_inc]
                    broken.append({'domain': main_inc, 'path': list(current_path), 'source': root_domain})
                else:
                    if subdomain_spf:
                        broken.extend(check_includes_recursive(inc, visited, depth + 1, path + [inc], root_domain))
            else:
                main_inc = get_main_domain(inc)
                if not main_inc:
                    continue
                inc_spf = get_spf_record(main_inc)
                current_path = path + [inc]
                if inc != main_inc:
                    current_path = path + [inc, main_inc]
                if not inc_spf:
                    broken.append({'domain': main_inc, 'path': list(current_path), 'source': root_domain})
                else:
                    broken.extend(check_includes_recursive(inc, visited, depth + 1, path + [inc], root_domain))

    return broken


def extract_a_directives(spf_record):
    a_domains = []
    parts = spf_record.split()
    for part in parts:
        lower = part.lower()
        if lower.startswith('a:'):
            a_domain = part.split(':', 1)[1]
            if '/' in a_domain:
                a_domain = a_domain.split('/')[0]
            a_domains.append(a_domain)
    return a_domains


def get_cname_records(domain):
    try:
        answers = resolver.resolve(domain, 'CNAME')
        return [str(rdata.target).rstrip('.') for rdata in answers]
    except:
        return []


# ── Process count helpers ──────────────────────────────────────────────────────

def get_active_process_count(username):
    with process_lock:
        count = 0
        for key, proc in user_processes.items():
            if proc.get('username') == username and proc.get('status') in ('running', 'starting'):
                count += 1
        return count


def can_start_process(username, has_unlimited, max_allowed=1):
    if max_allowed == 0:
        return False
    if has_unlimited:
        return True
    return get_active_process_count(username) < max_allowed


def get_process_key(username, process_type):
    return f"{username}_{process_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}"


# ── User process list ──────────────────────────────────────────────────────────

def get_user_processes(username):
    """Return all non-deleted processes (live + persisted) for a user."""
    live = []
    with process_lock:
        for key, proc in user_processes.items():
            if proc.get('username') == username:
                live.append(_make_snapshot(key, proc))

    live_keys = {p['key'] for p in live}

    history = load_persisted_processes(username)
    for p in history:
        if p['key'] not in live_keys and not p.get('deleted', False):
            live.append(p)

    live.sort(key=lambda x: x.get('started_at', ''), reverse=True)
    return live


# ── Process runners ────────────────────────────────────────────────────────────

def _periodic_persist(process_key, username, last_persist_ref, interval=30):
    """Save snapshot to disk if interval has elapsed. Returns updated timestamp."""
    now = time.time()
    if now - last_persist_ref[0] >= interval:
        last_persist_ref[0] = now
        proc = user_processes.get(process_key)
        if proc:
            snap = _make_snapshot(process_key, proc)
            snap['status'] = 'running'
            persist_process(username, snap)


def run_include_process(process_key, domains):
    proc = user_processes.get(process_key)
    if not proc:
        return
    username = proc.get('username', '')
    try:
        proc['status'] = 'running'
        proc['total'] = proc.get('total', len(domains))

        _last_persist = [time.time()]

        # Restore existing results dict keyed by domain to avoid duplicates
        existing_results = {r['domain']: r for r in proc.get('results', []) if isinstance(r, dict)}
        all_broken = dict(existing_results)

        for i, domain in enumerate(domains):
            if proc.get('stop_flag'):
                break
            try:
                main_spf = get_spf_record(domain)
                if main_spf:
                    broken_list = check_includes_recursive(domain)
                    for item in broken_list:
                        d = item['domain']
                        if d == domain:
                            continue
                        if d not in all_broken:
                            all_broken[d] = {
                                'domain': d,
                                'path': item['path'],
                                'source': item['source'],
                                'type': 'include'
                            }
            except Exception as e:
                logger.warning('include process error on domain %s: %s', domain, e)
            proc['progress'] = proc.get('progress', 0) + 1
            proc['results'] = sorted(all_broken.values(), key=lambda x: x['domain'])

            # Track completed domains
            cd = proc.setdefault('completed_domains', set())
            if isinstance(cd, list):
                cd = set(cd)
                proc['completed_domains'] = cd
            cd.add(domain)

            _periodic_persist(process_key, username, _last_persist)
            time.sleep(0.05)

        proc['results'] = sorted(all_broken.values(), key=lambda x: x['domain'])
        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
    except Exception as e:
        logger.error('run_include_process crashed for key %s: %s', process_key, e, exc_info=True)
        try:
            proc['status'] = 'interrupted'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
        except Exception:
            pass


def run_a_records_process(process_key, domains):
    proc = user_processes.get(process_key)
    if not proc:
        return
    username = proc.get('username', '')
    try:
        proc['status'] = 'running'
        proc['total'] = proc.get('total', len(domains))

        _last_persist = [time.time()]

        existing_results = {r['domain']: r for r in proc.get('results', []) if isinstance(r, dict)}
        all_broken = dict(existing_results)

        for i, domain in enumerate(domains):
            if proc.get('stop_flag'):
                break
            try:
                spf_record = get_spf_record(domain)
                if spf_record:
                    a_domains = extract_a_directives(spf_record)
                    for a_dom in a_domains:
                        a_spf = get_spf_record(a_dom)
                        if not a_spf:
                            if a_dom not in all_broken:
                                all_broken[a_dom] = {
                                    'domain': a_dom,
                                    'path': [domain, 'a:' + a_dom],
                                    'source': domain,
                                    'type': 'a_records'
                                }
            except Exception as e:
                logger.warning('a_records process error on domain %s: %s', domain, e)
            proc['progress'] = proc.get('progress', 0) + 1
            proc['results'] = sorted(all_broken.values(), key=lambda x: x['domain'])

            cd = proc.setdefault('completed_domains', set())
            if isinstance(cd, list):
                cd = set(cd)
                proc['completed_domains'] = cd
            cd.add(domain)

            _periodic_persist(process_key, username, _last_persist)
            time.sleep(0.05)

        proc['results'] = sorted(all_broken.values(), key=lambda x: x['domain'])
        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
    except Exception as e:
        logger.error('run_a_records_process crashed for key %s: %s', process_key, e, exc_info=True)
        try:
            proc['status'] = 'interrupted'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
        except Exception:
            pass


def check_spf_for_ip(domain, ip):
    sender = f"check@{domain}"
    helo = domain
    try:
        result, explanation = spf.check2(i=ip, s=sender, h=helo, timeout=10)
        return result, explanation
    except Exception as e:
        return 'error', str(e)


def run_ip_process(process_key, domains, target_ips):
    proc = user_processes.get(process_key)
    if not proc:
        return
    username = proc.get('username', '')
    try:
        proc['status'] = 'running'
        proc['total'] = proc.get('total', len(domains))

        _last_persist = [time.time()]

        valid_ips = []
        for ip in target_ips:
            try:
                socket.inet_pton(socket.AF_INET, ip)
                valid_ips.append(ip)
            except OSError:
                try:
                    socket.inet_pton(socket.AF_INET6, ip)
                    valid_ips.append(ip)
                except OSError:
                    continue

        if not valid_ips:
            proc['status'] = 'completed'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
            return

        def check_domain(domain):
            if proc.get('stop_flag'):
                return None
            try:
                domain_passes = []
                for ip in valid_ips:
                    if proc.get('stop_flag'):
                        return None
                    result, explanation = check_spf_for_ip(domain, ip)
                    if result == 'pass':
                        domain_passes.append({'domain': domain, 'ip': ip, 'result': 'pass', 'type': 'ip'})
                return domain_passes if domain_passes else None
            except Exception as e:
                logger.warning('ip process check_domain error on %s: %s', domain, e)
                return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(check_domain, d): d for d in domains}
            for future in as_completed(futures):
                if proc.get('stop_flag'):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                proc['progress'] = proc.get('progress', 0) + 1
                domain = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.warning('ip process future error on %s: %s', domain, e)
                    result = None
                if result:
                    with process_lock:
                        proc['results'].extend(result)

                cd = proc.setdefault('completed_domains', set())
                if isinstance(cd, list):
                    cd = set(cd)
                    proc['completed_domains'] = cd
                cd.add(domain)

                _periodic_persist(process_key, username, _last_persist)

        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
    except Exception as e:
        logger.error('run_ip_process crashed for key %s: %s', process_key, e, exc_info=True)
        try:
            proc['status'] = 'interrupted'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
        except Exception:
            pass


def run_cname_process(process_key, domains):
    proc = user_processes.get(process_key)
    if not proc:
        return
    username = proc.get('username', '')
    try:
        proc['status'] = 'running'
        proc['total'] = proc.get('total', len(domains))

        _last_persist = [time.time()]

        # Build seen set from existing results to avoid duplicates on resume
        seen = set()
        for r in proc.get('results', []):
            if isinstance(r, dict):
                seen.add((r.get('domain', ''), r.get('cname', '')))

        def check_domain(domain):
            if proc.get('stop_flag'):
                return []
            try:
                cnames = get_cname_records(domain)
                if not cnames:
                    return []
                hits = []
                for cname in cnames:
                    if proc.get('stop_flag'):
                        break
                    try:
                        main_cname = get_main_domain(cname)
                        if not main_cname:
                            continue
                        spf_record = get_spf_record(main_cname)
                        if not spf_record:
                            key = (domain, main_cname)
                            if key not in seen:
                                seen.add(key)
                                hits.append({'domain': domain, 'cname': main_cname, 'type': 'cname'})
                    except Exception as e:
                        logger.warning('cname process inner error on %s -> %s: %s', domain, cname, e)
                return hits
            except Exception as e:
                logger.warning('cname process check_domain error on %s: %s', domain, e)
                return []

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(check_domain, d): d for d in domains}
            for future in as_completed(futures):
                if proc.get('stop_flag'):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                proc['progress'] = proc.get('progress', 0) + 1
                domain = futures[future]
                try:
                    hits = future.result()
                except Exception as e:
                    logger.warning('cname process future error on %s: %s', domain, e)
                    hits = []
                if hits:
                    with process_lock:
                        for h in hits:
                            proc['results'].append(h)
                        proc['results'].sort(key=lambda x: (x['domain'], x['cname']))

                cd = proc.setdefault('completed_domains', set())
                if isinstance(cd, list):
                    cd = set(cd)
                    proc['completed_domains'] = cd
                cd.add(domain)

                _periodic_persist(process_key, username, _last_persist)

        proc['results'].sort(key=lambda x: (x['domain'], x['cname']))
        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
    except Exception as e:
        logger.error('run_cname_process crashed for key %s: %s', process_key, e, exc_info=True)
        try:
            proc['status'] = 'interrupted'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
        except Exception:
            pass


def run_query_process(process_key, domains, queries):
    proc = user_processes.get(process_key)
    if not proc:
        return
    username = proc.get('username', '')
    try:
        proc['status'] = 'running'
        proc['total'] = proc.get('total', len(domains))

        _last_persist = [time.time()]

        # Normalise queries to a list
        if isinstance(queries, str):
            queries = [queries]
        queries_lower = [q.lower() for q in queries]

        # Build existing domain set to avoid duplicates on resume
        existing_domains = {r['domain'] for r in proc.get('results', []) if isinstance(r, dict)}

        for i, domain in enumerate(domains):
            if proc.get('stop_flag'):
                break
            try:
                spf_record = get_spf_record(domain)
                if spf_record:
                    spf_lower = spf_record.lower()
                    matched = [q for q, ql in zip(queries, queries_lower) if ql in spf_lower]
                    if matched and domain not in existing_domains:
                        with process_lock:
                            proc['results'].append({
                                'domain': domain,
                                'spf': spf_record,
                                'query': ', '.join(matched),
                                'matched_queries': matched,
                                'type': 'query'
                            })
                        existing_domains.add(domain)
            except Exception as e:
                logger.warning('query process error on domain %s: %s', domain, e)
            proc['progress'] = proc.get('progress', 0) + 1

            cd = proc.setdefault('completed_domains', set())
            if isinstance(cd, list):
                cd = set(cd)
                proc['completed_domains'] = cd
            cd.add(domain)

            _periodic_persist(process_key, username, _last_persist)
            time.sleep(0.02)

        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
    except Exception as e:
        logger.error('run_query_process crashed for key %s: %s', process_key, e, exc_info=True)
        try:
            proc['status'] = 'interrupted'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
        except Exception:
            pass


# ── Public start / control API ─────────────────────────────────────────────────

def start_process(username, process_type, domains, has_unlimited, params=None, max_allowed=1):
    if not can_start_process(username, has_unlimited, max_allowed):
        if max_allowed == 0:
            return None, 'You are not allowed to launch processes. Contact your administrator.'
        return None, f'You have reached your process limit ({max_allowed}). Stop an existing process first.'

    if not domains:
        return None, 'No domains available. Please add domains first.'

    process_key = get_process_key(username, process_type)

    with process_lock:
        user_processes[process_key] = {
            'key': process_key,
            'type': process_type,
            'username': username,
            'status': 'starting',
            'progress': 0,
            'total': len(domains),
            'results': [],
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': '',
            'stop_flag': False,
            'params': params or {},
            'domains_input': list(domains),
            'completed_domains': set(),
            'deleted': False,
            'deleted_at': '',
        }

    if process_type == 'include':
        thread = threading.Thread(target=run_include_process, args=(process_key, domains), daemon=True)
    elif process_type == 'a_records':
        thread = threading.Thread(target=run_a_records_process, args=(process_key, domains), daemon=True)
    elif process_type == 'ip':
        target_ips = params.get('ips', []) if params else []
        if not target_ips:
            with process_lock:
                del user_processes[process_key]
            return None, 'Please provide at least one IP address.'
        thread = threading.Thread(target=run_ip_process, args=(process_key, domains, target_ips), daemon=True)
    elif process_type == 'query':
        queries = params.get('queries', []) if params else []
        if isinstance(queries, str):
            queries = [queries]
        # backward-compat: old single-query param
        if not queries and params and params.get('query'):
            queries = [params['query']]
        queries = [q.strip() for q in queries if q.strip()]
        if len(queries) < 1:
            with process_lock:
                del user_processes[process_key]
            return None, 'Please provide at least one search query.'
        thread = threading.Thread(target=run_query_process, args=(process_key, domains, queries), daemon=True)
    elif process_type == 'cname':
        thread = threading.Thread(target=run_cname_process, args=(process_key, domains), daemon=True)
    else:
        with process_lock:
            del user_processes[process_key]
        return None, 'Invalid process type.'

    # Persist immediately so the process survives app restarts before the first periodic persist
    with process_lock:
        snap = _make_snapshot(process_key, user_processes[process_key])
    persist_process(username, snap)

    thread.start()
    return process_key, None


def stop_process(username, process_key):
    with process_lock:
        if process_key in user_processes and user_processes[process_key].get('username') == username:
            proc = user_processes[process_key]
            if proc.get('status') in ('running', 'starting'):
                proc['stop_flag'] = True
            return True
    return False


def delete_process(username, process_key):
    """
    Soft-delete: marks as deleted for normal users.
    Process remains visible to admins in the archived section.
    """
    with process_lock:
        proc = user_processes.get(process_key)
        if proc:
            if proc.get('username') != username:
                return False
            if proc.get('status') == 'running':
                proc['stop_flag'] = True
            snapshot = _make_snapshot(process_key, proc)
            del user_processes[process_key]
        else:
            snapshot = None

    # Mark deleted in persisted storage
    history = load_persisted_processes(username)
    found = False
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for p in history:
        if p['key'] == process_key:
            p['deleted'] = True
            p['deleted_at'] = now_str
            found = True
            break

    if not found and snapshot:
        snapshot['deleted'] = True
        snapshot['deleted_at'] = now_str
        history.insert(0, snapshot)
        found = True

    if found:
        save_persisted_processes(username, history)
        return True
    return False


def resume_process(username, process_key, has_unlimited):
    """
    Resume an interrupted or manually-stopped process from where it left off.
    Skips already-completed domains.
    """
    with process_lock:
        proc = user_processes.get(process_key)
        if proc and proc.get('username') == username:
            if proc.get('status') in ('running', 'starting'):
                return False, 'Process is already running.'
            # Stopped/completed in memory — remove so we reload cleanly from disk
            del user_processes[process_key]

    if not can_start_process(username, has_unlimited):
        return False, 'You already have a running process.'

    history = load_persisted_processes(username)
    snapshot = next((p for p in history if p['key'] == process_key), None)
    if not snapshot:
        return False, 'Process not found.'
    if snapshot.get('deleted', False):
        return False, 'Process has been deleted.'
    if snapshot['status'] not in ('interrupted', 'stopped'):
        return False, f"Cannot resume a '{snapshot['status']}' process."

    completed = set(snapshot.get('completed_domains', []))
    all_domains = snapshot.get('domains_input', [])
    remaining = [d for d in all_domains if d not in completed]

    if not remaining:
        snapshot['status'] = 'completed'
        snapshot['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, snapshot)
        return False, 'All domains already processed. Process marked as completed.'

    process_type = snapshot.get('type', '')
    params = snapshot.get('params', {})

    with process_lock:
        user_processes[process_key] = {
            'key': process_key,
            'type': process_type,
            'username': username,
            'status': 'starting',
            'progress': snapshot.get('progress', 0),
            'total': snapshot.get('total', len(all_domains)),
            'results': list(snapshot.get('results', [])),
            'started_at': snapshot.get('started_at', ''),
            'finished_at': '',
            'stop_flag': False,
            'params': params,
            'domains_input': all_domains,
            'completed_domains': completed,
            'deleted': False,
            'deleted_at': '',
        }

    if process_type == 'include':
        thread = threading.Thread(target=run_include_process, args=(process_key, remaining), daemon=True)
    elif process_type == 'a_records':
        thread = threading.Thread(target=run_a_records_process, args=(process_key, remaining), daemon=True)
    elif process_type == 'ip':
        target_ips = params.get('ips', [])
        thread = threading.Thread(target=run_ip_process, args=(process_key, remaining, target_ips), daemon=True)
    elif process_type == 'query':
        queries = params.get('queries', [])
        if isinstance(queries, str):
            queries = [queries]
        if not queries and params.get('query'):
            queries = [params['query']]
        queries = [q.strip() for q in queries if q.strip()]
        thread = threading.Thread(target=run_query_process, args=(process_key, remaining, queries), daemon=True)
    elif process_type == 'cname':
        thread = threading.Thread(target=run_cname_process, args=(process_key, remaining), daemon=True)
    else:
        with process_lock:
            del user_processes[process_key]
        return False, 'Invalid process type.'

    thread.start()
    return True, None


# ── Admin helpers ──────────────────────────────────────────────────────────────

def get_all_processes_admin():
    """
    Return ALL processes from ALL users (live + persisted + deleted).
    """
    all_procs = {}

    with process_lock:
        for key, proc in user_processes.items():
            all_procs[key] = _make_snapshot(key, proc)

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
                        if p.get('deleted') and not all_procs[k].get('deleted'):
                            all_procs[k]['deleted'] = True
                            all_procs[k]['deleted_at'] = p.get('deleted_at', '')
            except Exception as e:
                logger.error('Admin scan error for %s: %s', username, e)

    return sorted(all_procs.values(), key=lambda x: x.get('started_at', ''), reverse=True)


def admin_stop_process(process_key):
    """Admin can stop any active process regardless of owner."""
    with process_lock:
        proc = user_processes.get(process_key)
        if not proc:
            return False
        if proc.get('status') in ('running', 'starting'):
            proc['stop_flag'] = True
            return True
    return False


def admin_hard_delete_process(process_key):
    """Permanently remove a process (admin only).

    Returns True if the process was removed from memory and/or any
    persisted file. Previously this only returned True when the process
    was found in a persisted file, which made archived rows that lived
    only in memory (or had already been cleaned out) report 'not found'
    even though they had been removed.
    """
    removed_in_memory = False
    with process_lock:
        if process_key in user_processes:
            del user_processes[process_key]
            removed_in_memory = True
    removed_persisted = hard_delete_persisted_process_by_key(process_key)
    return removed_in_memory or removed_persisted


# ── Namecheap domain availability checker ─────────────────────────────────────

def _extract_registrable_domain(domain):
    """Returns (registrable_domain, is_valid, message)."""
    original = (domain or '').strip().lower()
    if not original:
        return (original, False, 'Empty domain')
    try:
        ext = _tld_extractor(original)
        if not ext.suffix:
            return (original, False, 'Invalid TLD or domain format')
        if ext.domain and ext.suffix:
            registrable = f"{ext.domain}.{ext.suffix}"
            if ext.subdomain:
                return (registrable, True, f"Subdomain detected: using parent domain '{registrable}'")
            return (registrable, True, 'Valid domain')
        return (original, False, f"Could not extract valid domain from '{original}'")
    except Exception as e:
        return (original, False, f'Error parsing domain: {e}')


def _get_public_ipv4(force_refresh=False):
    """Cache the public IPv4 (Namecheap requires it). 1-hour TTL."""
    with _public_ip_lock:
        now = time.time()
        cached = _public_ip_cache.get('ip')
        fetched_at = _public_ip_cache.get('fetched_at', 0)
        if cached and not force_refresh and (now - fetched_at) < 3600:
            return cached
        for url in ('https://api.ipify.org?format=json', 'https://ip4.seeip.org'):
            try:
                r = requests.get(url, timeout=5)
                if r.ok:
                    ip = r.json()['ip'] if url.endswith('json') else r.text.strip()
                    if ip and '.' in ip and ':' not in ip:
                        _public_ip_cache['ip'] = ip
                        _public_ip_cache['fetched_at'] = now
                        return ip
            except Exception as e:
                logger.warning('IP lookup failed at %s: %s', url, e)
                continue
        return cached


def _check_availability_batch(domains, client_ip, bad_tlds=None):
    """
    Call Namecheap API for up to 50 domains. Returns (results_dict, bad_tlds_set).
    Namecheap rejects the WHOLE batch if any single domain has an unsupported TLD.
    To survive that, we:
      - skip domains whose TLD we already learned is unsupported (in `bad_tlds`)
      - if Namecheap rejects with "Tld for 'X' is not found", learn that TLD,
        mark matching domains as Invalid TLD, and retry the rest.
    """
    if bad_tlds is None:
        bad_tlds = set()
    results = {}
    if not domains:
        return results, bad_tlds

    remaining = []
    for d in domains:
        ext = _tld_extractor(d)
        suffix = (ext.suffix or '').lower()
        if suffix and suffix in bad_tlds:
            results[d] = f'Invalid TLD (.{suffix})'
        else:
            remaining.append(d)

    while remaining:
        params = {
            'ApiUser': NAMECHEAP_API_USER,
            'ApiKey': NAMECHEAP_API_KEY,
            'UserName': NAMECHEAP_API_USER,
            'Command': 'namecheap.domains.check',
            'ClientIp': client_ip,
            'DomainList': ','.join(remaining),
        }
        try:
            r = requests.get(NAMECHEAP_API_URL, params=params, timeout=30)
            if r.status_code != 200:
                for d in remaining:
                    results[d] = f'Error: HTTP {r.status_code}'
                return results, bad_tlds
            parsed = _parse_availability_response(r.text, remaining)
        except requests.exceptions.RequestException as e:
            for d in remaining:
                results[d] = f'Error: {e}'
            return results, bad_tlds

        statuses = list(parsed.values())
        all_api_error = bool(statuses) and all((s or '').startswith('API Error:') for s in statuses)
        if all_api_error:
            error_msg = statuses[0]
            m = re.search(r"Tld for '([^']+)' is not found", error_msg)
            if m:
                bad = m.group(1).lower()
                bad_tlds.add(bad)
                bad_set = {d for d in remaining if d.lower() == bad or d.lower().endswith('.' + bad)}
                if not bad_set:
                    for d in remaining:
                        results[d] = error_msg
                    return results, bad_tlds
                for d in bad_set:
                    results[d] = f'Invalid TLD (.{bad})'
                remaining = [d for d in remaining if d not in bad_set]
                if remaining:
                    time.sleep(NAMECHEAP_DELAY_SECONDS)
                continue
            for d in remaining:
                results[d] = error_msg
            return results, bad_tlds

        results.update(parsed)
        return results, bad_tlds

    return results, bad_tlds


def _parse_availability_response(xml_text, domains):
    results = {}
    try:
        root = ET.fromstring(xml_text)
        errors = root.findall('.//Error') + root.findall('.//{http://api.namecheap.com/xml.response}Error')
        if errors:
            err_text = errors[0].text or 'Unknown API error'
            return {d: f'API Error: {err_text}' for d in domains}
        cmd = root.find('.//nc:CommandResponse', NAMECHEAP_NS) or root.find('.//CommandResponse')
        if cmd is None:
            return {d: 'Error: Invalid API response' for d in domains}
        items = cmd.findall('.//nc:DomainCheckResult', NAMECHEAP_NS)
        if not items:
            items = cmd.findall('.//DomainCheckResult')
        for it in items:
            d = it.get('Domain', '')
            available = (it.get('Available', 'false') or 'false').lower() == 'true'
            if available:
                results[d] = 'Available'
            else:
                reason = it.get('Description') or it.get('Reason') or it.get('Message') or ''
                results[d] = f'Not Available - {reason}' if reason else 'Not Available'
        for d in domains:
            results.setdefault(d, 'No response from API')
    except ET.ParseError as e:
        return {d: f'Error: Invalid XML ({e})' for d in domains}
    return results


def _build_availability_pairs(proc):
    """
    Normalize a process's results into [{main, found, registrable, valid, message}].
    Only the 'found' (Domain found with no SPF) is registered with Namecheap.
    """
    ptype = proc.get('type', '')
    out = []
    for r in proc.get('results', []) or []:
        if not isinstance(r, dict):
            continue
        if ptype in ('include', 'a_records'):
            main = r.get('source') or ''
            found = r.get('domain') or ''
        elif ptype == 'cname':
            main = r.get('domain') or ''
            found = r.get('cname') or ''
        else:
            continue
        if not found:
            continue
        registrable, is_valid, message = _extract_registrable_domain(found)
        out.append({
            'main': main,
            'found': found,
            'registrable': registrable,
            'valid': is_valid,
            'message': message,
            'status': '',
        })
    return out


def _run_availability_check(username, process_key):
    with process_lock:
        proc = user_processes.get(process_key)
    if not proc:
        return

    pairs = _build_availability_pairs(proc)
    valid_pairs = [p for p in pairs if p['valid']]
    invalid_pairs = [p for p in pairs if not p['valid']]
    for p in invalid_pairs:
        p['status'] = 'Invalid'

    unique_targets = list(dict.fromkeys(p['registrable'] for p in valid_pairs))

    avail = proc.setdefault('availability', {})
    avail['total'] = len(unique_targets)
    avail['progress'] = 0
    avail['pairs'] = pairs

    if not unique_targets:
        avail['status'] = 'completed'
        avail['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        avail['available_count'] = 0
        persist_process(username, _make_snapshot(process_key, proc))
        return

    client_ip = _get_public_ipv4()
    if not client_ip:
        avail['status'] = 'error'
        avail['error'] = 'Could not determine public IPv4 address (required by Namecheap API)'
        avail['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        persist_process(username, _make_snapshot(process_key, proc))
        return

    api_results = {}
    bad_tlds = set()
    batches = [unique_targets[i:i + NAMECHEAP_BATCH_SIZE] for i in range(0, len(unique_targets), NAMECHEAP_BATCH_SIZE)]
    for i, batch in enumerate(batches):
        if proc.get('availability_stop'):
            avail['status'] = 'error'
            avail['error'] = 'Stopped by user'
            avail['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            persist_process(username, _make_snapshot(process_key, proc))
            return
        results, bad_tlds = _check_availability_batch(batch, client_ip, bad_tlds)
        api_results.update(results)
        avail['progress'] = min(avail['progress'] + len(batch), avail['total'])
        persist_process(username, _make_snapshot(process_key, proc))
        if i < len(batches) - 1:
            time.sleep(NAMECHEAP_DELAY_SECONDS)

    for p in valid_pairs:
        p['status'] = api_results.get(p['registrable'], 'No response from API')

    available_count = sum(1 for p in pairs if (p.get('status') or '').startswith('Available'))
    avail['available_count'] = available_count
    avail['status'] = 'completed'
    avail['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    persist_process(username, _make_snapshot(process_key, proc))


def start_availability_check(username, process_key):
    """Kick off a Namecheap availability check thread for a finished process."""
    with process_lock:
        proc = user_processes.get(process_key)
        if not proc:
            # Restore from disk if needed
            for p in load_persisted_processes(username):
                if p.get('key') == process_key:
                    p['stop_flag'] = False
                    p['availability_stop'] = False
                    user_processes[process_key] = p
                    proc = p
                    break
        if not proc:
            return False, 'Process not found'
        if proc.get('username') != username:
            return False, 'Permission denied'
        if proc.get('type') not in ('include', 'a_records', 'cname'):
            return False, 'Availability check not supported for this process type'
        if proc.get('status') in ('running', 'starting'):
            return False, 'Wait for the main process to finish first'
        if not proc.get('results'):
            return False, 'No results to check'
        avail = proc.get('availability') or {}
        if avail.get('status') == 'running':
            return False, 'Availability check already running'
        proc['availability'] = {
            'status': 'running',
            'progress': 0,
            'total': 0,
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': '',
            'error': '',
            'available_count': 0,
            'pairs': [],
        }
        proc['availability_stop'] = False

    persist_process(username, _make_snapshot(process_key, proc))
    threading.Thread(target=_run_availability_check, args=(username, process_key), daemon=True).start()
    return True, None


def get_availability_csv(username, process_key):
    """Return (csv_text, filename) for a completed availability check, or (None, error)."""
    with process_lock:
        proc = user_processes.get(process_key)
    if not proc:
        for p in load_persisted_processes(username):
            if p.get('key') == process_key:
                proc = p
                break
    if not proc:
        return None, 'Process not found'
    if proc.get('username') != username:
        return None, 'Permission denied'
    avail = proc.get('availability') or {}
    if avail.get('status') != 'completed':
        return None, 'Availability check not completed'

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Main Domain', 'Domain found with no spf', 'Available status'])
    for p in avail.get('pairs', []) or []:
        writer.writerow([p.get('main', ''), p.get('found', ''), p.get('status', '')])
    filename = f"available_domains_{proc.get('type', 'process')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return buf.getvalue(), filename
