# Capstone Intake Program: Detailed End-to-End Behavior

This document explains exactly what the intake program does, from receiving an email to parsing, scanning, storing, and post-processing.

Primary code: `src/intake_worker.py`

## 1) What the program is

The program is a polling intake worker that:

1. Pulls incoming email messages from a dedicated mailbox.
2. Always fetches full RFC822 message bytes (raw `.eml` content).
3. Parses the raw bytes into a structured email object.
4. Routes each message to a `user_id` (multi-tenant mapping).
5. Applies abuse controls (sender allowlist).
6. Performs a lightweight scan.
7. Stores either:
   - a processed case, or
   - a quarantined record.
8. Marks/moves messages as processed in Gmail/IMAP so they are not repeatedly reprocessed.

It supports two ingestion modes:

1. Gmail API mode (preferred).
2. IMAP mode (fallback).

## 2) Startup sequence

When you run:

```powershell
python src/intake_worker.py --once
```

the worker executes this flow:

1. Loads `.env` if present (`load_env_file`).
2. Configures logging (`LOG_LEVEL`, default `INFO`).
3. Parses CLI args (`--once` optional).
4. Builds `Settings` from environment variables.
5. Validates configuration:
   - `GMAIL_MODE` must be `gmail_api` or `imap`.
   - polling interval and rate limits must be valid.
   - Gmail API mode requires OAuth env vars.
   - IMAP mode requires IMAP credentials.
   - optional `DEFAULT_USER_ID` must match `[A-Za-z0-9._-]+`.
6. Chooses storage backend:
   - Postgres if `DATABASE_URL` starts with `postgres://` or `postgresql://`.
   - otherwise SQLite (`INTAKE_DB_PATH`).
7. Ensures DB schema exists.
8. Optionally loads approved senders from `APPROVED_SENDERS_FILE`:
   - if file exists: load and upsert entries.
   - if missing: warning only, continue.
9. Instantiates processor + ingestion worker (Gmail API or IMAP).
10. Runs one poll (`--once`) or an infinite loop with sleep.

## 3) Database tables and purpose

Both SQLite and Postgres use the same logical schema:

1. `approved_senders`
   - `(user_id, sender_email)` composite key.
   - source-of-truth allowlist for abuse controls.

2. `cases`
   - stores accepted/processed emails.
   - unique key: `(intake_mode, provider_message_id)`.

3. `quarantined_messages`
   - stores rejected emails with a `reason`.
   - unique key: `(intake_mode, provider_message_id)`.

## 4) Ingestion mode A: Gmail API (preferred)

### 4.1 Poll step

Each poll does:

1. Call `users.messages.list` with:
   - query: `GMAIL_QUERY` (default: `is:unread newer_than:7d`)
   - `maxResults=100`, with pagination.
2. Collect message IDs.

### 4.2 Fetch raw message step

For each message ID:

1. Call `users.messages.get` with `format=raw`.
2. Read `raw` field (base64url).
3. Add needed base64 padding.
4. Decode to raw RFC822 bytes.

### 4.3 OAuth/token behavior

The worker does not store access tokens in DB.

It uses:

1. `GOOGLE_REFRESH_TOKEN` from env.
2. Exchanges refresh token at `https://oauth2.googleapis.com/token`.
3. Caches short-lived access token in memory only.
4. Refreshes when expired.

### 4.4 API reliability controls

For Gmail API calls:

1. Rate limiting:
   - minimum spacing between requests:
     `1 / GMAIL_API_RATE_LIMIT_PER_SEC`.
2. Retries with backoff on:
   - HTTP 429/500/502/503/504,
   - network errors/timeouts.
3. Backoff:
   - exponential from `GMAIL_API_BACKOFF_BASE_SECONDS`,
   - capped at `GMAIL_API_BACKOFF_MAX_SECONDS`,
   - adds jitter.
4. 401 behavior:
   - clear cached token and retry.

### 4.5 Mark processed step

If message handling status is `processed`, `quarantined`, or `duplicate`:

1. Ensure `GMAIL_LABEL_PROCESSED` exists (create if needed).
2. Modify message labels:
   - remove `UNREAD`,
   - add processed label.

If processing throws an exception, message is not marked processed in that cycle.

## 5) Ingestion mode B: IMAP fallback

Each poll does:

1. Connect TLS to `IMAP_HOST:IMAP_PORT` (`imap.gmail.com:993` typically).
2. Login with `IMAP_USER` / `IMAP_PASSWORD`.
3. Select `IMAP_FOLDER` (default `INBOX`).
4. Ensure `IMAP_PROCESSED_FOLDER` exists (create if missing).
5. Search `UNSEEN` messages.
6. For each message:
   - fetch `(UID RFC822)`,
   - parse UID from server metadata,
   - use UID as provider message ID,
   - process raw RFC822 bytes.
7. If status is `processed`, `quarantined`, or `duplicate`:
   - copy message to processed folder,
   - set `\Seen \Deleted`,
   - after loop, `expunge` to finalize move.
8. Logout.

## 6) Common per-message pipeline (core logic)

This is the exact flow for every raw message, regardless of source.

### Step 1: Duplicate guard

Before parsing:

1. Query `cases` and `quarantined_messages` for `(intake_mode, provider_message_id)`.
2. If found:
   - return status `duplicate`,
   - skip further processing.

### Step 2: Parse raw `.eml`

1. Parse raw bytes with:
   - `BytesParser(policy=policy.default).parsebytes(raw_bytes)`.
2. This builds a MIME-aware email object used for header and body traversal.

### Step 3: Persist raw evidence file

1. Ensure `INTAKE_RAW_DIR` exists.
2. Write raw bytes to disk as:
   - `<UTC timestamp>_<mode>_<provider_id>.eml`
   - with sanitized filename tokens.
3. Save resulting `raw_path` for traceability.

### Step 4: Extract metadata

From parsed email:

1. `user_id`:
   - look in headers: `Delivered-To`, `X-Original-To`, then `To`.
   - parse all addresses.
   - take first local-part matching `scan+<user_id>`.
2. `sender_email`:
   - parse first valid address from `From`.
3. `subject`:
   - `Subject` header (or empty string).
4. `received_at`:
   - parse `Date` header to UTC ISO.
   - fallback to current UTC time if parse fails.
5. `rfc822_message_id`:
   - from `Message-Id` header (if present).

### Step 5: Optional user fallback

If no plus-address `user_id` was found and `DEFAULT_USER_ID` is set:

1. Assign `user_id = DEFAULT_USER_ID`.
2. Log that fallback was used.

### Step 6: Routing validation / quarantine reason 1

If `user_id` is still missing:

1. Insert row into `quarantined_messages` with:
   - `reason = "missing_plus_address_user_id"`.
2. Return `quarantined`.

### Step 7: Abuse control / quarantine reason 2

If sender is missing or not allowlisted for that `user_id`:

1. Check `approved_senders` table for exact normalized sender email.
2. If not approved:
   - insert into `quarantined_messages` with:
     `reason = "sender_not_approved"`.
   - return `quarantined`.

### Step 8: Lightweight scan

If message passes routing + allowlist:

1. Walk MIME parts.
2. Count attachments:
   - any part with attachment disposition,
   - or any part with filename.
3. Count URL markers in text parts:
   - regex search for `http://` or `https://`.
4. Flag risky attachment names by extension:
   - `.bat`, `.cmd`, `.com`, `.exe`, `.jar`, `.js`, `.ps1`, `.scr`, `.vbs`.
5. Produce scan result JSON:
   - `raw_size_bytes`,
   - `attachment_count`,
   - `url_count`,
   - `risky_attachment_names`.

### Step 9: Store processed case

Insert into `cases`:

1. intake metadata (`intake_mode`, provider id, RFC822 message id).
2. tenant + sender metadata (`user_id`, `sender_email`).
3. email metadata (`subject`, `received_at`, `raw_path`).
4. `scan_result_json`.

Insert uses conflict-safe semantics; duplicate inserts are ignored.

### Step 10: Return status

Final status per message:

1. `processed`
2. `quarantined`
3. `duplicate`

The ingestion mode then decides how to mark/move source mailbox item based on status.

## 7) What gets quarantined and why

Current quarantine reasons are:

1. `missing_plus_address_user_id`
   - could not extract `scan+<user_id>` from addressed headers,
   - and no `DEFAULT_USER_ID` fallback set.

2. `sender_not_approved`
   - `From` sender absent or not listed in `approved_senders` for resolved `user_id`.

## 8) Poll loop behavior

In continuous mode:

1. Run one poll cycle.
2. Log handled count.
3. Sleep `max(0, poll_interval - cycle_duration)`.
4. Repeat forever.

In `--once` mode:

1. Run exactly one cycle.
2. Exit.

## 9) Data used by the dashboard

The Next.js dashboard reads from:

1. `cases`
2. `quarantined_messages`
3. `approved_senders`

and supports:

1. viewing recent processed cases/quarantine records,
2. adding/removing approved senders.

## 10) OAuth helper script (one-time setup)

File: `scripts/gmail_oauth.py`

Purpose:

1. Start local temporary HTTP listener on `127.0.0.1:<random_port>`.
2. Build OAuth consent URL with scope `https://www.googleapis.com/auth/gmail.modify`.
3. Open browser for user consent.
4. Receive OAuth authorization code on callback.
5. Exchange code for tokens.
6. Print refresh token.
7. Write/update `GOOGLE_REFRESH_TOKEN=...` in `.env`.

This helper is for developer setup; runtime worker only needs env vars.

## 11) Important operational notes

1. Forwarding format matters:
   - plus-address routing depends on headers preserving `scan+<user_id>@...`.
   - if forwarding strips this, use `DEFAULT_USER_ID` for single-tenant operation.
2. Allowlist is enforced:
   - unapproved senders are intentionally quarantined.
3. Raw `.eml` retention:
   - every non-duplicate message is written to disk before allowlist/quarantine decision.
4. Access tokens are never persisted in DB:
   - only refresh token is stored in env.

## 12) Minimal flow summary

1. Pull unread mail.
2. Fetch full RFC822 bytes.
3. Parse email.
4. Resolve `user_id`.
5. Validate sender allowlist.
6. Scan message content/attachments.
7. Store case or quarantine row.
8. Mark/move source message as processed.
