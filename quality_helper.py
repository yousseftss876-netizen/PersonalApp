import os
import json
import random
import re
import time
import requests
import shutil
import threading
import logging
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

USER_DATA_DIR = "quality_helper_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

MAX_PDF_SIZE = 5 * 1024 * 1024  # 5 MB limit

MIN_IMAGE_SIZE = 60 * 1024
MAX_IMAGE_SIZE = 200 * 1024
SKIP_EXTENSIONS = [".webp"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

SERVICE_PHRASES = [
    "web design service",
    "app development service",
    "seo optimization service",
    "digital marketing service",
    "content writing service",
    "graphic design service",
    "video editing service",
    "business consulting service",
    "house cleaning service",
    "coffee delivery service",
    "restaurant takeout service",
]

SUBJECT_TEMPLATES = [
    "How a {service} helped a business grow fast",
    "Why one company invested in a {service}",
    "When a brand realized it needed a {service}",
    "How a small team scaled using a {service}",
    "What changed after switching to a {service}",
]

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

def get_user_images_dir(username):
    images_dir = os.path.join(get_user_dir(username), "images")
    os.makedirs(images_dir, exist_ok=True)
    return images_dir

def get_user_data_file(username):
    return os.path.join(get_user_dir(username), "process_data.json")

def generate_subject():
    return random.choice(SUBJECT_TEMPLATES).format(
        service=random.choice(SERVICE_PHRASES)
    )

def clean_filename(text):
    text = re.sub(r"[^\w\s-]", "", text)
    return text.replace(" ", "_")

def get_user_process_status(username):
    data_file = get_user_data_file(username)
    if os.path.exists(data_file):
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    return None

def save_user_process_data(username, data):
    data_file = get_user_data_file(username)
    with open(data_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def is_process_running(username):
    return username in user_processes and user_processes[username].get('running', False)

def delete_user_process(username):
    user_dir = get_user_dir(username)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)
    if username in user_processes:
        del user_processes[username]
    return True

def run_image_generation(username, keywords, total_images):
    from playwright.sync_api import sync_playwright
    
    user_processes[username] = {
        'running': True,
        'progress': 0,
        'total': total_images,
        'status': 'Starting...',
        'error': None
    }
    
    try:
        images_dir = get_user_images_dir(username)
        
        if len(keywords) < total_images:
            user_processes[username]['error'] = f"Not enough keywords ({len(keywords)}) for {total_images} images"
            user_processes[username]['running'] = False
            return
        
        selected_keywords = random.sample(keywords, total_images)
        
        subjects = []
        image_data = []
        
        user_processes[username]['status'] = 'Starting browser...'
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()
            
            for idx, keyword in enumerate(selected_keywords):
                if not user_processes.get(username, {}).get('running', False):
                    break
                    
                user_processes[username]['status'] = f'Searching: {keyword}'
                
                try:
                    # Faster search URL
                    page.goto(
                        f"https://www.bing.com/images/search?q={keyword}&first=1",
                        wait_until="commit",
                        timeout=30000,
                    )
                    
                    page.wait_for_selector("a.iusc", timeout=10000)
                    results = page.query_selector_all("a.iusc")
                    random.shuffle(results)
                    
                    for item in results[:10]:
                        data_m = item.get_attribute("m")
                        if not data_m:
                            continue
                        
                        try:
                            meta = json.loads(data_m)
                            img_url = meta.get("murl")
                        except:
                            continue
                        
                        if not img_url or not img_url.startswith("http"):
                            continue
                        
                        try:
                            r = requests.get(
                                img_url,
                                headers={
                                    "User-Agent": UA,
                                    "Referer": "https://www.bing.com/",
                                },
                                timeout=10,
                            )
                            
                            ct = r.headers.get("Content-Type", "").lower()
                            size = len(r.content)
                            
                            if (
                                not r.ok
                                or "image" not in ct
                                or size < MIN_IMAGE_SIZE
                                or size > MAX_IMAGE_SIZE
                            ):
                                continue
                            
                            ext = ".png" if "png" in ct else ".jpg"
                            subject = generate_subject()
                            filename = f"{clean_filename(subject)}{ext}"
                            path = os.path.join(images_dir, filename)
                            
                            with open(path, "wb") as f:
                                f.write(r.content)
                            
                            image_data.append({
                                'filename': filename,
                                'url': img_url,
                                'keyword': keyword,
                                'subject': subject,
                                'size_kb': size // 1024
                            })
                            subjects.append(subject)
                            user_processes[username]['progress'] += 1
                            break # Found one, move to next keyword
                            
                        except:
                            continue
                except Exception as e:
                    logging.error(f"Error searching for {keyword}: {e}")
                    continue

            browser.close()
        
        process_data = {
            'status': 'completed',
            'created_at': datetime.now().isoformat(),
            'keywords_used': selected_keywords,
            'total_requested': total_images,
            'total_fetched': len(image_data),
            'subjects': subjects,
            'images': image_data,
            'image_links': [img['filename'] for img in image_data]
        }
        
        save_user_process_data(username, process_data)
        
        user_processes[username]['progress'] = total_images
        user_processes[username]['status'] = 'Completed'
        user_processes[username]['running'] = False
        
    except Exception as e:
        logging.error(f"Error in image generation for {username}: {e}")
        user_processes[username]['error'] = str(e)
        user_processes[username]['running'] = False


# ===================== PDF FUNCTIONS =====================

pdf_user_processes = {}
pdf_user_process_locks = {}

def get_pdf_user_lock(username):
    if username not in pdf_user_process_locks:
        pdf_user_process_locks[username] = threading.Lock()
    return pdf_user_process_locks[username]

def get_user_pdfs_dir(username):
    pdfs_dir = os.path.join(get_user_dir(username), "pdfs")
    os.makedirs(pdfs_dir, exist_ok=True)
    return pdfs_dir

def get_user_pdf_data_file(username):
    return os.path.join(get_user_dir(username), "pdf_process_data.json")

def get_pdf_user_process_status(username):
    data_file = get_user_pdf_data_file(username)
    if os.path.exists(data_file):
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    return None

def save_pdf_user_process_data(username, data):
    data_file = get_user_pdf_data_file(username)
    with open(data_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def is_pdf_process_running(username):
    return username in pdf_user_processes and pdf_user_processes[username].get('running', False)

def delete_pdf_user_process(username):
    pdfs_dir = get_user_pdfs_dir(username)
    if os.path.exists(pdfs_dir):
        shutil.rmtree(pdfs_dir)
    pdf_data_file = get_user_pdf_data_file(username)
    if os.path.exists(pdf_data_file):
        os.remove(pdf_data_file)
    if username in pdf_user_processes:
        del pdf_user_processes[username]
    return True

def is_good_pdf_filename(filename):
    name_without_ext = filename.replace('.pdf', '')
    if any(char.isdigit() for char in name_without_ext):
        return False
    words = [w for w in name_without_ext.split('_') if w.strip()]
    return len(words) >= 5

def extract_paper_title(abs_soup):
    try:
        title_selectors = [
            "h1.title",
            "h1.mathjax",
            ".title.mathjax",
            "h1",
            ".title"
        ]
        for selector in title_selectors:
            title_element = abs_soup.select_one(selector)
            if title_element:
                title = title_element.get_text(strip=True)
                if title.lower().startswith('title:'):
                    title = title[6:].strip()
                return title[:100]
    except:
        pass
    return None

def run_pdf_generation(username, keywords, total_pdfs):
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    )
    HEADERS = {"User-Agent": UA}
    
    pdf_user_processes[username] = {
        'running': True,
        'progress': 0,
        'total': total_pdfs,
        'status': 'Starting...',
        'error': None
    }
    
    try:
        pdfs_dir = get_user_pdfs_dir(username)
        
        if len(keywords) < total_pdfs:
            pdf_user_processes[username]['error'] = f"Not enough keywords ({len(keywords)}) for {total_pdfs} PDFs. Please add more keywords."
            pdf_user_processes[username]['running'] = False
            return
        
        available_keywords = keywords.copy()
        used_keywords = set()
        seen_urls = set()
        downloaded_pdfs = []
        downloaded_subjects = []
        pdf_data = []
        downloaded = 0
        max_attempts = total_pdfs * 5
        attempt_count = 0
        
        pdf_user_processes[username]['status'] = 'Searching arXiv...'
        
        while downloaded < total_pdfs and attempt_count < max_attempts:
            if not pdf_user_processes.get(username, {}).get('running', False):
                break
            
            attempt_count += 1
            
            if not available_keywords:
                available_keywords = [k for k in keywords if k not in used_keywords]
                if not available_keywords:
                    available_keywords = keywords.copy()
            
            keyword = random.choice(available_keywords)
            available_keywords.remove(keyword)
            used_keywords.add(keyword)
            
            encoded_keyword = requests.utils.quote(keyword)
            search_url = f"https://arxiv.org/search/?query={encoded_keyword}&searchtype=all&source=header"
            
            pdf_user_processes[username]['status'] = f'Searching: {keyword}'
            
            try:
                r = requests.get(search_url, headers=HEADERS, timeout=20)
                r.raise_for_status()
            except requests.exceptions.RequestException as e:
                logging.error(f"Failed to access search page: {e}")
                continue
            
            soup = BeautifulSoup(r.text, "html.parser")
            paper_links = soup.select("p.list-title.is-inline-block a[href^='/abs/']")
            
            if not paper_links:
                paper_links = soup.select("a[href*='/abs/']")
            
            if not paper_links:
                continue
            
            pdf_downloaded = False
            papers_tried = 0
            
            for link in paper_links:
                if downloaded >= total_pdfs:
                    break
                
                if pdf_downloaded:
                    break
                
                papers_tried += 1
                if papers_tried > 10:
                    break
                
                abs_url = urljoin("https://arxiv.org", link["href"])
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)
                
                try:
                    abs_page = requests.get(abs_url, headers=HEADERS, timeout=20)
                    abs_page.raise_for_status()
                except requests.exceptions.RequestException:
                    continue
                
                abs_soup = BeautifulSoup(abs_page.text, "html.parser")
                paper_title = extract_paper_title(abs_soup)
                
                arxiv_id = abs_url.split('/')[-1]
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                
                if paper_title:
                    clean_title = re.sub(r'[^a-zA-Z\s]', ' ', paper_title)
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    words = clean_title.split()
                    
                    if len(words) >= 5:
                        filename_parts = [word.capitalize() for word in words]
                        filename = '_'.join(filename_parts) + '.pdf'
                        
                        if any(char.isdigit() for char in filename.replace('.pdf', '')):
                            continue
                    else:
                        continue
                else:
                    continue
                
                filepath = os.path.join(pdfs_dir, filename)
                
                if os.path.exists(filepath):
                    continue
                
                pdf_user_processes[username]['status'] = f'Downloading: {filename[:40]}...'
                
                try:
                    with requests.get(pdf_url, headers=HEADERS, stream=True, timeout=30) as pdf_r:
                        pdf_r.raise_for_status()
                        
                        content_length = pdf_r.headers.get('content-length')
                        if content_length and int(content_length) > MAX_PDF_SIZE:
                            continue
                        
                        content_type = pdf_r.headers.get('content-type', '')
                        if 'application/pdf' not in content_type and 'pdf' not in content_type.lower():
                            continue
                        
                        downloaded_bytes = 0
                        with open(filepath, "wb") as f:
                            for chunk in pdf_r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    downloaded_bytes += len(chunk)
                                    
                                    if downloaded_bytes > MAX_PDF_SIZE:
                                        f.close()
                                        os.remove(filepath)
                                        break
                            else:
                                final_size = os.path.getsize(filepath)
                                if final_size > MAX_PDF_SIZE:
                                    os.remove(filepath)
                                    continue
                                
                                downloaded += 1
                                pdf_downloaded = True
                                
                                downloaded_pdfs.append(filename)
                                subject = filename.replace('.pdf', '').replace('_', ' ')
                                downloaded_subjects.append(subject)
                                
                                pdf_data.append({
                                    'filename': filename,
                                    'keyword': keyword,
                                    'subject': subject,
                                    'size_kb': final_size // 1024,
                                    'arxiv_url': abs_url
                                })
                                
                                pdf_user_processes[username]['progress'] = downloaded
                                pdf_user_processes[username]['status'] = f'Downloaded {downloaded}/{total_pdfs} PDFs'
                                
                except requests.exceptions.RequestException as e:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    continue
            
            time.sleep(0.3)
        
        process_data = {
            'status': 'completed',
            'created_at': datetime.now().isoformat(),
            'keywords_used': list(used_keywords),
            'total_requested': total_pdfs,
            'total_fetched': len(pdf_data),
            'subjects': downloaded_subjects,
            'pdfs': pdf_data,
            'pdf_links': downloaded_pdfs
        }
        
        save_pdf_user_process_data(username, process_data)
        
        pdf_user_processes[username]['progress'] = total_pdfs
        pdf_user_processes[username]['status'] = 'Completed'
        pdf_user_processes[username]['running'] = False
        
    except Exception as e:
        logging.error(f"Error in PDF generation for {username}: {e}")
        pdf_user_processes[username]['error'] = str(e)
        pdf_user_processes[username]['running'] = False
