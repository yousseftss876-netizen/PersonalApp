import os
import json
import random
import re
import string
import asyncio
import threading
import logging
import traceback
from datetime import datetime
from faker import Faker
import uuid

USER_DATA_DIR = "news_subscription_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

SUCCESS_DOMAINS_FILE = "all_successfully_domain.txt"

GLOBAL_BROWSER_SEMAPHORE = threading.Semaphore(3)

def _lower_thread_priority():
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass

def add_successful_domain(domain):
    """Add a domain to the global successful domains list."""
    try:
        domain = domain.lower().strip()
        if domain.startswith('https://'): domain = domain[8:]
        if domain.startswith('http://'): domain = domain[7:]
        
        # Check if already exists to avoid duplicates
        existing = set()
        if os.path.exists(SUCCESS_DOMAINS_FILE):
            with open(SUCCESS_DOMAINS_FILE, 'r', encoding='utf-8') as f:
                existing = {line.strip().lower() for line in f}
        
        if domain not in existing:
            with open(SUCCESS_DOMAINS_FILE, 'a', encoding='utf-8') as f:
                f.write(f"{domain}\n")
    except Exception as e:
        logging.error(f"Error saving successful domain: {e}")

def get_process_state_file(username, process_id="default"):
    return os.path.join(get_user_dir(username), f"state_{process_id}.json")

def save_process_state(username, state):
    state_file = get_process_state_file(username, state.get('id', 'default'))
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

def load_process_state(username, process_id="default"):
    state_file = get_process_state_file(username, process_id)
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    return None

def delete_process_state(username, process_id="default"):
    state_file = get_process_state_file(username, process_id)
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except:
            pass

fake = Faker()

CONFIG = {
    'page_timeout': 30000,
    'element_timeout': 10000,
    'wait_after_click': 3000,
    'wait_between_domains': (3000, 8000),
    'max_retries': 2,
    'navigation_timeout': 30000,
    'form_fill_delay': 800,
    'captcha_wait': 20000,
    'max_concurrent': 2,
    'user_agents': [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]
}

user_processes = {}
user_process_locks = {}

def get_user_lock(username):
    if username not in user_process_locks:
        user_process_locks[username] = threading.Lock()
    return user_process_locks[username]

def get_user_dir(username):
    user_dir = os.path.join(USER_DATA_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def get_user_history_file(username):
    return os.path.join(get_user_dir(username), "process_history.json")

def get_user_process_history(username):
    history_file = get_user_history_file(username)
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []

def save_user_process_history(username, history):
    history_file = get_user_history_file(username)
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2)

def add_process_to_history(username, process_data):
    history = get_user_process_history(username)
    if 'id' not in process_data or not process_data.get('id'):
        process_data['id'] = str(uuid.uuid4())
    process_data.setdefault('deleted', False)
    history.insert(0, process_data)
    save_user_process_history(username, history)

def delete_process_from_history(username, process_id):
    """Soft-delete: mark the entry as deleted but keep in history.
    Hard-delete is reserved for the Processes Management admin page."""
    history = get_user_process_history(username)
    changed = False
    for p in history:
        if p.get('id') == process_id:
            p['deleted'] = True
            p['deleted_at'] = datetime.now().isoformat()
            changed = True
            break
    if changed:
        save_user_process_history(username, history)
    return True

def hard_delete_process_from_history(username, process_id):
    """Permanently remove a process from a user's history."""
    history = get_user_process_history(username)
    new_history = [p for p in history if p.get('id') != process_id]
    if len(new_history) != len(history):
        save_user_process_history(username, new_history)
        return True
    return False

def _list_all_users_with_data():
    try:
        return [d for d in os.listdir(USER_DATA_DIR)
                if os.path.isdir(os.path.join(USER_DATA_DIR, d))]
    except FileNotFoundError:
        return []

def _format_active_process_snapshot(username, p):
    """Convert an in-memory user_processes entry to the management snapshot format."""
    pid = p.get('id', 'default')
    return {
        'id': pid,
        'username': username,
        'email_used': p.get('email'),
        'total_domains_processed': p.get('progress', 0),
        'successful_registrations': p.get('successful', 0),
        'failed_registrations': p.get('failed', 0),
        'success_rate': round((p.get('successful', 0) / max(p.get('progress', 1), 1)) * 100) if p.get('progress', 0) > 0 else 0,
        'created_at': p.get('start_time'),
        'start_time': p.get('start_time'),
        'end_time': None,
        'duration': None,
        'status': 'paused' if p.get('paused') else ('running' if p.get('running') else 'stopped'),
        'progress': p.get('progress', 0),
        'total': p.get('total', 0),
        'deleted': False,
        'is_active': True,
    }

def get_all_processes_admin():
    """Return every News Subscription process across all users (running/finished/paused/deleted)."""
    items = []
    seen_ids = set()

    for username, p in list(user_processes.items()):
        if not isinstance(p, dict):
            continue
        if ':' in username:
            uname = username.split(':', 1)[0]
        else:
            uname = username
        snap = _format_active_process_snapshot(uname, p)
        snap_id = snap.get('id')
        items.append(snap)
        if snap_id:
            seen_ids.add(f"{uname}:{snap_id}")

    for uname in _list_all_users_with_data():
        history = get_user_process_history(uname)
        for entry in history:
            entry_id = entry.get('id')
            key = f"{uname}:{entry_id}" if entry_id else None
            if key and key in seen_ids:
                continue
            row = dict(entry)
            row['username'] = uname
            row.setdefault('deleted', False)
            row['is_active'] = False
            items.append(row)

    def _sort_key(it):
        return it.get('start_time') or it.get('created_at') or ''
    items.sort(key=_sort_key, reverse=True)
    return items

def admin_stop_process(process_id):
    """Stop any running process by id (admin) — searches all in-memory processes."""
    for pid_key, p in list(user_processes.items()):
        if not isinstance(p, dict):
            continue
        if p.get('id') == process_id or pid_key.endswith(f":{process_id}"):
            uname = pid_key.split(':', 1)[0] if ':' in pid_key else pid_key
            return stop_user_process(uname, process_id)
    return False

def admin_hard_delete_process(process_id):
    """Permanently delete a process across all users — stops it first if running."""
    admin_stop_process(process_id)
    deleted = False
    for uname in _list_all_users_with_data():
        if hard_delete_process_from_history(uname, process_id):
            deleted = True
    return deleted

def get_process_csv(process_id):
    """Build a one-row CSV summary for a News Subscription process."""
    import csv as _csv
    from io import StringIO

    record = None
    owner = None
    for uname in _list_all_users_with_data():
        for entry in get_user_process_history(uname):
            if entry.get('id') == process_id:
                record = entry
                owner = uname
                break
        if record:
            break

    if not record:
        for pid_key, p in user_processes.items():
            if isinstance(p, dict) and (p.get('id') == process_id or pid_key.endswith(f":{process_id}")):
                owner = pid_key.split(':', 1)[0] if ':' in pid_key else pid_key
                record = _format_active_process_snapshot(owner, p)
                break

    if not record:
        return None, 'Process not found'

    buf = StringIO()
    w = _csv.writer(buf)
    w.writerow(['username', 'process_id', 'email_used', 'status',
                'total_domains_processed', 'successful_registrations',
                'failed_registrations', 'success_rate',
                'start_time', 'end_time', 'duration', 'deleted'])
    w.writerow([
        owner or '',
        record.get('id', ''),
        record.get('email_used', ''),
        record.get('status', ''),
        record.get('total_domains_processed', 0),
        record.get('successful_registrations', 0),
        record.get('failed_registrations', 0),
        record.get('success_rate', 0),
        record.get('start_time', ''),
        record.get('end_time', ''),
        record.get('duration', ''),
        record.get('deleted', False),
    ])
    return buf.getvalue().encode('utf-8'), None

def is_process_running(username):
    return username in user_processes and user_processes[username].get('running', False)

def stop_user_process(username, process_id='default'):
    """Stop a specific process and ensure it is saved to history."""
    # Ensure we use the full PID internally
    internal_pid = f"{username}:{process_id}"
    
    with get_user_lock(username):
        if internal_pid in user_processes:
            process = user_processes[internal_pid]
            process['running'] = False
            process['status'] = 'Stopped by user'
            
            # Record end time and calculate duration
            end_time = datetime.now()
            start_time_str = process.get('start_time')
            duration_str = "N/A"
            if start_time_str:
                try:
                    start_time = datetime.fromisoformat(start_time_str)
                    duration = end_time - start_time
                    hours, remainder = divmod(duration.total_seconds(), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    duration_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
                except Exception as e:
                    logging.error(f"Error calculating duration: {e}")

            # Save to history immediately when stopped
            history_data = {
                'id': process.get('id', process_id),
                'email_used': process.get('email'),
                'total_domains_processed': process.get('progress', 0),
                'successful_registrations': process.get('successful', 0),
                'failed_registrations': process.get('failed', 0),
                'success_rate': round((process.get('successful', 0) / max(process.get('progress', 1), 1)) * 100) if process.get('progress', 0) > 0 else 0,
                'created_at': end_time.isoformat(),
                'start_time': start_time_str,
                'end_time': end_time.isoformat(),
                'duration': duration_str,
                'status': 'stopped'
            }
            add_process_to_history(username, history_data)
            
            # Save state to file before removing from memory
            save_process_state(username, process)
            
            # Clean up the running process
            if internal_pid in user_processes:
                del user_processes[internal_pid]
            
            # Also clean up the state file if it exists
            delete_process_state(username, process_id)
            return True
    return False

def validate_domain(domain):
    domain = domain.strip()
    if not domain:
        return None, "Empty domain"
    
    if domain.startswith(('http://', 'https://')):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(domain)
            if parsed.netloc:
                return domain, None
        except:
            pass
        return None, "Invalid URL format"
    
    domain_pattern = re.compile(
        r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
    )
    
    if domain_pattern.match(domain):
        return f"https://{domain}", None
    
    return None, f"Invalid domain format: {domain}"

def generate_password(length=12):
    uppercase = string.ascii_uppercase
    lowercase = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    
    password = [
        random.choice(uppercase),
        random.choice(lowercase),
        random.choice(digits),
        random.choice(special)
    ]
    
    all_chars = uppercase + lowercase + digits + special
    for _ in range(length - 4):
        password.append(random.choice(all_chars))
    
    random.shuffle(password)
    return ''.join(password)

async def check_domain_relevance(page):
    try:
        await page.wait_for_timeout(2000)
        page_text = (await page.inner_text('body', timeout=8000)).lower()
        
        required_keywords = [
            'create account', 'login', 'log in', 'sign in', 'sign up',
            'signup', 'free trial', 'register', 'registration'
        ]
        
        for keyword in required_keywords:
            if keyword in page_text:
                return True
        
        return False
        
    except Exception as e:
        return False

async def is_registration_related(text, href=None):
    if not text or len(text.strip()) < 2:
        return False
    
    text_lower = text.lower().strip()
    
    negative_keywords = ['log in', 'login', 'sign in', 'signin', 'sign-in']
    for keyword in negative_keywords:
        if keyword in text_lower:
            return False
    
    primary_keywords = [
        'sign up', 'signup', 'sign-up', 'register', 'registration', 'create account',
        'create free account', 'join now', 'get started', 'subscribe', 'newsletter',
        'email signup', 'free account', 'start now', 'enroll', 'join free',
        'become member', 'new account', 'get access', 'start free trial'
    ]
    
    third_party_keywords = ['google', 'facebook', 'twitter', 'apple', 'oauth', 'with', 'github']
    
    for keyword in primary_keywords:
        if keyword in text_lower:
            if any(tp in text_lower for tp in third_party_keywords):
                return False
            return True
    
    if href:
        href_lower = href.lower()
        href_keywords = [
            '/signup', '/sign-up', '/register', '/registration', '/join',
            '/subscribe', '/create/account', '/account/create', '/enroll'
        ]
        for keyword in href_keywords:
            if keyword in href_lower:
                if any(tp in href_lower for tp in third_party_keywords):
                    return False
                return True
    
    return False

async def get_element_info(element):
    try:
        if not await element.is_visible():
            return None, None
        
        text = ''
        methods = [
            ('inner_text', lambda: element.inner_text(timeout=3000)),
            ('text_content', lambda: element.text_content()),
            ('value', lambda: element.get_attribute('value')),
            ('aria-label', lambda: element.get_attribute('aria-label')),
            ('title', lambda: element.get_attribute('title')),
            ('alt', lambda: element.get_attribute('alt')),
            ('data-text', lambda: element.get_attribute('data-text')),
            ('placeholder', lambda: element.get_attribute('placeholder')),
        ]
        
        for method_name, method in methods:
            try:
                result = await method()
                if result and result.strip():
                    text = result.strip()
                    break
            except:
                continue
        
        href = None
        try:
            href = await element.get_attribute('href')
        except:
            pass
        
        return text, href
        
    except Exception as e:
        return None, None

async def safe_click(element, page, timeout=5000, force=False, is_check=False):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    try:
        async with page.expect_popup(timeout=timeout) as popup_info:
            if is_check:
                if force:
                    await element.check(force=True, timeout=timeout)
                else:
                    await element.check(timeout=timeout)
            else:
                if force:
                    await element.click(force=True, timeout=timeout)
                else:
                    await element.click(timeout=timeout)
        
        popup = await popup_info.value
        if popup:
            old_page = page
            page = popup
            await page.wait_for_load_state('domcontentloaded', timeout=10000)
            await old_page.close()
            return page, True
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        pass
    
    return page, False

async def handle_popup_with_email_detection(page, email):
    try:
        await page.wait_for_timeout(2000)
        
        popup_selectors = [
            '[role="dialog"]:visible',
            '.modal:visible',
            '.popup:visible',
            '[class*="modal" i]:visible',
            '[class*="popup" i]:visible',
            '[id*="modal" i]:visible',
            '[id*="popup" i]:visible',
            '.overlay:visible'
        ]
        
        popup = None
        for selector in popup_selectors:
            try:
                popup = await page.query_selector(selector)
                if popup and await popup.is_visible():
                    break
            except:
                continue
        
        if not popup:
            return False, page
        
        email_inputs = await popup.query_selector_all(
            'input[type="email"]:visible, input[name*="email" i]:visible, '
            'input[placeholder*="email" i]:visible'
        )
        
        has_email_field = False
        for email_input in email_inputs:
            if await email_input.is_visible():
                has_email_field = True
                break
        
        if not has_email_field:
            close_selectors = [
                'button[aria-label*="close" i]:visible',
                'button[title*="close" i]:visible',
                '[class*="close" i]:visible',
                'button:has-text("×"):visible',
                'button:has-text("Close"):visible',
                '[role="button"][aria-label*="close" i]:visible'
            ]
            
            for selector in close_selectors:
                try:
                    close_btn = await popup.query_selector(selector)
                    if close_btn and await close_btn.is_visible():
                        page, _ = await safe_click(close_btn, page)
                        await page.wait_for_timeout(1000)
                        return True, page
                except:
                    continue
            
            try:
                await page.keyboard.press('Escape')
                await page.wait_for_timeout(1000)
                return True, page
            except:
                pass
        
        else:
            fake_local = Faker()
            popup_email = email
            
            all_inputs = await popup.query_selector_all('input:visible, textarea:visible')
            
            for input_elem in all_inputs:
                try:
                    input_type = (await input_elem.get_attribute('type') or 'text').lower()
                    input_name = (await input_elem.get_attribute('name') or '').lower()
                    input_placeholder = (await input_elem.get_attribute('placeholder') or '').lower()
                    field_context = f"{input_name} {input_placeholder}".lower()
                    
                    if input_type == 'email' or 'email' in field_context:
                        await input_elem.fill(popup_email)
                    elif 'name' in field_context and 'email' not in field_context:
                        await input_elem.fill(fake_local.name())
                    elif input_type == 'text':
                        await input_elem.fill(fake_local.name())
                    
                    await page.wait_for_timeout(500)
                except:
                    continue
            
            submit_selectors = [
                'button[type="submit"]:visible',
                'input[type="submit"]:visible',
                'button:has-text("Submit"):visible',
                'button:has-text("Subscribe"):visible',
                'button:has-text("Continue"):visible',
                'button:has-text("Sign up"):visible'
            ]
            
            for selector in submit_selectors:
                try:
                    submit_btn = await popup.query_selector(selector)
                    if submit_btn and await submit_btn.is_visible():
                        page, _ = await safe_click(submit_btn, page)
                        await page.wait_for_timeout(2000)
                        return True, page
                except:
                    continue
        
        return False, page
        
    except Exception as e:
        return False, page

async def find_registration_elements_comprehensive(page):
    try:
        await page.wait_for_load_state('domcontentloaded', timeout=10000)
        await page.wait_for_timeout(3000)
        
        registration_elements = []
        
        element_selectors = [
            'a:has-text("Register"):visible',
            'a:has-text("Sign Up"):visible',
            'a:has-text("Sign up"):visible',
            'a:has-text("Create Account"):visible',
            'a:has-text("Subscribe"):visible',
            'a:has-text("Newsletter"):visible',
            'button:has-text("Register"):visible',
            'button:has-text("Sign Up"):visible',
            'button:has-text("Subscribe"):visible',
            'button:has-text("Join"):visible',
            'button:has-text("Get Started"):visible',
            'a[href*="register" i]:visible',
            'a[href*="signup" i]:visible',
            'a[href*="sign-up" i]:visible',
            'a[href*="subscribe" i]:visible',
            'a[href*="join" i]:visible',
            '[class*="register" i]:visible',
            '[class*="signup" i]:visible',
            'input[type="submit"][value*="register" i]:visible',
            'input[type="submit"][value*="sign up" i]:visible',
            'input[type="submit"][value*="subscribe" i]:visible'
        ]
        
        processed_elements = set()
        
        for selector in element_selectors:
            try:
                elements = await asyncio.wait_for(
                    page.query_selector_all(selector),
                    timeout=5.0
                )
                
                for element in elements[:5]:
                    try:
                        element_id = await asyncio.wait_for(
                            element.evaluate('el => el.outerHTML.substring(0, 100)'),
                            timeout=2.0
                        )
                        if element_id in processed_elements:
                            continue
                        processed_elements.add(element_id)
                        
                        if not await element.is_visible():
                            continue
                        
                        text, href = await get_element_info(element)
                        if text and await is_registration_related(text, href):
                            registration_elements.append((element, text, href or ''))
                            
                            if len(registration_elements) >= 8:
                                break
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        continue
                        
                if len(registration_elements) >= 8:
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                continue
        
        def get_priority(item):
            text = item[1].lower()
            href = item[2].lower() if item[2] else ''
            
            if text.strip() in ['register', 'sign up', 'signup', 'subscribe']:
                return 0
            elif any(x in text for x in ['register', 'sign up', 'create account', 'subscribe']):
                return 1
            elif any(x in text for x in ['join', 'get started', 'newsletter']):
                return 2
            elif any(x in href for x in ['register', 'signup', 'join', 'subscribe']):
                return 3
            return 4
        
        registration_elements.sort(key=get_priority)
        
        return registration_elements[:6]
        
    except Exception as e:
        return []

async def smart_form_fill(page, email):
    try:
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=10000)
        except Exception as e:
            pass
        
        await page.wait_for_timeout(5000)
        
        username = fake.user_name().lower() + str(random.randint(100, 999))
        password = generate_password(16)
        first_name = fake.first_name()
        last_name = fake.last_name()
        full_name = f"{first_name} {last_name}"
        
        filled_count = 0
        
        try:
            all_form_elements = await page.query_selector_all(
                'input:not([type="hidden"]):not([disabled]):visible, '
                'textarea:not([disabled]):visible, '
                'select:not([disabled]):visible'
            )
            
            for i, element in enumerate(all_form_elements):
                try:
                    if not await element.is_visible():
                        continue
                    
                    input_type = 'text'
                    input_name = ''
                    input_id = ''
                    input_placeholder = ''
                    tag_name = ''
                    
                    try:
                        tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
                        input_type = (await element.get_attribute('type') or 'text').lower()
                        input_name = (await element.get_attribute('name') or '').lower()
                        input_id = (await element.get_attribute('id') or '').lower()
                        input_placeholder = (await element.get_attribute('placeholder') or '').lower()
                    except Exception as e:
                        pass
                    
                    field_context = f"{input_name} {input_id} {input_placeholder}".lower()
                    
                    try:
                        current_value = await element.get_attribute('value') or ''
                        if current_value.strip() and len(current_value.strip()) > 2:
                            continue
                    except:
                        pass
                    
                    filled = False
                    
                    if tag_name == 'select':
                        try:
                            options = await element.query_selector_all('option')
                            if len(options) > 1:
                                valid_options = []
                                for option in options[1:]:
                                    value = await option.get_attribute('value')
                                    if value and value.strip():
                                        valid_options.append(value)
                                
                                if valid_options:
                                    selected_value = random.choice(valid_options)
                                    await element.select_option(selected_value)
                                    filled = True
                        except Exception as e:
                            pass
                    
                    else:
                        try:
                            page, _ = await safe_click(element, page)
                            await element.fill('')
                            await page.wait_for_timeout(300)
                            
                            if input_type == 'email' or 'email' in field_context:
                                await element.fill(email)
                                filled = True
                            
                            elif input_type == 'password':
                                await element.fill(password)
                                filled = True
                            
                            elif ('username' in field_context or 'login' in field_context) and input_type != 'password':
                                await element.fill(username)
                                filled = True
                            
                            elif 'first' in field_context and 'name' in field_context:
                                await element.fill(first_name)
                                filled = True
                            elif 'last' in field_context and 'name' in field_context:
                                await element.fill(last_name)
                                filled = True
                            elif ('full' in field_context or 'name' in field_context) and 'email' not in field_context:
                                await element.fill(full_name)
                                filled = True
                            
                            elif 'phone' in field_context or 'mobile' in field_context:
                                phone = fake.phone_number()
                                await element.fill(phone)
                                filled = True
                            
                            elif 'age' in field_context:
                                age = str(random.randint(18, 65))
                                await element.fill(age)
                                filled = True
                            elif 'birth' in field_context or 'dob' in field_context:
                                birth_date = fake.date_of_birth(minimum_age=18, maximum_age=65).strftime('%m/%d/%Y')
                                await element.fill(birth_date)
                                filled = True
                            
                            elif 'company' in field_context or 'organization' in field_context:
                                company = fake.company()
                                await element.fill(company)
                                filled = True
                            
                            elif 'title' in field_context or 'position' in field_context:
                                title = fake.job()
                                await element.fill(title)
                                filled = True
                            
                            elif 'address' in field_context:
                                address = fake.address().replace('\n', ', ')
                                await element.fill(address)
                                filled = True
                            elif 'city' in field_context:
                                city = fake.city()
                                await element.fill(city)
                                filled = True
                            elif 'zip' in field_context or 'postal' in field_context:
                                zip_code = fake.zipcode()
                                await element.fill(zip_code)
                                filled = True
                            
                            elif input_type == 'text' and not filled:
                                if any(word in field_context for word in ['search', 'query', 'keyword']):
                                    continue
                                else:
                                    await element.fill(full_name)
                                    filled = True
                            
                            elif tag_name == 'textarea':
                                message = f"Hello, I'm interested in your services. Please contact me at {email}. Thank you!"
                                await element.fill(message)
                                filled = True
                            
                        except Exception as e:
                            pass
                    
                    if filled:
                        filled_count += 1
                        await page.wait_for_timeout(800)
                    
                except Exception as e:
                    continue
        
        except Exception as e:
            pass
        
        try:
            checkboxes = await page.query_selector_all('input[type="checkbox"]:visible')
            for checkbox in checkboxes:
                try:
                    checkbox_name = (await checkbox.get_attribute('name') or '').lower()
                    checkbox_id = (await checkbox.get_attribute('id') or '').lower()
                    checkbox_context = f"{checkbox_name} {checkbox_id}".lower()
                    
                    if any(term in checkbox_context for term in
                          ['agree', 'terms', 'privacy', 'consent', 'accept', 'newsletter', 'updates']):
                        if not await checkbox.is_checked():
                            page, _ = await safe_click(checkbox, page, is_check=True)
                            filled_count += 1
                except Exception as e:
                    pass
            
            radio_groups = {}
            radios = await page.query_selector_all('input[type="radio"]:visible')
            for radio in radios:
                try:
                    radio_name = await radio.get_attribute('name')
                    if radio_name and radio_name not in radio_groups:
                        page, _ = await safe_click(radio, page, is_check=True)
                        radio_groups[radio_name] = True
                        filled_count += 1
                except Exception as e:
                    pass
                    
        except Exception as e:
            pass
        
        return filled_count
        
    except Exception as e:
        return 0

async def smart_form_submit(page):
    try:
        submit_selectors = [
            'button:has-text("Register"):visible',
            'button:has-text("Sign Up"):visible',
            'button:has-text("Sign up"):visible',
            'button:has-text("Create Account"):visible',
            'button:has-text("Subscribe"):visible',
            'button:has-text("Join"):visible',
            'input[type="submit"]:visible',
            'button[type="submit"]:visible',
            'input[value*="register" i]:visible',
            'input[value*="sign up" i]:visible',
            'input[value*="subscribe" i]:visible',
            'input[value*="create" i]:visible',
            'input[value*="join" i]:visible',
            'button:has-text("Submit"):visible',
            'button:has-text("Continue"):visible',
            'button:has-text("Get Started"):visible',
            '.btn-primary:visible',
            'form button:visible'
        ]
        
        submit_button = None
        button_text = ""
        third_party_keywords = ['google', 'facebook', 'twitter', 'apple', 'oauth', 'with', 'github']
        
        for selector in submit_selectors:
            try:
                submit_button = await page.query_selector(selector)
                if submit_button and await submit_button.is_visible():
                    button_text, _ = await get_element_info(submit_button)
                    if button_text and any(tp in button_text.lower() for tp in third_party_keywords):
                        continue
                    break
            except:
                continue
        
        if not submit_button:
            return False, page
        
        current_url = page.url
        
        try:
            await submit_button.scroll_into_view_if_needed()
            await page.wait_for_timeout(1000)
            page, _ = await safe_click(submit_button, page, timeout=10000, force=True)
        except Exception as e:
            return False, page
        
        await page.wait_for_timeout(5000)
        await page.wait_for_timeout(3000)
        
        new_url = page.url
        new_title = await page.title()
        
        success_url_indicators = [
            'success', 'confirm', 'confirmation', 'thank', 'thanks', 'welcome',
            'verify', 'verification', 'activation', 'activate', 'registered',
            'complete', 'done', 'dashboard', 'account', 'profile', 'subscribed'
        ]
        
        if new_url != current_url:
            if any(indicator in new_url.lower() for indicator in success_url_indicators):
                return True, page
        
        if new_title:
            success_title_indicators = [
                'success', 'confirm', 'thank', 'welcome', 'verify', 'complete',
                'registration', 'account created', 'signed up', 'subscribed'
            ]
            if any(indicator in new_title.lower() for indicator in success_title_indicators):
                return True, page
        
        try:
            page_text = await page.inner_text('body', timeout=8000)
            page_text_lower = page_text.lower()
            
            success_indicators = [
                'thank you', 'thanks', 'success', 'successful', 'successfully',
                'welcome', 'registered', 'registration complete', 'subscribed',
                'subscription successful', 'confirmation', 'verify your email',
                'check your email', 'account created', 'signed up',
                'congratulations', 'almost done', 'please verify', 'activation',
                'confirm your subscription', 'welcome aboard', 'you\'re in',
                'registration successful', 'confirmation email sent'
            ]
            
            for indicator in success_indicators:
                if indicator in page_text_lower:
                    return True, page
                    
        except Exception as e:
            pass
        
        return True, page
        
    except Exception as e:
        return False, page

async def comprehensive_registration_workflow(page, email, domain):
    try:
        max_workflow_steps = 2
        current_step = 0
        forms_found = 0
        fields_filled = 0
        made_progress = False
        
        while current_step < max_workflow_steps:
            current_step += 1
            current_url = page.url
            step_progress = False
            
            try:
                forms = await page.query_selector_all('form:visible')
                forms_found = len(forms)
                
                if forms:
                    fields_filled_count = await smart_form_fill(page, email)
                    fields_filled += fields_filled_count
                    
                    if fields_filled_count > 0:
                        step_progress = True
                        made_progress = True
                        
                        submission_result, page = await smart_form_submit(page)
                        
                        if submission_result:
                            return True, page
                    
            except Exception as e:
                pass
            
            if not step_progress:
                try:
                    popup_handled, page = await handle_popup_with_email_detection(page, email)
                    
                    forms = await page.query_selector_all('form:visible')
                    if forms and forms_found < len(forms):
                        fields_filled_count = await smart_form_fill(page, email)
                        fields_filled += fields_filled_count
                        if fields_filled_count > 0:
                            submission_result, page = await smart_form_submit(page)
                            if submission_result:
                                return True, page
                    
                    reg_elements = await find_registration_elements_comprehensive(page)
                    
                    if reg_elements:
                        step_progress = True
                        made_progress = True
                        
                        elements_to_try = min(2, len(reg_elements))
                        
                        for i, (element, text, href) in enumerate(reg_elements[:elements_to_try]):
                            try:
                                await element.scroll_into_view_if_needed()
                                await page.wait_for_timeout(1000)
                                page, _ = await safe_click(element, page, timeout=10000)
                                
                                await page.wait_for_timeout(CONFIG['wait_after_click'])
                                
                                new_url = page.url
                                if new_url != current_url:
                                    break
                                
                                await page.wait_for_timeout(2000)
                                new_forms = await page.query_selector_all('form:visible')
                                if new_forms and len(new_forms) > forms_found:
                                    fields_filled_count = await smart_form_fill(page, email)
                                    fields_filled += fields_filled_count
                                    if fields_filled_count > 0:
                                        submission_result, page = await smart_form_submit(page)
                                        if submission_result:
                                            return True, page
                                    break
                                
                            except Exception as e:
                                continue
                    
                except Exception as e:
                    pass
            
            if not step_progress:
                if current_step >= 2 and not made_progress:
                    return False, page
            
            await page.wait_for_timeout(1500)
        
        return False, page
        
    except Exception as e:
        return False, page

async def process_domain_with_retry(page, domain, email):
    url = domain if domain.startswith(('http://', 'https://')) else f"https://{domain}"
    
    max_retries = CONFIG['max_retries']
    relevance_checked = False
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                pass
            
            try:
                await page.goto(url, timeout=CONFIG['navigation_timeout'], wait_until='domcontentloaded')
            except Exception as nav_error:
                if attempt == max_retries - 1:
                    return False
                continue
            
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except:
                pass
            
            await page.wait_for_timeout(5000)
            
            if not relevance_checked:
                if not await check_domain_relevance(page):
                    return False
                relevance_checked = True
            
            await handle_popup_with_email_detection(page, email)
            
            success, page = await comprehensive_registration_workflow(page, email, domain)
            
            if success:
                return True
            else:
                if attempt == max_retries - 1:
                    return False
                
        except Exception as e:
            if attempt == max_retries - 1:
                return False
            
        if attempt < max_retries - 1:
            retry_delay = random.randint(3000, 8000)
            await page.wait_for_timeout(retry_delay)
    
    return False

def is_infinity_process(username):
    try:
        with open('users.txt', 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split(',')
                if len(parts) >= 3 and parts[2].strip() == username:
                    return 'infinity-process' in [p.strip() for p in parts[4:]]
    except:
        pass
    return False

def get_active_processes(username):
    return [pid for pid, p in user_processes.items() if pid.startswith(f"{username}:") and p.get('running')]

def pause_user_process(username, process_id="default"):
    pid = f"{username}:{process_id}"
    if pid in user_processes:
        user_processes[pid]['paused'] = True
        user_processes[pid]['status'] = 'Paused'
        state = load_process_state(username, process_id)
        if state:
            state['paused'] = True
            save_process_state(username, state)
    return True

def resume_user_process(username, process_id="default"):
    pid = f"{username}:{process_id}"
    if pid in user_processes:
        user_processes[pid]['paused'] = False
        user_processes[pid]['status'] = 'Resuming...'
        state = load_process_state(username, process_id)
        if state:
            state['paused'] = False
            save_process_state(username, state)
    return True

async def process_single_domain(p, domain, email, username, results, current_domains, process_id="default"):
    pid = f"{username}:{process_id}"
    domain_display = domain[:50] + '...' if len(domain) > 50 else domain
    
    if pid not in user_processes:
        return
        
    current_domains.add(domain_display)
    
    browser = None
    acquired_global = False
    try:
        while user_processes.get(pid, {}).get('paused'):
            await asyncio.sleep(2)
            if pid not in user_processes or not user_processes[pid].get('running'):
                return

        if pid not in user_processes or not user_processes[pid].get('running'):
            return

        active_count = len([proc for proc in user_processes.values() if proc.get('running')])
        delay = random.uniform(1.0, 3.0) + (0.5 * max(0, active_count - 1))
        await asyncio.sleep(delay)

        for _ in range(60):
            got = GLOBAL_BROWSER_SEMAPHORE.acquire(blocking=False)
            if got:
                acquired_global = True
                break
            if pid not in user_processes or not user_processes[pid].get('running'):
                return
            await asyncio.sleep(1)
        
        if not acquired_global:
            results['completed'] += 1
            results['failed'] += 1
            return

        if pid not in user_processes or not user_processes[pid].get('running'):
            return

        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--single-process',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-sync',
                '--disable-translate',
                '--no-first-run',
                '--disable-default-apps',
                '--disable-hang-monitor',
                '--disable-prompt-on-repost',
                '--disable-client-side-phishing-detection',
                '--disable-component-update',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-ipc-flooding-protection',
                '--js-flags=--max-old-space-size=128',
            ]
        )
        context = await browser.new_context(user_agent=random.choice(CONFIG['user_agents']))
        page = await context.new_page()
        
        success = await process_domain_with_retry(page, domain, email)
        
        if pid not in user_processes or not user_processes[pid].get('running'):
            if browser: await browser.close()
            return

        if success:
            results['successful'] += 1
            add_successful_domain(domain)
        else:
            results['failed'] += 1
            
        results['completed'] += 1
        results['completed_list'].append(domain)
        
        state = {
            'id': process_id,
            'email': email,
            'domains': results['all_domains'],
            'completed_list': results['completed_list'],
            'successful': results['successful'],
            'failed': results['failed'],
            'paused': user_processes.get(pid, {}).get('paused', False),
            'running': user_processes.get(pid, {}).get('running', True),
            'last_updated': datetime.now().isoformat(),
            'start_time': user_processes.get(pid, {}).get('start_time')
        }
        save_process_state(username, state)
        
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
    except Exception as e:
        if pid in user_processes:
            results['completed'] += 1
            results['failed'] += 1
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if acquired_global:
            GLOBAL_BROWSER_SEMAPHORE.release()
        current_domains.discard(domain_display)

async def run_subscription_process_async(username, email, domains, process_id="default", resume_state=None):
    pid = f"{username}:{process_id}"
    
    if resume_state:
        valid_domains = [d for d in resume_state['domains'] if d not in resume_state['completed_list']]
        results = {
            'successful': resume_state['successful'],
            'failed': resume_state['failed'],
            'completed': len(resume_state['completed_list']),
            'completed_list': resume_state['completed_list'],
            'all_domains': resume_state['domains']
        }
    else:
        valid_domains = []
        for d in domains:
            v, _ = validate_domain(d)
            if v: valid_domains.append(v)
        results = {'successful': 0, 'failed': 0, 'completed': 0, 'completed_list': [], 'all_domains': valid_domains}

    user_processes[pid] = {
        'running': True,
        'paused': resume_state.get('paused', False) if resume_state else False,
        'progress': results['completed'],
        'total': len(results['all_domains']),
        'status': 'Processing...',
        'successful': results['successful'],
        'failed': results['failed'],
        'current_domains': [],
        'id': process_id,
        'start_time': resume_state.get('start_time', datetime.now().isoformat()) if resume_state else datetime.now().isoformat()
    }
    
    current_domains_set = set()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            semaphore = asyncio.Semaphore(CONFIG['max_concurrent'])
            batch_size = CONFIG['max_concurrent']
            for i in range(0, len(valid_domains), batch_size):
                if not user_processes.get(pid, {}).get('running'):
                    break
                batch = valid_domains[i:i + batch_size]
                async def wrap(d):
                    async with semaphore:
                        if not user_processes.get(pid, {}).get('running'): return
                        await process_single_domain(p, d, email, username, results, current_domains_set, process_id)
                        if pid in user_processes:
                            user_processes[pid].update({
                                'progress': results['completed'],
                                'successful': results['successful'],
                                'failed': results['failed'],
                                'current_domains': list(current_domains_set)
                            })
                await asyncio.gather(*[wrap(d) for d in batch])
                await asyncio.sleep(random.uniform(1.0, 2.0))
            
            # Check if process still exists before final completion
            if pid not in user_processes:
                return

        if pid not in user_processes:
            logging.error(f"PID {pid} not found in user_processes during completion cleanup")
            return

        end_time = datetime.now()
        start_time_str = user_processes[pid].get('start_time')
        duration_str = "N/A"
        if start_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str)
                duration = end_time - start_time
                hours, remainder = divmod(duration.total_seconds(), 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
            except Exception as e:
                logging.error(f"Error calculating duration: {e}")

        delete_process_state(username, process_id)
        user_processes[pid]['running'] = False
        add_process_to_history(username, {
            'status': 'completed', 
            'created_at': end_time.isoformat(),
            'start_time': start_time_str,
            'end_time': end_time.isoformat(),
            'duration': duration_str,
            'successful_registrations': results['successful'], 
            'failed_registrations': results['failed'],
            'success_rate': round(results['successful']/(results['successful']+results['failed'])*100,1) if (results['successful']+results['failed'])>0 else 0,
            'total_domains_processed': results['successful']+results['failed'], 
            'email_used': email
        })
        
        # Finally remove from memory
        if pid in user_processes:
            del user_processes[pid]
    except Exception as e:
        logging.error(f"Process error: {e}")
        if pid in user_processes:
            user_processes[pid]['running'] = False
            user_processes[pid]['status'] = f"Error: {str(e)}"

def run_subscription_process(username, email, domains, process_id="default", resume_state=None):
    def _run_with_low_priority():
        _lower_thread_priority()
        asyncio.run(run_subscription_process_async(username, email, domains, process_id, resume_state))
    threading.Thread(target=_run_with_low_priority, daemon=True).start()
