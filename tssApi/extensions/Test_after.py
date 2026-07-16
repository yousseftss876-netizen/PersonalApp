import imaplib
import email
import email.utils
from email.header import decode_header
from datetime import datetime, timezone
import logging
import re
import time

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)
CORS(app, resources={
    r"/count_emails": {"origins": "*"}
})


# ---------------------------------------------------------------------------
# Helpers (same logic as in the main app)
# ---------------------------------------------------------------------------

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
                except Exception:
                    decoded_parts.append(part.decode('utf-8', errors='ignore'))
            else:
                decoded_parts.append(part.decode('utf-8', errors='ignore'))
        else:
            decoded_parts.append(str(part))

    return ''.join(decoded_parts)


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
        except Exception:
            pass

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
    base_delay = 3  # seconds; doubles each attempt (3 → 6 → 12 …)

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

                            # --- Use REAL arrival time from Received headers ---
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
        except Exception:
            pass

        return result

    except Exception as e:
        logging.error(f"Error in count_emails_in_folders: {e}")
        result['error'] = str(e)
        return result


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

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
        received_after = data.get('received_after', '').strip()

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

        # Call the counting function with received_after parameter
        result = count_emails_in_folders(email_addr, app_password, subject_filter, from_filter, received_after if received_after else None)

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5617, debug=False)