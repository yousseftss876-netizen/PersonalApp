[x] 1. Clean and install the required packages properly
[x] 2. Fix Python import issues and dependencies
[x] 3. Configure proper server binding for Replit environment
[x] 4. Restart the workflow to see if the project is working
[x] 5. Verify the project is working using the feedback tool
[x] 6. Updated TSS Extract Emails service with user improvements
[x] 7. Migration completed successfully - TSS Extract Emails app is running on Replit
[x] 8. Fixed gunicorn dependency installation for Replit environment
[x] 9. Optimized TSS Extract Emails service - reduced extraction time from 120+ seconds to 5-10 seconds using batch processing
[x] 10. Added "Find News" service - displays news Gmail accounts with last 50 inbox emails and copy source functionality
[x] 11. Updated users.txt format to include Name field: entity,Name,username,password[,permissions]
[x] 12. Updated User class and authentication to support new format with Name and multiple permissions (ok, allow_add_gmail_of_news)
[x] 13. Updated all templates to display user's Name instead of username in welcome messages and navigation bars
[x] 14. Moved "Find News" service from dashboard to services page for easy access
[x] 15. Added Gmail account management for users with "allow_add_gmail_of_news" permission - add/edit/delete news accounts
[x] 16. Reinstalled all required Python packages (gunicorn, Flask, Flask-Login, Flask-SQLAlchemy, psycopg2-binary, email-validator)
[x] 17. Verified application is running successfully on Replit with workflow status: RUNNING
[x] 18. Confirmed TSS Gmail Access login page displays correctly and application is fully functional
[x] 19. Migration import to Replit environment completed successfully - all systems operational
[x] 20. Fixed JavaScript scope issue for Find News manage accounts - buttons now work correctly
[x] 21. Added explicit window object bindings for all account management functions (add, update, delete)
[x] 22. Redesigned Find News dashboard with modern glassmorphism UI, gradients, and improved styling
[x] 23. Redesigned Manage Accounts modal with cleaner forms and better visual design
[x] 24. Configured workflow with proper webview output type and port 5000 binding
[x] 25. Verified all Python packages installed correctly (gunicorn, Flask, Flask-Login, Flask-SQLAlchemy, psycopg2-binary, email-validator)
[x] 26. Confirmed application is running and accessible - TSS Gmail Access login page displaying correctly
[x] 27. Final migration to Replit environment completed - all systems operational and ready for use
[x] 28. Re-verified Python packages installation after environment reset (gunicorn, Flask, Flask-Login, Flask-SQLAlchemy, psycopg2-binary, email-validator)
[x] 29. Reconfigured workflow with webview output type and port 5000 binding for proper Replit environment integration
[x] 30. Confirmed application is running successfully - workflow status: RUNNING
[x] 31. Verified TSS Gmail Access login page displays correctly with screenshot - all systems fully operational
[x] 32. Import migration to Replit environment completed and verified - application ready for use
[x] 33. Created user_extraction_accounts.txt for user-specific Gmail accounts storage
[x] 34. Updated backend API endpoints to support user-specific extraction accounts (each user sees only their own accounts)
[x] 35. Legacy TSSW extraction accounts now visible only to y.ouiguemane user
[x] 36. Updated Extract Emails template to show Manage Accounts button for all users with Extract Emails permission
[x] 37. Added auto-update for DMARC prefix field - textarea updates automatically as user types
[x] 38. Improved DMARC results layout with side-by-side design (results table left, output textarea right) to avoid scrolling
[x] 39. Reinstalled packages and reconfigured workflow for Replit environment migration - application running successfully
[x] 40. Optimized DMARC lookup speed with parallel processing (ThreadPoolExecutor) - up to 20 concurrent DNS lookups
[x] 41. Added real-time progress display in loading overlay showing "Processing X/Y..." during DMARC lookups
[x] 42. Reduced DNS resolver timeout from 5s to 2s for faster responses
[x] 43. Implemented SSE (Server-Sent Events) endpoint for streaming DMARC progress updates
[x] 44. Reinstalled Python packages and reconfigured workflow with webview output for Replit environment
[x] 45. Application running successfully on port 5000 - workflow status: RUNNING
[x] 46. Import migration to Replit environment completed - all systems operational
[x] 47. DMARC: Added copy button for filtered domains in results section
[x] 48. SPF: Added single prefix checkbox option - applies same prefix to all domains when checked
[x] 49. SPF: Added three mutually exclusive record type options (IPs, A records, Includes)
[x] 50. Updated backend API to support new SPF generation with A records and Includes
[x] 51. Fixed prefixed domains handling for single-prefix mode and non-IP SPF types
[x] 52. Fixed A records SPF format: now outputs prefix.domain,TXT,v=spf1 a:subdomain.prefix.domain -all
[x] 53. Fixed Include records format: now outputs _spf.domain,TXT,v=spf1 include:domain1 include:domain2 -all (no prefix before _spf)
[x] 54. Added parallel processing for MX lookups with ThreadPoolExecutor (up to 20 concurrent lookups)
[x] 55. Added parallel processing for TXT lookups with ThreadPoolExecutor (up to 20 concurrent lookups)
[x] 56. Added SSE streaming endpoints for MX and TXT lookups with real-time progress display
[x] 57. Updated frontend MX lookup to use streaming endpoint with "Processing X/Y..." progress display
[x] 58. Updated frontend TXT lookup to use streaming endpoint with "Processing X/Y..." progress display
[x] 59. MX and TXT lookups now have copy filtered domains button (already implemented)
[x] 60. Final import migration to Replit environment completed - Dec 13, 2025
[x] 61. All packages reinstalled and workflow reconfigured with webview output type
[x] 62. Application verified running successfully with screenshot confirmation
[x] 63. Added Export CSV button for DMARC filtered domains - exports based on current filter (All/Found/Not Found)
[x] 64. CSV export includes Domain and DMARC Record columns
[x] 65. Added gmass permission check - TSS Gmail Access service now only visible to users with "gmass" in their permissions
[x] 66. Updated User class to support has_gmass_permission property
[x] 67. All changes verified and application running successfully - Dec 14, 2025
[x] 68. Final environment migration - Dec 14, 2025 - packages reinstalled, workflow configured, application running successfully
[x] 69. Environment migration - Dec 19, 2025 - reinstalled Python packages and reconfigured workflow with webview output
[x] 70. Verified application running successfully on port 5000 with screenshot confirmation - TSS Gmail Access login page displayed correctly
[x] 71. Import migration to Replit environment completed and verified - all systems operational
[x] 72. Environment migration - Dec 19, 2025 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 73. Reconfigured workflow with webview output type and port 5000 binding
[x] 74. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 75. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 76. Final import migration to Replit environment completed - all items marked as done
[x] 77. Blacklist Lookup Performance Optimization - Dec 19, 2025:
    - Added parallel processing with ThreadPoolExecutor (30 concurrent workers)
    - Implemented SSE streaming for real-time progress display (X/Y format with progress bar)
    - Moved DQS_KEY to environment variable for security
[x] 78. Blacklist Lookup UI Redesign - Dec 19, 2025:
    - Modern glassmorphism design matching app style (purple/blue gradients)
    - Pagination with 16 items per page, prev/next buttons and page numbers
    - Search bar for filtering by server name, IP, or domain
    - Copy Clean IPs and Export CSV buttons moved to top toolbar
    - Column filters for all blacklist types (CSS, PBL, XBL, SBL, Barracuda, DBL)
    - Loading overlay with real-time progress indicator (X/Y with animated progress bar)
    - Responsive table with horizontal scrolling
[x] 79. Environment migration - Dec 20, 2025 - reinstalled Python packages and reconfigured workflow with webview output
[x] 80. Application running successfully on port 5000 - workflow status: RUNNING
[x] 81. Import migration to Replit environment completed - Dec 20, 2025 - all items marked as done
[x] 82. Blacklist Lookup service updates - Dec 20, 2025:
    - Added SBL card to stats section with green color (stat-sbl)
    - Updated Status filter options: changed from (ACTIVE, PAUSED, PROD) to (ALL, PAUSED, PRODUCTION)
    - Added copy icons to Serveur, IP, Domain column headers
    - Updated updateStatistics() to display SBL stats
[x] 83. Updated copy functionality for Blacklist Lookup - Dec 20, 2025:
    - Column headers (Serveur, IP, Domain) are now clickable
    - Copy icons appear on header hover (opacity transition)
    - Clicking copy on column header copies ALL values from that column based on current filter
    - copyColumnValues() function filters and copies entire column with newline separators
    - Toast shows count of copied values (e.g., "Copied 25 IPs to clipboard!")
    - Works with all active filters applied to the table
[x] 84. Application running successfully after copy functionality updates - workflow status: RUNNING - no errors detected
[x] 85. Environment migration - Dec 21, 2025 - reinstalled Python packages and reconfigured workflow with webview output
[x] 86. Verified application running successfully on port 5000 - workflow status: RUNNING
[x] 87. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 88. Import migration to Replit environment completed - all items marked as done
[x] 89. Environment migration - Dec 22, 2025 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 90. Reconfigured workflow with webview output type and port 5000 binding
[x] 91. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 92. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 93. Final import migration to Replit environment completed - all items marked as done
[x] 94. Blacklist Lookup Updates - Dec 22, 2025:
    - Fixed "Clean" filter option - changed values from "not Listed" to "Clean" to match actual data
    - Fixed Status filter to be case-insensitive (PAUSED/paused, PRODUCTION/Production work equally)
    - Changed input format separator from ":" (colon) to ";" (semicolon) for IPv6 compatibility
    - New format: SERVEUR;IP;DOMAIN;STATUS (supports both IPv4 and IPv6 addresses)
    - Added IPv6 regex validation and proper IPv6 blacklist lookups using expanded format
    - Updated placeholder text and format instructions in the UI
[x] 95. Environment migration - Jan 13, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 96. Reconfigured workflow with webview output type and port 5000 binding
[x] 97. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 98. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 99. Import migration to Replit environment completed - all items marked as done
[x] 100. Environment migration - Jan 21, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 101. Reconfigured workflow with webview output type and port 5000 binding
[x] 102. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 103. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 104. Import migration to Replit environment completed - all items marked as done
[x] 105. Blacklist Lookup: Added DQSKeyManager for load balancing between multiple DQS keys
[x] 106. Blacklist Lookup: Updated check_blacklists_stream to use a single key per process while cycling through available keys
[x] 107. Blacklist Lookup: Verified multiple DQS keys support (f3jqdoqp... and tfpurh2d...)
[x] 108. Blacklist Lookup: Fixed "Copy Clean IPs" button with robust fallback for non-secure contexts or permission issues
[x] 109. Environment migration - Jan 28, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 110. Reconfigured workflow with webview output type and port 5000 binding
[x] 111. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 112. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 113. Import migration to Replit environment completed - all items marked as done
[x] 114. Quality Seeds Helper Service - Jan 28, 2026:
    - Added new "Quality Seeds Helper" service with `quality_helper` permission
    - Implemented "Get Images" script using Playwright for Bing image search
    - Created user-specific storage folders for process data and images
    - Added keywords textarea input (one keyword per line)
    - Added image count input (1-50 images per process)
    - Implemented real-time progress tracking during image generation
    - Created image gallery display with results
    - Added subjects textarea with copy button
    - Added image links textarea with copy button
    - Implemented ZIP download for all images
    - Added process deletion with full cleanup (images + data)
    - Created sidebar navigation for future script additions
    - Permission check: `quality_helper` required to access service
[x] 115. Environment migration - Jan 28, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 116. Reconfigured workflow with webview output type and port 5000 binding
[x] 117. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 118. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 119. Import migration to Replit environment completed - all items marked as done
[x] 120. Environment migration - Jan 29, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator)
[x] 121. Reconfigured workflow with webview output type and port 5000 binding
[x] 122. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 123. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 124. Import migration to Replit environment completed - all items marked as done
[x] 125. Quality Seeds Helper: Added "Get PDFs" script - Jan 29, 2026:
    - New sidebar item for "Get PDFs" in Quality Seeds Helper service
    - PDF search using arXiv (academic papers)
    - Keywords input (one per line) with max 50 PDFs per process
    - PDF filenames: 5+ words, no numbers, max 5MB each
    - User-specific PDF storage in quality_helper_data/{username}/pdfs/
    - Real-time progress display ("Processing X/Y..." with progress bar)
    - Results display with PDF grid, subjects textarea, and PDF links textarea
    - Copy buttons for subjects and PDF links textareas
    - Download all PDFs as ZIP file
    - Delete process (removes PDFs and data)
    - One process per user (must delete before creating new)
    - Cannot start new process while one is running
[x] 126. Added PDF API routes: /api/quality-helper/pdf/status, start, delete, download-zip, serve
[x] 127. Added PDF functions to quality_helper.py: run_pdf_generation, storage, arXiv integration
[x] 128. Installed beautifulsoup4 package for HTML parsing
[x] 129. Verified application running successfully - workflow status: RUNNING
[x] 130. Get PDFs script implementation completed - all items marked as done
[x] 131. Environment migration - Jan 31, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests)
[x] 132. Reconfigured workflow with webview output type and port 5000 binding
[x] 133. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 134. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 135. Import migration to Replit environment completed - all items marked as done
[x] 136. Environment migration - Feb 03, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests)
[x] 137. Reconfigured workflow with webview output type and port 5000 binding
[x] 138. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 139. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 140. Import migration to Replit environment completed - Feb 03, 2026 - all items marked as done
[x] 141. News Subscription: Added pause/resume functionality with JSON persistence
[x] 142. News Subscription: Supported multiple concurrent processes for users with "infinity-process" permission
[x] 143. News Subscription: Implemented global logging of successful domains to all_successfully_domain.txt
[x] 145. Extract Emails: Optimized IMAP connection with 30s timeout to prevent freezes
[x] 146. Extract Emails: Implemented connection health check and auto-reconnect logic during batch processing
[x] 148. Extract Emails: Added 3x retry logic for SSL handshake timeouts with increased 60s timeout
[x] 149. Logging: Enhanced Flask logs to include [username] for all requests using a custom logging filter
[x] 150. News Subscription: Fixed "Stop" button by adding explicit cleanup and delayed memory removal for better status feedback
[x] 151. Environment migration - Feb 05, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, playwright, faker)
[x] 152. Reconfigured workflow with webview output type and port 5000 binding
[x] 153. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 154. Import migration to Replit environment completed - Feb 05, 2026 - all items marked as done
[x] 155. Environment migration - Feb 08, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, playwright, faker)
[x] 156. Reconfigured workflow with webview output type and port 5000 binding
[x] 157. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 158. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 159. Import migration to Replit environment completed - Feb 08, 2026 - all items marked as done
[x] 160. Environment migration - Feb 08, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, playwright, faker, dnspython)
[x] 161. Reconfigured workflow with webview output type and port 5000 binding
[x] 162. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 163. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 164. Import migration to Replit environment completed - Feb 08, 2026 - all items marked as done
[x] 165. User Management Feature - Feb 08, 2026:
    - Added "Manage Users" service visible to users with "user_management" permission
    - User CRUD: Add, Edit, Delete users with entity, name, username, password
    - Permission management: Toggle all service permissions per user via UI chips
    - News Subscription limits: Set max concurrent processes per user
    - Domain quota: Monthly domain processing quota with usage tracking (user_quotas.json)
    - Quota banner: Users see remaining domains in News Subscription page
    - Process limit enforcement: Non-infinity users limited by max_processes setting
    - Domain quota enforcement: Users blocked when monthly quota exhausted
    - Quota reset: Automatic monthly reset + manual reset button for admins
    - Comma validation: Prevents commas in user fields to protect CSV format
    - Monthly quota persistence: Reset persisted to disk on load
    - Added user_management permission to redouan and y.ouiguemane users
[x] 166. News Subscription Performance Optimization - Feb 08, 2026:
    - Global browser semaphore: max 3 simultaneous Chromium browsers across all users/processes
    - Reduced per-process concurrent browsers from 4 to 2
    - Resource-friendly Chromium flags (--single-process, --max-old-space-size=128, etc.)
    - Thread priority lowering via os.nice(10) so Flask stays responsive
    - Batch processing with delays between batches instead of all-at-once asyncio.gather
    - Adaptive delays between domains based on active process count
    - Non-blocking semaphore acquisition with 60s timeout (cancellable on stop)
    - Removed duplicate stop_user_process function (kept version with history saving)
[x] 167. Environment migration - Feb 14, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, playwright, faker, dnspython)
[x] 168. Reconfigured workflow with webview output type and port 5000 binding
[x] 169. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 170. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 171. Import migration to Replit environment completed - Feb 14, 2026 - all items marked as done
[x] 172. Environment migration - Feb 15, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, playwright, faker, dnspython)
[x] 173. Reconfigured workflow with webview output type and port 5000 binding
[x] 174. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 175. Screenshot confirmed TSS Gmail Access login page displays correctly
[x] 176. Import migration to Replit environment completed - Feb 15, 2026 - all items marked as done
[x] 177. Domain Founder Service - Feb 15, 2026:
    - Added new "Domain Founder" service with `domain_founder` permission
    - Added `unlimited_domain_founder` permission for unlimited concurrent processes
    - Created domain_founder.py backend module with persistent domain storage per user
    - Implemented 4 process types: Include Records, A Records, IPs Check, Query Search
    - Parallel DNS lookups using ThreadPoolExecutor (20 concurrent workers)
    - Real-time progress tracking with 2-second polling (supports navigation to other services)
    - Results displayed in well-designed tables with per-type columns
    - Copy results and Export CSV buttons for each completed process
    - Process management: start, stop, delete with proper permission checks
    - Process limit enforcement: 1 process at a time unless unlimited_domain_founder permission
    - Per-user data isolation (domain storage and process isolation)
    - Service card added to services.html with matching glassmorphism design
    - Added domain_founder and unlimited_domain_founder to ALL_PERMISSIONS for user management
    - Granted domain_founder and unlimited_domain_founder to admin users (redouan, y.ouiguemane)
[x] 178. Application running successfully after Domain Founder implementation - workflow status: RUNNING
[x] 179. Environment migration - Apr 09, 2026 - reinstalled Python packages (gunicorn, flask, flask-login, flask-sqlalchemy, psycopg2-binary, email-validator, beautifulsoup4, requests, faker, dnspython)
[x] 180. Reconfigured workflow with webview output type and port 5000 binding using python -m gunicorn
[x] 181. Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 182. Screenshot confirmed TSS Services Access login page displays correctly
[x] 183. Import migration to Replit environment completed - Apr 09, 2026 - all items marked as done
[x] 184. Domain Founder: Added "CNAME Records" process type - Apr 09, 2026:
    - Looks up CNAME records for each domain in "My Domains"
    - Checks if each CNAME target has an SPF record
    - Displays only CNAMEs with no SPF record (Main Domain + Canonical Name columns)
    - Skips domains with no CNAME records or whose CNAMEs already have SPF
    - Parallel processing with ThreadPoolExecutor (20 workers)
    - Full Copy and Export CSV support for results
[x] 185. Environment migration - Apr 10, 2026 - fixed gunicorn startup issue:
    - Fixed "No module named gunicorn" error by using full path to .pythonlibs gunicorn binary
    - Created start.sh wrapper script using /home/runner/workspace/.pythonlibs/bin/gunicorn
    - Reconfigured workflow with webview output type and port 5000 binding
    - Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
    - Screenshot confirmed TSS Services Access login page displays correctly
[x] 186. Import migration to Replit environment completed - Apr 10, 2026 - all items marked as done
[x] 187. SubDomain Finder service added - Apr 10, 2026:
    - Created subdomain_finder.py backend module with full process lifecycle management
    - Uses /root/go/bin/subfinder binary (already installed on machine)
    - Global semaphore (max 5 concurrent subfinder calls) protects server performance across all users
    - Each process runs up to 3 domains in parallel; multiple processes can run simultaneously
    - Pause/Resume: waits for current subfinder calls to finish before pausing (no kills)
    - Stop: signals stop_flag, worker exits cleanly after current domain completes
    - Results persisted to disk per user (subdomain_finder_data/); survive restarts
    - Process naming: optional custom name or auto-generated random adjective-noun-number
    - Real-time UI: polls every 3s, shows per-domain progress + live subdomain tags
    - CSV download per process (Domain, Subdomain columns)
    - Created templates/subdomain_finder.html with tabs (Processes / My Domains)
    - Added service card to services.html (Recon tag, sky blue theme)
    - Permission: subdomain_finder added to ALL_PERMISSIONS
    - User class: has_subdomain_finder_permission, sf_max_processes, sf_max_domains
    - Manage Users: SubDomain Finder Limits section (max concurrent processes + max domains/process)
    - Admin endpoint: /api/subdomain-finder/admin/all-processes for managers to view all active processes
    - All admin users (redouan, y.ouiguemane) can grant subdomain_finder permission via Manage Users UI
[x] 188. SubDomain Finder: Per-process domain input - Apr 10, 2026:
    - Removed shared "My Domains" pool concept from the UI
    - Each process now has its own domains textarea entered at launch time
    - New Process panel has: optional name input + domains textarea (one per line) + Launch button
    - Domain count shown live as user types (real-time counter)
    - Backend sf_start route updated to accept domains from request body instead of loading from file
    - Client-side validation: empty domain list blocked, max domains limit checked before sending
[x] 189. Environment migration - Apr 14, 2026 - fixed gunicorn startup issue:
    - Fixed "No module named gunicorn" error by reconfiguring workflow to use bash start.sh
    - Workflow uses /home/runner/workspace/.pythonlibs/bin/gunicorn via start.sh wrapper
    - Reconfigured workflow with webview output type and port 5000 binding
    - Verified application running successfully - workflow status: RUNNING, gunicorn listening on port 5000
[x] 190. Import migration to Replit environment completed - Apr 14, 2026 - all items marked as done
