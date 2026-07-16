import os
import json
import logging
import threading
import time
import imaplib
import email
from email.header import decode_header
from datetime import datetime

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

DATA_DIR = 'email_founder_data'
ACCOUNTS_DIR = os.path.join(DATA_DIR, 'accounts')
os.makedirs(DATA_DIR, exist_ok=True)

user_processes = {}
process_lock = threading.Lock()


def _get_user_accounts_file(username):
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    return os.path.join(ACCOUNTS_DIR, f'{username}_accounts.json')


def load_user_accounts(username):
    filepath = _get_user_accounts_file(username)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []


def _save_user_accounts(username, accounts):
    filepath = _get_user_accounts_file(username)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def save_user_account(username, gmail_email, app_password):
    accounts = load_user_accounts(username)
    exists = False
    for acc in accounts:
        if acc['email'] == gmail_email:
            acc['password'] = app_password
            exists = True
            break
    if not exists:
        accounts.append({'email': gmail_email, 'password': app_password})
    _save_user_accounts(username, accounts)


def delete_user_account(username, gmail_email):
    accounts = load_user_accounts(username)
    accounts = [a for a in accounts if a['email'] != gmail_email]
    _save_user_accounts(username, accounts)


def connect_gmail(gmail_email, app_password):
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=30)
        mail.login(gmail_email, app_password)
        return mail, None
    except imaplib.IMAP4.error as e:
        return None, f"Authentication failed: {str(e)}"
    except Exception as e:
        return None, f"Connection error: {str(e)}"


def list_labels(gmail_email, app_password):
    mail, error = connect_gmail(gmail_email, app_password)
    if error:
        return None, error
    try:
        status, folders = mail.list()
        labels = []
        if status == "OK":
            for folder in folders:
                decoded = folder.decode()
                parts = decoded.split(' "/" ')
                if len(parts) == 2:
                    label = parts[1].replace('"', '')
                    labels.append(label)
        mail.logout()
        return labels, None
    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        return None, str(e)


def get_folder_mapping(folder_choice, custom_label=None):
    mapping = {
        'inbox': ('INBOX', None),
        'primary': ('INBOX', 'category:primary'),
        'promotions': ('INBOX', 'category:promotions'),
        'social': ('INBOX', 'category:social'),
        'updates': ('INBOX', 'category:updates'),
        'forums': ('INBOX', 'category:forums'),
        'starred': ('[Gmail]/Starred', None),
        'custom': (custom_label, None) if custom_label else ('INBOX', None),
    }
    return mapping.get(folder_choice, ('INBOX', None))


def get_user_output_dir(username):
    user_dir = os.path.join(DATA_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def get_process_history_file(username):
    return os.path.join(DATA_DIR, f'{username}_history.json')


def load_process_history(username):
    filepath = get_process_history_file(username)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []


def save_process_history(username, history):
    filepath = get_process_history_file(username)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def has_running_process(username):
    for key, proc in user_processes.items():
        if key.startswith(username + '_') and proc.get('status') in ('running', 'starting'):
            return True
    return False


def count_user_history(username):
    history = load_process_history(username)
    return len(history)


def start_extraction(username, gmail_email, app_password, folder_choice, custom_label, range_from, range_to, mode, max_history=10):
    if has_running_process(username):
        return None, "You already have a process running. Please wait for it to finish."

    history_count = count_user_history(username)
    if history_count >= max_history:
        return None, f"You have reached your limit of {max_history} extractions. Please delete some past extractions to continue."

    if range_from < 1:
        range_from = 1
    if range_to < range_from:
        return None, "The 'To' value must be greater than or equal to 'From'."
    if mode not in ('1', '2', '3'):
        return None, "Invalid mode selected."

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    process_key = f"{username}_{timestamp}"

    folder, category_query = get_folder_mapping(folder_choice, custom_label)
    folder_label = custom_label if folder_choice == 'custom' else folder_choice.capitalize()

    mode_labels = {'1': 'Full Email Source', '2': 'Text Plain Only', '3': 'Text HTML Only'}
    mode_label = mode_labels.get(mode, 'Full Email Source')

    output_filename = f"emails_{timestamp}.txt"
    output_path = os.path.join(get_user_output_dir(username), output_filename)

    proc = {
        'key': process_key,
        'username': username,
        'gmail_email': gmail_email,
        'folder': folder,
        'folder_label': folder_label,
        'category_query': category_query,
        'range_from': range_from,
        'range_to': range_to,
        'mode': mode,
        'mode_label': mode_label,
        'status': 'starting',
        'progress': 0,
        'total': 0,
        'output_file': output_filename,
        'output_path': output_path,
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'finished_at': None,
        'error': None,
        'stop_flag': False,
        'email_count': 0,
    }

    with process_lock:
        user_processes[process_key] = proc

    thread = threading.Thread(
        target=run_extraction,
        args=(process_key, gmail_email, app_password, folder, category_query, range_from, range_to, mode, output_path),
        daemon=True
    )
    thread.start()

    return process_key, None


def run_extraction(process_key, gmail_email, app_password, folder, category_query, range_from, range_to, mode, output_path):
    proc = user_processes.get(process_key)
    if not proc:
        return

    try:
        mail, error = connect_gmail(gmail_email, app_password)
        if error:
            proc['status'] = 'error'
            proc['error'] = error
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_to_history(proc)
            return

        proc['status'] = 'running'

        status, _ = mail.select(f'"{folder}"')
        if status != "OK":
            proc['status'] = 'error'
            proc['error'] = f"Could not open folder: {folder}"
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_to_history(proc)
            try:
                mail.logout()
            except:
                pass
            return

        if category_query:
            status, messages = mail.search(None, f'X-GM-RAW "{category_query}"')
        else:
            status, messages = mail.search(None, "ALL")

        email_ids = messages[0].split()
        if not email_ids:
            proc['status'] = 'completed'
            proc['error'] = 'No emails found in this folder.'
            proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_to_history(proc)
            try:
                mail.logout()
            except:
                pass
            return

        email_ids = list(reversed(email_ids))

        actual_from = max(1, range_from)
        actual_to = min(range_to, len(email_ids))
        selected_ids = email_ids[actual_from - 1:actual_to]

        proc['total'] = len(selected_ids)

        results = []
        for idx, eid in enumerate(selected_ids):
            if proc.get('stop_flag'):
                break

            try:
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    proc['progress'] = idx + 1
                    continue

                raw_email = msg_data[0][1]

                if mode == '1':
                    results.append(raw_email.decode(errors="ignore"))
                else:
                    msg = email.message_from_bytes(raw_email)
                    content = _extract_content(msg, mode)
                    if content is not None:
                        results.append(content)

                proc['progress'] = idx + 1
                proc['email_count'] = len(results)

            except Exception as e:
                logging.error(f"Error fetching email {eid}: {e}")
                proc['progress'] = idx + 1
                continue

        try:
            with open(output_path, 'w', encoding='utf-8', newline='') as f:
                for i, content in enumerate(results):
                    f.write(content.rstrip('\r\n'))
                    if i != len(results) - 1:
                        f.write("\n__SEP__\n")
        except Exception as e:
            logging.error(f"Error writing output file: {e}")
            proc['error'] = f"Error saving file: {str(e)}"

        proc['email_count'] = len(results)
        proc['status'] = 'stopped' if proc.get('stop_flag') else 'completed'
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            mail.logout()
        except:
            pass

    except Exception as e:
        logging.error(f"Extraction error: {e}")
        proc['status'] = 'error'
        proc['error'] = str(e)
        proc['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    _save_to_history(proc)


def _extract_content(msg, mode):
    found = False
    content = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" in content_disposition:
                continue

            if mode == "2" and content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    content = payload.decode(errors="ignore")
                    found = True
                    break
            elif mode == "3" and content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    content = payload.decode(errors="ignore")
                    found = True
                    break
    else:
        # Single-part message: respect the requested mode. If the user asked for
        # plain text only (mode 2) but the email is HTML (or vice-versa), skip it
        # instead of returning the wrong content type.
        content_type = msg.get_content_type()
        if mode == "2" and content_type != "text/plain":
            return None
        if mode == "3" and content_type != "text/html":
            return None

        payload = msg.get_payload(decode=True)
        if payload:
            content = payload.decode(errors="ignore")
            found = True

    if not found:
        return None

    return content


def _save_to_history(proc):
    username = proc['username']
    history = load_process_history(username)
    entry = {
        'key': proc['key'],
        'gmail_email': proc['gmail_email'],
        'folder_label': proc['folder_label'],
        'mode_label': proc['mode_label'],
        'range_from': proc['range_from'],
        'range_to': proc['range_to'],
        'status': proc['status'],
        'email_count': proc.get('email_count', 0),
        'output_file': proc['output_file'],
        'started_at': proc['started_at'],
        'finished_at': proc['finished_at'],
        'error': proc.get('error'),
    }
    existing_idx = None
    for i, h in enumerate(history):
        if h['key'] == proc['key']:
            existing_idx = i
            break
    if existing_idx is not None:
        history[existing_idx] = entry
    else:
        history.insert(0, entry)

    history = history[:50]
    save_process_history(username, history)


def get_user_processes(username):
    running = []
    for key, proc in user_processes.items():
        if key.startswith(username + '_'):
            running.append({
                'key': proc['key'],
                'gmail_email': proc['gmail_email'],
                'folder_label': proc['folder_label'],
                'mode_label': proc['mode_label'],
                'range_from': proc['range_from'],
                'range_to': proc['range_to'],
                'status': proc['status'],
                'progress': proc['progress'],
                'total': proc['total'],
                'email_count': proc.get('email_count', 0),
                'output_file': proc['output_file'],
                'started_at': proc['started_at'],
                'finished_at': proc['finished_at'],
                'error': proc.get('error'),
            })
    return running


def stop_process(username, key):
    proc = user_processes.get(key)
    if proc and key.startswith(username + '_'):
        proc['stop_flag'] = True
        return True
    return False


def delete_process(username, key):
    if key in user_processes and key.startswith(username + '_'):
        proc = user_processes[key]
        if proc['status'] in ('running', 'starting'):
            proc['stop_flag'] = True
        del user_processes[key]
        return True
    return False


def delete_history_entry(username, key):
    history = load_process_history(username)
    output_file = None
    for h in history:
        if h['key'] == key:
            output_file = h.get('output_file')
            break

    history = [h for h in history if h['key'] != key]
    save_process_history(username, history)

    if output_file:
        filepath = os.path.join(get_user_output_dir(username), output_file)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass
    return True


def get_download_path(username, filename):
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename != filename:
        return None
    user_dir = get_user_output_dir(username)
    filepath = os.path.normpath(os.path.join(user_dir, safe_filename))
    if not filepath.startswith(os.path.normpath(user_dir)):
        return None
    if os.path.exists(filepath):
        return filepath
    return None
