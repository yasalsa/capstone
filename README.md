# Capstone Gmail Intake + Dashboard

This project now includes:

1. A dual-mode Gmail intake worker (Gmail API preferred + IMAP fallback).
2. A web dashboard for operations and allowlist management (Next.js, Vercel-ready).

## Project Structure

- `src/intake_worker.py`: ingestion worker with Gmail API + IMAP modes.
- `scripts/gmail_oauth.py`: one-time local OAuth helper to obtain refresh token.
- `app/*`: Next.js dashboard UI.
- `lib/*`: dashboard data layer.
- `db/postgres_schema.sql`: SQL schema for Postgres deployments.
- `.env.example`: environment variables template.

## Key Features

### Intake Modes

- `gmail_api`:
  1. Poll every 60s with query `is:unread newer_than:7d`.
  2. Fetch message `format=raw`.
  3. Decode RFC822, parse, scan, store.
  4. Remove `UNREAD` + apply `GMAIL_LABEL_PROCESSED`.
- `imap` fallback:
  1. Poll `UNSEEN` from configured folder.
  2. Fetch full `RFC822`.
  3. Parse, scan, store.
  4. Move to processed folder.

### Multi-Tenant Mapping

- Extracts `user_id` from plus-addressing in `Delivered-To` / `X-Original-To` / `To`.
- Expected format: `scan+<user_id>@gmail.com`.
- Optional single-tenant fallback: set `DEFAULT_USER_ID=...` if forwarded messages do not preserve plus-address headers.

### Abuse Controls

- Only approved senders per user are processed.
- Unknown sender or missing user routing is quarantined.

### Dashboard

- KPI cards: cases, quarantine, allowlist totals.
- Recent processed case table.
- Recent quarantine table.
- Add/remove approved senders by `user_id`.

## Database Mode

The intake worker supports:

1. SQLite (default): `INTAKE_DB_PATH=data/intake.db`
2. Postgres (recommended for Vercel): set `DATABASE_URL=postgres://...`

When `DATABASE_URL` starts with `postgres://` or `postgresql://`, worker auto-creates the schema in Postgres.

## Environment Setup

Copy `.env.example` to `.env` and set values.

### Required for Gmail API mode

- `GMAIL_MODE=gmail_api`
- `GOOGLE_CLIENT_ID=...`
- `GOOGLE_CLIENT_SECRET=...`
- `GOOGLE_REFRESH_TOKEN=...`
- `GMAIL_USER=me`
- `GMAIL_LABEL_PROCESSED=processed`

### Required for IMAP fallback mode

- `GMAIL_MODE=imap`
- `IMAP_HOST=imap.gmail.com`
- `IMAP_PORT=993`
- `IMAP_USER=...`
- `IMAP_PASSWORD=...`
- `IMAP_FOLDER=INBOX`
- `IMAP_PROCESSED_FOLDER=Processed`

### Sender allowlist bootstrap (optional)

- `APPROVED_SENDERS_FILE=approved_senders.json`
- Start from `approved_senders.example.json`.
- If this file is missing, worker logs a warning and continues with allowlist entries already in the database.

## OAuth Helper

Run once:

```powershell
python scripts/gmail_oauth.py
```

It opens browser consent, prints refresh token, and writes `GOOGLE_REFRESH_TOKEN` into `.env`.

## Run Intake Worker

Install Python deps:

```powershell
pip install -r requirements.txt
```

Run continuously:

```powershell
python src/intake_worker.py
```

Run one cycle:

```powershell
python src/intake_worker.py --once
```

## Run Dashboard (Local)

Install Node deps:

```powershell
npm install
```

Run dev server:

```powershell
npm run dev
```

Dashboard requires `DATABASE_URL` pointing at Postgres.

## Deploy Dashboard to Vercel

1. Push this repo to GitHub.
2. Import project in Vercel.
3. Set environment variable:
   - `DATABASE_URL` (Vercel Postgres or any hosted Postgres).
4. Deploy.

Recommended production setup:

1. Use Vercel Postgres for dashboard data.
2. Run the intake worker on a separate always-on host (VM/container/cron) with the same `DATABASE_URL`.
3. Keep Gmail polling out of Vercel serverless functions.
