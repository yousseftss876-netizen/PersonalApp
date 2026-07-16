# Overview

TSS Gmail Access is a Flask-based web application providing secure, role-based access to multiple Gmail accounts via IMAP. It features a robust authentication system with entity-based access control, allowing different TSS entities (TSS1, TSS2, TSS3, TSSF, TSSW) to manage and view email data through a responsive dashboard. The application has evolved into a multi-service platform, incorporating advanced tools like a Quality Seeds Helper, Blacklist Lookup, Domain Checker, Find News, and Email Extraction, enhancing operational efficiency and data analysis for various business functions within TSS.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Web Framework Architecture
The application uses Flask with Flask-Login for authentication, adhering to a secure MVC pattern. Key components include `app.py` for main logic, `main.py` as the entry point, `login.html` and `dashboard.html` for UI, and text files (`users.txt`, `gmailaccounts.txt`) for user and account configurations.

## Frontend Design
The interface is built with Tailwind CSS for a responsive, mobile-first design. It incorporates custom CSS animations, Font Awesome icons, Phosphor Icons (via unpkg CDN), and JavaScript-enhanced forms for improved user experience. Entity-based color coding is used for visual identification in dropdowns (TSS1: Blue, TSS2: Green, TSS3: Yellow, TSSF: Orange, TSSW: Red). All service pages share a unified sticky glassmorphism header via `templates/_header.html` — it includes the TSS Platform logo, page title with Phosphor icon, and a profile dropdown (Back to Services, Manage Users, Access Management, Sign Out). Pages set `page_title`, `page_icon`, and optionally `back_url`/`back_label` before the include. The services landing page uses a 2026 aesthetic with live search, colored service cards, and animated hover effects.

## Email Processing Architecture
Optimized IMAP integration uses Python's `imaplib` for direct Gmail connections, fetching only email headers and limiting to the last 20 emails per folder for performance. Client-side filtering provides real-time search, and custom functions handle MIME decoding. Comprehensive error handling is in place for connection failures. The system features advanced connection pooling, sharing persistent IMAP connections at the entity level, and uses Server-Sent Events (SSE) for real-time email updates.

## Security Model
Flask-Login manages secure session and authentication. Entity-based access control restricts users to their designated Gmail accounts, with TSSW users having administrative access to all entities. Gmail App Passwords are used for enhanced security, and user management is file-based via `users.txt`. Session persistence is maintained until explicit logout, with session secrets managed via environment variables. The `users.txt` file supports a granular permission system for access to various services.

## Data Flow
Users log in, are authenticated against `users.txt`, and their entity access determines available Gmail accounts. Upon selection, an IMAP connection is established using credentials from `gmailaccounts.txt`. Email data is fetched, processed, and rendered via Flask templates.

## Service Architecture
The platform includes several services:
- **TSS Gmail Access**: Core email viewing with folder categorization (Primary, Promotions, Social, Updates, Forums, Spam).
- **Quality Seeds Helper**: Automates seed quality enhancement, including image fetching (using Playwright), subject generation, and image management with user-specific data storage.
- **Blacklist Lookup**: Parallel processing with SSE for real-time progress, UI redesign with pagination, search, and column filters.
- **Domain Checker**: Consolidated MX/TXT/SPF/A Records lookup with SSE, parallel processing, color-coded badges, and CSV export.
- **TSSW Rapport**: A dedicated reporting service for TSSW users.
- **Find News**: Service with Gmail account management capabilities.
- **TSS Extract Emails**: Advanced email analysis, SPF/DKIM status, sender IP extraction, and CSV export.
- **IPs Checker**: Server and IP address management with CIDR class validation, event tracking (Available/Down/custom), IP search/lookup, and event history. Data stored in JSON files (`ip_checker_data.json`, `ip_checker_events.json`, `ip_checker_event_types.json`). Two permissions: `ips_cheker` (view/search) and `add_ip_cheker` (full CRUD management).
- **Domain Founder**: SPF record analysis across managed domains. Supports 4 process types: Include Records (extracts root domains from SPF include directives), A Records (extracts domains from SPF a: directives), IP Check (verifies if IPs are authorized in SPF), Query Search (searches for text within SPF records). Features parallel DNS lookups (20 workers), persistent per-user domain storage, real-time progress tracking with polling, copy/export CSV, and process limit enforcement. Backend in `domain_founder.py`, data stored in `domain_founder_data/` directory. Two permissions: `domain_founder` (access) and `unlimited_domain_founder` (unlimited concurrent processes).
- **Email Founder**: Gmail email extraction service. Users can add/manage Gmail accounts (stored in `email_founder_extraction.txt`), select folders (Inbox, Primary, Promotions, Social, Updates, Forums, Starred, Custom Label), set email range (From/To), choose extraction mode (Full Source, Text Plain, Text HTML), and download extracted emails as TXT files with `__SEP__` separator. One process at a time per user. Process history persisted in JSON. Configurable per-user extraction quota (default 10, stored in `user_quotas.json`). Backend in `email_founder.py`, data stored in `email_founder_data/` directory. Permission: `email_founder`.
- **Access Management**: IP-based login restriction system. Admins can define a whitelist of allowed IP addresses; users not on the whitelist are blocked at login. Specific users can be added to an exempt list to bypass IP restrictions entirely. When the IP list is empty, no restrictions apply. Data stored in `access_management.json`. Permission: `access_management`. Accessible via the "Access Management" button in the services header.
- **Useful Extensions**: Browser extension repository for sharing Chrome, Firefox, and IceDragon extensions (.rar files). Supports two extension types: Normal Extension (direct download) and Updated Extension (version history with descriptions and timestamps, only latest version downloadable). Updated Extensions allow owners to upload new versions with update descriptions; the history page shows all past updates chronologically. Data stored in `extensions.json`, files in `static/downloads/`. Includes installation guides for each browser. Two permissions: `download_extension` (view/download) and `add_extensions` (upload/update/delete management).

# External Dependencies

## Core Dependencies
- **Flask**: Web framework.
- **Flask-Login**: Authentication and session management.
- **imaplib**: Python standard library for IMAP access.
- **email**: Python standard library for email parsing.

## Frontend Dependencies
- **Tailwind CSS**: Utility-first CSS framework (via CDN).
- **Font Awesome**: Icon library (via CDN).

## Email Service Integration
- **Gmail IMAP**: Direct integration with `imap.gmail.com:993`.
- **App Passwords**: Required for Gmail account authentication.

## Development Environment
- **Python Logging**: For debugging and error tracking.
- **Flask Development Server**: For local development.

## Hosting Requirements
- **Port Configuration**: Runs on port 5000.
- **Environment Variables**: `SESSION_SECRET` for security.
- **File Permissions**: Read access to `users.txt` and `gmailaccounts.txt`.