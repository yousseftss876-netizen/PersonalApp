import json
import os
import ipaddress
import logging
import threading
from datetime import datetime

IP_CHECKER_DATA_FILE = 'ip_checker_data.json'
IP_CHECKER_EVENTS_FILE = 'ip_checker_events.json'
IP_CHECKER_EVENT_TYPES_FILE = 'ip_checker_event_types.json'

_lock = threading.Lock()

DEFAULT_EVENT_TYPES = ['Available', 'Down']

_SENTINEL = object()

def _load_json(filepath, default=_SENTINEL):
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading {filepath}: {e}")
    if default is _SENTINEL:
        return {}
    return default


def _save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving {filepath}: {e}")


def load_servers():
    return _load_json(IP_CHECKER_DATA_FILE, {})


def save_servers(data):
    with _lock:
        _save_json(IP_CHECKER_DATA_FILE, data)


def load_events():
    return _load_json(IP_CHECKER_EVENTS_FILE, [])


def save_events(events):
    with _lock:
        _save_json(IP_CHECKER_EVENTS_FILE, events)


def load_event_types():
    types = _load_json(IP_CHECKER_EVENT_TYPES_FILE, None)
    if types is None:
        types = DEFAULT_EVENT_TYPES[:]
        _save_json(IP_CHECKER_EVENT_TYPES_FILE, types)
    return types


def save_event_types(types):
    with _lock:
        _save_json(IP_CHECKER_EVENT_TYPES_FILE, types)


def validate_cidr(cidr_str):
    try:
        network = ipaddress.ip_network(cidr_str, strict=False)
        return True, network
    except ValueError as e:
        return False, str(e)


def validate_ip_in_cidr(ip_str, cidr_str):
    try:
        ip = ipaddress.ip_address(ip_str.strip())
        network = ipaddress.ip_network(cidr_str, strict=False)
        return ip in network
    except ValueError:
        return False


def validate_ip_format(ip_str):
    try:
        ipaddress.ip_address(ip_str.strip())
        return True
    except ValueError:
        return False


def add_server_with_class(server_name, cidr, ip_list=None):
    valid, result = validate_cidr(cidr)
    if not valid:
        return False, f"Invalid CIDR: {result}", {}

    data = load_servers()
    is_new_server = server_name not in data

    if is_new_server:
        data[server_name] = {
            'classes': {},
            'created_at': datetime.now().isoformat()
        }

    if cidr in data[server_name]['classes']:
        return False, f"Class '{cidr}' already exists in server '{server_name}'.", {}

    data[server_name]['classes'][cidr] = {
        'ips': [],
        'created_at': datetime.now().isoformat()
    }

    results = {'added': 0, 'invalid': [], 'duplicate': [], 'out_of_range': []}
    if ip_list:
        existing = set()
        added = []
        for ip_str in ip_list:
            ip_str = ip_str.strip()
            if not ip_str:
                continue
            if not validate_ip_format(ip_str):
                results['invalid'].append(ip_str)
                continue
            if ip_str in existing:
                results['duplicate'].append(ip_str)
                continue
            if not validate_ip_in_cidr(ip_str, cidr):
                results['out_of_range'].append(ip_str)
                continue
            existing.add(ip_str)
            added.append(ip_str)
        data[server_name]['classes'][cidr]['ips'] = added
        results['added'] = len(added)

    save_servers(data)
    action = "created" if is_new_server else "updated"
    msg = f"Server '{server_name}' {action} with class '{cidr}'."
    if results['added'] > 0:
        msg += f" {results['added']} IPs added."
    issues = len(results['invalid']) + len(results['duplicate']) + len(results['out_of_range'])
    if issues > 0:
        parts = []
        if results['invalid']:
            parts.append(f"{len(results['invalid'])} invalid")
        if results['duplicate']:
            parts.append(f"{len(results['duplicate'])} duplicate")
        if results['out_of_range']:
            parts.append(f"{len(results['out_of_range'])} out of range")
        msg += f" Skipped: {', '.join(parts)}."
    return True, msg, results


def delete_server(server_name):
    data = load_servers()
    if server_name not in data:
        return False, f"Server '{server_name}' not found."
    del data[server_name]
    save_servers(data)
    events = load_events()
    events = [e for e in events if e.get('server') != server_name]
    save_events(events)
    return True, f"Server '{server_name}' deleted."


def rename_server(old_name, new_name):
    data = load_servers()
    if old_name not in data:
        return False, f"Server '{old_name}' not found."
    if new_name in data:
        return False, f"Server '{new_name}' already exists."
    data[new_name] = data.pop(old_name)
    save_servers(data)
    events = load_events()
    for e in events:
        if e.get('server') == old_name:
            e['server'] = new_name
    save_events(events)
    return True, f"Server renamed to '{new_name}'."


def add_class_to_server(server_name, cidr):
    valid, result = validate_cidr(cidr)
    if not valid:
        return False, f"Invalid CIDR: {result}"
    data = load_servers()
    if server_name not in data:
        return False, f"Server '{server_name}' not found."
    if cidr in data[server_name]['classes']:
        return False, f"Class '{cidr}' already exists in server '{server_name}'."
    data[server_name]['classes'][cidr] = {
        'ips': [],
        'created_at': datetime.now().isoformat()
    }
    save_servers(data)
    return True, f"Class '{cidr}' added to server '{server_name}'."


def delete_class_from_server(server_name, cidr):
    data = load_servers()
    if server_name not in data:
        return False, f"Server '{server_name}' not found."
    if cidr not in data[server_name]['classes']:
        return False, f"Class '{cidr}' not found in server '{server_name}'."
    del data[server_name]['classes'][cidr]
    save_servers(data)
    events = load_events()
    events = [e for e in events if not (e.get('server') == server_name and e.get('cidr') == cidr and e.get('scope') == 'class')]
    save_events(events)
    return True, f"Class '{cidr}' deleted from server '{server_name}'."


def add_ips_to_class(server_name, cidr, ip_list):
    data = load_servers()
    if server_name not in data:
        return False, f"Server '{server_name}' not found.", {}
    if cidr not in data[server_name]['classes']:
        return False, f"Class '{cidr}' not found.", {}

    cls = data[server_name]['classes'][cidr]
    existing = set(cls['ips'])

    added = []
    invalid = []
    duplicate = []
    out_of_range = []

    for ip_str in ip_list:
        ip_str = ip_str.strip()
        if not ip_str:
            continue
        if not validate_ip_format(ip_str):
            invalid.append(ip_str)
            continue
        if ip_str in existing:
            duplicate.append(ip_str)
            continue
        if not validate_ip_in_cidr(ip_str, cidr):
            out_of_range.append(ip_str)
            continue
        existing.add(ip_str)
        added.append(ip_str)

    cls['ips'] = list(existing)
    save_servers(data)

    results = {
        'added': len(added),
        'invalid': invalid,
        'duplicate': duplicate,
        'out_of_range': out_of_range
    }

    msg = f"{len(added)} IPs added."
    issues = len(invalid) + len(duplicate) + len(out_of_range)
    if issues > 0:
        parts = []
        if invalid:
            parts.append(f"{len(invalid)} invalid")
        if duplicate:
            parts.append(f"{len(duplicate)} duplicate")
        if out_of_range:
            parts.append(f"{len(out_of_range)} out of range")
        msg += f" Skipped: {', '.join(parts)}."

    return True, msg, results


def delete_ip_from_class(server_name, cidr, ip_str):
    data = load_servers()
    if server_name not in data:
        return False, f"Server '{server_name}' not found."
    if cidr not in data[server_name]['classes']:
        return False, f"Class '{cidr}' not found."
    cls = data[server_name]['classes'][cidr]
    if ip_str not in cls['ips']:
        return False, f"IP '{ip_str}' not found in class '{cidr}'."
    cls['ips'].remove(ip_str)
    save_servers(data)
    events = load_events()
    events = [e for e in events if not (e.get('scope') == 'ip' and ip_str in e.get('ips', []))]
    save_events(events)
    return True, f"IP '{ip_str}' deleted."


def add_event(server_name, event_type, scope, cidr=None, ips=None, declared_by='system'):
    events = load_events()
    event = {
        'id': f"evt_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        'server': server_name,
        'event_type': event_type,
        'scope': scope,
        'cidr': cidr,
        'ips': ips or [],
        'declared_by': declared_by,
        'timestamp': datetime.now().isoformat(),
    }
    events.insert(0, event)
    save_events(events)
    return True, "Event declared successfully.", event


def delete_event(event_id):
    events = load_events()
    events = [e for e in events if e.get('id') != event_id]
    save_events(events)
    return True, "Event deleted."


def add_custom_event_type(event_name):
    types = load_event_types()
    if event_name in types:
        return False, f"Event type '{event_name}' already exists."
    types.append(event_name)
    save_event_types(types)
    return True, f"Event type '{event_name}' added."


def delete_event_type(event_name):
    if event_name in DEFAULT_EVENT_TYPES:
        return False, f"Cannot delete default event type '{event_name}'."
    types = load_event_types()
    if event_name not in types:
        return False, f"Event type '{event_name}' not found."
    types.remove(event_name)
    save_event_types(types)
    return True, f"Event type '{event_name}' deleted."


def get_latest_status(server_name, cidr=None, ip_str=None):
    events = load_events()
    for e in events:
        if e.get('server') != server_name:
            continue
        if ip_str:
            if e.get('scope') == 'ip' and ip_str in e.get('ips', []):
                return e
            if e.get('scope') == 'class' and e.get('cidr') == cidr:
                return e
            if e.get('scope') == 'server':
                return e
        elif cidr:
            if e.get('scope') == 'class' and e.get('cidr') == cidr:
                return e
            if e.get('scope') == 'server':
                return e
        else:
            if e.get('scope') == 'server':
                return e
    return None


import random

def generate_random_ips(server_names, selected_cidrs, from_idx, to_idx, filter_type='all'):
    data = load_servers()
    all_matching_ips = []

    for server_name in server_names:
        if server_name not in data:
            continue
        
        server_data = data[server_name]
        for cidr in selected_cidrs:
            if cidr not in server_data['classes']:
                continue
            
            cls = server_data['classes'][cidr]
            ips = cls['ips']
            
            if filter_type != 'all':
                filtered = []
                for ip in ips:
                    status = get_latest_status(server_name, cidr, ip)
                    status_type = status['event_type'] if status else 'Available'
                    if status_type == filter_type:
                        filtered.append(ip)
                ips = filtered
                
            all_matching_ips.extend(ips)

    if not all_matching_ips:
        return False, "No IPs found matching the criteria.", ""

    # Sort and remove duplicates if they exist across classes/servers
    all_matching_ips = sorted(list(set(all_matching_ips)))
    unique_count = len(all_matching_ips)
    
    total_requested = (to_idx - from_idx) + 1
    result_ips = []
    
    for i in range(total_requested):
        ip = all_matching_ips[i % unique_count]
        idx = from_idx + i
        result_ips.append(f"{idx}#{ip}:92")

    return True, "Success", "\n".join(result_ips)


def search_ip(ip_str):
    if not validate_ip_format(ip_str):
        return False, f"'{ip_str}' is not a valid IP address.", []

    data = load_servers()
    results = []

    for server_name, server_data in data.items():
        for cidr, cls in server_data['classes'].items():
            if ip_str in cls['ips']:
                status = get_latest_status(server_name, cidr, ip_str)
                results.append({
                    'server': server_name,
                    'cidr': cidr,
                    'status': status,
                    'registered': True
                })
            elif validate_ip_in_cidr(ip_str, cidr):
                results.append({
                    'server': server_name,
                    'cidr': cidr,
                    'status': None,
                    'registered': False
                })

    return True, f"Found {len(results)} match(es).", results
