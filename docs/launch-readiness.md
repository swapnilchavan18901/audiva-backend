# Audiva launch readiness

Updated 2026-11-03. These changes provide a baseline for a controlled, single-instance launch; they do not certify the business, deployment, or every integration as production-ready.

## Implemented protections

| Area | Behavior |
| --- | --- |
| Authentication abuse | Per-IP login, signup, and demo token buckets run before password hashing. Hashing work has four concurrent slots by default; a saturated queue returns 503 with a retry hint. |
| General API traffic | Per-IP and verified-user budgets; separate budgets for writes, voice starts, patient calls, and campaign starts. |
| Public voice integration | Per-IP limits plus organization budgets for valid session tokens and voice starts. Customer API keys remain server-side. |
| Call cleanup | Voice disconnect and campaign stop use a separate 60/minute budget, so an exhausted ordinary API budget does not prevent cleanup. |
| Provider delivery | Twilio answer/status callbacks bypass ordinary client throttles. Their existing scoped token checks remain active, and bodies are bounded at 16 KiB. WebSockets retain the existing voice-session and session-duration controls. |
| Uploads | A 3 MB overall request cap, actual-byte checks for chunked bodies, a 30-second body-read timeout, and the existing 2 MB CSV/10,000-contact limits. Requests are admitted before write handlers execute. |
| Response handling | Request IDs, sanitized unhandled failures, validation responses without echoed input values, no-store API responses, and security headers. |
| Production settings | PostgreSQL, explicit HTTPS dashboard origins, an HTTPS backend URL, and a non-template JWT secret are checked when `APP_ENVIRONMENT=production`. Demo defaults off in production; signup is independently configurable. |
| Operations | Separate liveness and database-readiness endpoints; cleanup runs on shutdown and partial-startup failure. Docker runs one worker, suppresses token-bearing access URLs, and bounds HTTP concurrency. |
| Frontend | Auth options come from the backend; disabled signup/demo actions are hidden. Rate-limit messages respect Retry-After. Error and missing-page screens provide recovery actions. |
| CI | Both repositories have read-only GitHub Actions checks. Backend CI provisions disposable PostgreSQL for locking and migration tests; frontend CI runs tests and a production build. Workflows have not been run in GitHub by this local task. |

## Default rate budgets

These are **token buckets**, not strict fixed-window quotas. A bucket permits the listed initial burst and refills evenly over the period. Budgets are shared across routes in the same category.

| Setting | Default | Key |
| --- | --- | --- |
| API_REQUESTS_PER_MINUTE | 2400 | Client IP |
| USER_REQUESTS_PER_MINUTE | 1200 | Verified user |
| LOGIN_REQUESTS_PER_MINUTE | 10 | Client IP |
| SIGNUP_REQUESTS_PER_HOUR | 5 | Client IP |
| DEMO_REQUESTS_PER_HOUR | 3 | Client IP |
| WRITE_REQUESTS_PER_MINUTE | 120 | IP and verified user, independently |
| VOICE_STARTS_PER_MINUTE | 10 | IP; verified user or public-integration organization |
| PHONE_CALLS_PER_MINUTE | 10 | IP and verified user, independently |
| CAMPAIGN_STARTS_PER_MINUTE | 3 | IP and verified user, independently |
| VOICE_TOKENS_PER_MINUTE | 60 | IP and organization, independently |

All budgets apply within one process. State is bounded by RATE_LIMIT_MAX_BUCKETS (20,000), and inactive entries expire. When full, the limiter rejects new identities rather than dropping active protection. Restarting the process resets budgets. This is **not a distributed limiter, billing quota, DDoS service, or per-customer spending cap**. Before running multiple replicas, move admission budgets into shared storage and redesign the existing in-process voice-session ownership. Do not simply increase Uvicorn workers.

## Production configuration

1. Set `APP_ENVIRONMENT=production` in the deployment environment, not only in a developer shell.
2. Use a managed PostgreSQL database and a strong, generated JWT_SECRET. Keep `.env` and all provider secrets out of images, Git, frontend variables, and support screenshots. The Docker build context now excludes local environments and machine artifacts.
3. Set PUBLIC_BACKEND_URL to the externally reachable HTTPS backend origin and CORS_ORIGINS to the exact HTTPS dashboard origins. Leave REGISTRATION_ENABLED=true for public signup, or false to pause new accounts. Disabling registration does not implement an invitation-based signup flow; existing accounts can still accept workspace invitations.
4. Demo defaults off in production. Set DEMO_ENABLED=true only if you deliberately want anonymous visitors to create persisted demo accounts and use configured voice services. Public demos can incur provider costs. No automatic demo-account retention cleanup is included.
5. Use one backend worker. At the reverse proxy, strip client-supplied forwarding headers and allow only known proxy IPs/networks through Uvicorn's FORWARDED_ALLOW_IPS. The application never trusts X-Forwarded-For directly. An incorrect proxy configuration either combines customers into one IP budget or allows spoofing. Do not expose the origin alongside an edge proxy that you intend to enforce security.
6. Terminate TLS at the trusted proxy; confirm that Uvicorn sees the correct HTTPS scheme. Enable HSTS at the frontend/edge too, after validating HTTPS. API HSTS is emitted for production HTTPS requests.
7. Run `uv run alembic upgrade head` before serving. The Docker startup already does this. Use `/health` for process liveness and `/ready` for database reachability. Readiness does not prove that Twilio, Sarvam, Google, Attendee, or TURN credentials work.
8. Deploy backend changes before the matching frontend: login now reads `/api/v1/auth/options`. Build the frontend with its production NEXT_PUBLIC_API_URL.
9. Private dashboard API docs are disabled in production. Public voice integration docs remain at `/api/v1/public/docs`.
10. Run `uv run python -m app.core.launch_check` with production environment variables. This checks configuration, database access, and migration revision without contacting paid providers.

## Release verification

- Run `uv run ruff check app tests`, `uv run pytest -q`, `npm test`, and `npm run build` in their respective repositories.
- Run PostgreSQL-specific tests against a **disposable** database using OUTREACH_TEST_POSTGRES_URL and MEETING_TEST_POSTGRES_URL. The migration test drops/rebuilds its schema; never point it at production.
- Verify login, duplicate registration, invalid/expired tokens, workspace isolation, member/admin/operator permissions, expired/revoked API keys, rate-limit recovery, and denied CORS origins in staging.
- Try a small CSV, malformed CSV, duplicates, too-large files, an empty contact list, and concurrent campaign starts/stops. Use test contacts you control.
- Verify microphone denial, unsupported WebRTC, TURN connectivity, page navigation during connection, interrupted calls, and reconnect behavior.
- Place a deliberately controlled clinic/outreach call and test busy/no-answer/failure paths. A completed phone call must not be presented as proof that an appointment was booked.
- Test Meet admission, auto-join timing, recurring meetings, cancellation, assistant departure, and memory update/retry. Test the application behind the actual production proxy, not just localhost.
- Verify the security headers and 429/413/408 responses through both the dashboard and mounted public API, including CORS-readable Retry-After and request IDs.

## Operational ownership before public launch

| Item | Required decision or work |
| --- | --- |
| Backups and recovery | Enable automated database backups and encryption with the host; choose retention, restore targets, and an owner. Restore a backup into staging and record the result before accepting customer records. Backups are not provisioned by this code change. |
| Alerts | Connect uptime probes to `/health` and `/ready`. Alert on sustained 5xx, 429, memory/CPU, storage growth, failed jobs, and provider failures. Choose a real on-call/support destination; no notifications have been configured here. |
| Cost containment | Set provider billing alerts and spend limits. Implement persisted per-workspace usage entitlements and a dispatch-time call budget before offering paid/unrestricted telephony. A rate-limited campaign can still contain 10,000 contacts. |
| Accounts | Email verification, password recovery, server-side session revocation, MFA, and an account deletion flow are not implemented. Choose an email provider and sender domain before broad public signup; do not treat registration alone as proof of email ownership. |
| Privacy and customer terms | Decide what data is collected, retention/export/deletion policies, support contact, and permitted calling use. Have applicable privacy, terms, consent, and healthcare-data requirements reviewed for your launch market. No invented legal pages or compliance certification are included. |
| Security review | Review tenant boundaries and production roles, secret rotation, dependencies, and the JS-readable bearer-token cookie architecture. Frontend CSP currently restricts framing, objects, forms, and base URLs; it is not a strict nonce-based script policy. |
| Rollback | Keep the last tested immutable frontend/backend releases. Back up before migrations. Confirm schema compatibility before rolling application code back; do not automatically downgrade a database carrying new customer data. |
| Performance | Load-test the intended concurrency against staging with mocked/budgeted providers. Tune budgets for real polling traffic and users sharing an office/NAT IP. No throughput or load-capacity claim has been established by unit tests. |

## Guidance used

- [Starlette pure ASGI middleware](https://www.starlette.io/middleware/)
- [Uvicorn trusted proxy settings](https://uvicorn.dev/settings/)
- [OWASP REST security guidance](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)
