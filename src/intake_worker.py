#!/usr/bin/env python3
"""
Dual-mode Gmail intake worker.

Modes:
- Gmail API (`GMAIL_MODE=gmail_api`) - preferred.
- IMAP (`GMAIL_MODE=imap`) - fallback.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import imaplib

try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover - optional dependency for postgres mode
    psycopg = None


LOGGER = logging.getLogger("intake_worker")

USER_PLUS_RE = re.compile(r"^scan\+([A-Za-z0-9._-]+)$", re.IGNORECASE)
USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_FILE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
URL_RE = re.compile(r"https?://", re.IGNORECASE)
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def normalize_email(value: str) -> str:
    return value.strip().lower()


def safe_file_token(value: str) -> str:
    return SAFE_FILE_TOKEN_RE.sub("_", value)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass
class Settings:
    gmail_mode: str
    poll_interval_seconds: int
    database_url: Optional[str]
    default_user_id: Optional[str]
    db_path: Path
    raw_dir: Path
    approved_senders_file: Optional[Path]
    gmail_user: str
    gmail_query: str
    gmail_label_processed: str
    gmail_api_rate_limit_per_sec: float
    gmail_api_max_retries: int
    gmail_api_backoff_base_seconds: float
    gmail_api_backoff_max_seconds: float
    google_client_id: Optional[str]
    google_client_secret: Optional[str]
    google_refresh_token: Optional[str]
    imap_host: str
    imap_port: int
    imap_user: Optional[str]
    imap_password: Optional[str]
    imap_folder: str
    imap_processed_folder: str

    @classmethod
    def from_env(cls) -> "Settings":
        approved_file_raw = os.getenv("APPROVED_SENDERS_FILE")
        default_user_id_raw = (os.getenv("DEFAULT_USER_ID") or "").strip()
        return cls(
            gmail_mode=os.getenv("GMAIL_MODE", "gmail_api").strip().lower(),
            poll_interval_seconds=env_int("GMAIL_POLL_INTERVAL_SECONDS", 60),
            database_url=os.getenv("DATABASE_URL"),
            default_user_id=default_user_id_raw or None,
            db_path=Path(os.getenv("INTAKE_DB_PATH", "data/intake.db")),
            raw_dir=Path(os.getenv("INTAKE_RAW_DIR", "data/raw")),
            approved_senders_file=Path(approved_file_raw) if approved_file_raw else None,
            gmail_user=os.getenv("GMAIL_USER", "me"),
            gmail_query=os.getenv("GMAIL_QUERY", "is:unread newer_than:7d"),
            gmail_label_processed=os.getenv("GMAIL_LABEL_PROCESSED", "processed"),
            gmail_api_rate_limit_per_sec=float(os.getenv("GMAIL_API_RATE_LIMIT_PER_SEC", "5")),
            gmail_api_max_retries=env_int("GMAIL_API_MAX_RETRIES", 5),
            gmail_api_backoff_base_seconds=float(
                os.getenv("GMAIL_API_BACKOFF_BASE_SECONDS", "1.0")
            ),
            gmail_api_backoff_max_seconds=float(
                os.getenv("GMAIL_API_BACKOFF_MAX_SECONDS", "30.0")
            ),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID"),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            google_refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
            imap_host=os.getenv("IMAP_HOST", "imap.gmail.com"),
            imap_port=env_int("IMAP_PORT", 993),
            imap_user=os.getenv("IMAP_USER"),
            imap_password=os.getenv("IMAP_PASSWORD"),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
            imap_processed_folder=os.getenv("IMAP_PROCESSED_FOLDER", "Processed"),
        )

    def validate(self) -> None:
        if self.gmail_mode not in {"gmail_api", "imap"}:
            raise ValueError("GMAIL_MODE must be one of: gmail_api, imap.")
        if self.poll_interval_seconds <= 0:
            raise ValueError("GMAIL_POLL_INTERVAL_SECONDS must be > 0.")
        if self.default_user_id and not USER_ID_RE.match(self.default_user_id):
            raise ValueError(
                "DEFAULT_USER_ID may only contain letters, numbers, dot, underscore, dash."
            )
        if self.database_url and self.database_url.startswith(("postgres://", "postgresql://")):
            if psycopg is None:
                raise ValueError(
                    "DATABASE_URL uses postgres but psycopg is not installed. "
                    "Install dependency: pip install psycopg[binary]"
                )
        if self.gmail_api_rate_limit_per_sec <= 0:
            raise ValueError("GMAIL_API_RATE_LIMIT_PER_SEC must be > 0.")
        if self.gmail_api_max_retries < 0:
            raise ValueError("GMAIL_API_MAX_RETRIES must be >= 0.")
        if self.gmail_mode == "gmail_api":
            required = {
                "GOOGLE_CLIENT_ID": self.google_client_id,
                "GOOGLE_CLIENT_SECRET": self.google_client_secret,
                "GOOGLE_REFRESH_TOKEN": self.google_refresh_token,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "Missing required Gmail API environment variables: "
                    + ", ".join(missing)
                )
        if self.gmail_mode == "imap":
            required = {
                "IMAP_USER": self.imap_user,
                "IMAP_PASSWORD": self.imap_password,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "Missing required IMAP environment variables: " + ", ".join(missing)
                )


class IntakeRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approved_senders (
                user_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, sender_email)
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intake_mode TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                rfc822_message_id TEXT,
                user_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT,
                received_at TEXT,
                raw_path TEXT NOT NULL,
                scan_result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (intake_mode, provider_message_id)
            );

            CREATE TABLE IF NOT EXISTS quarantined_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intake_mode TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                rfc822_message_id TEXT,
                user_id TEXT,
                sender_email TEXT,
                subject TEXT,
                received_at TEXT,
                reason TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (intake_mode, provider_message_id)
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def has_provider_message(self, intake_mode: str, provider_message_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM cases WHERE intake_mode = ? AND provider_message_id = ?
            UNION
            SELECT 1 FROM quarantined_messages WHERE intake_mode = ? AND provider_message_id = ?
            LIMIT 1
            """,
            (intake_mode, provider_message_id, intake_mode, provider_message_id),
        ).fetchone()
        return row is not None

    def upsert_approved_sender(self, user_id: str, sender_email: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO approved_senders (user_id, sender_email)
            VALUES (?, ?)
            """,
            (user_id, normalize_email(sender_email)),
        )
        self.conn.commit()

    def load_approved_senders_file(self, path: Path) -> None:
        if not path.exists():
            LOGGER.warning(
                "Approved sender file not found at %s. Continuing with existing DB allowlist only.",
                path,
            )
            return
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("Approved sender file must be a JSON object.")
        for user_id, senders in data.items():
            if not isinstance(user_id, str):
                raise ValueError("Approved sender file keys must be strings (user_id).")
            if not isinstance(senders, list):
                raise ValueError(
                    f"Approved sender entry for user '{user_id}' must be a list."
                )
            for sender in senders:
                if not isinstance(sender, str):
                    raise ValueError(
                        f"Approved sender for user '{user_id}' must be a string."
                    )
                self.upsert_approved_sender(user_id=user_id, sender_email=sender)

    def is_sender_approved(self, user_id: str, sender_email: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM approved_senders
            WHERE user_id = ? AND sender_email = ?
            LIMIT 1
            """,
            (user_id, normalize_email(sender_email)),
        ).fetchone()
        return row is not None

    def store_case(
        self,
        intake_mode: str,
        provider_message_id: str,
        rfc822_message_id: Optional[str],
        user_id: str,
        sender_email: str,
        subject: str,
        received_at: str,
        raw_path: str,
        scan_result: Dict[str, Any],
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO cases (
                intake_mode, provider_message_id, rfc822_message_id, user_id,
                sender_email, subject, received_at, raw_path, scan_result_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intake_mode,
                provider_message_id,
                rfc822_message_id,
                user_id,
                normalize_email(sender_email),
                subject,
                received_at,
                raw_path,
                json.dumps(scan_result, sort_keys=True),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def store_quarantine(
        self,
        intake_mode: str,
        provider_message_id: str,
        rfc822_message_id: Optional[str],
        user_id: Optional[str],
        sender_email: Optional[str],
        subject: str,
        received_at: str,
        reason: str,
        raw_path: str,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO quarantined_messages (
                intake_mode, provider_message_id, rfc822_message_id, user_id,
                sender_email, subject, received_at, reason, raw_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intake_mode,
                provider_message_id,
                rfc822_message_id,
                user_id,
                normalize_email(sender_email) if sender_email else None,
                subject,
                received_at,
                reason,
                raw_path,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1


class PostgresIntakeRepository:
    def __init__(self, database_url: str) -> None:
        if psycopg is None:
            raise RuntimeError(
                "psycopg is required for postgres mode. "
                "Install with: pip install psycopg[binary]"
            )
        self.conn = psycopg.connect(database_url)
        self.conn.autocommit = False
        self._init_schema()

    def _init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS approved_senders (
                user_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, sender_email)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cases (
                id BIGSERIAL PRIMARY KEY,
                intake_mode TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                rfc822_message_id TEXT,
                user_id TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                subject TEXT,
                received_at TEXT,
                raw_path TEXT NOT NULL,
                scan_result_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (intake_mode, provider_message_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantined_messages (
                id BIGSERIAL PRIMARY KEY,
                intake_mode TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                rfc822_message_id TEXT,
                user_id TEXT,
                sender_email TEXT,
                subject TEXT,
                received_at TEXT,
                reason TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (intake_mode, provider_message_id)
            )
            """,
        ]
        try:
            with self.conn.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def _write(self, query: str, params: Tuple[Any, ...]) -> int:
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, params)
                rowcount = cursor.rowcount
            self.conn.commit()
            return rowcount
        except Exception:
            self.conn.rollback()
            raise

    def has_provider_message(self, intake_mode: str, provider_message_id: str) -> bool:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM cases WHERE intake_mode = %s AND provider_message_id = %s
                UNION
                SELECT 1 FROM quarantined_messages WHERE intake_mode = %s AND provider_message_id = %s
                LIMIT 1
                """,
                (intake_mode, provider_message_id, intake_mode, provider_message_id),
            )
            row = cursor.fetchone()
        return row is not None

    def upsert_approved_sender(self, user_id: str, sender_email: str) -> None:
        self._write(
            """
            INSERT INTO approved_senders (user_id, sender_email)
            VALUES (%s, %s)
            ON CONFLICT (user_id, sender_email) DO NOTHING
            """,
            (user_id, normalize_email(sender_email)),
        )

    def load_approved_senders_file(self, path: Path) -> None:
        if not path.exists():
            LOGGER.warning(
                "Approved sender file not found at %s. Continuing with existing DB allowlist only.",
                path,
            )
            return
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("Approved sender file must be a JSON object.")
        for user_id, senders in data.items():
            if not isinstance(user_id, str):
                raise ValueError("Approved sender file keys must be strings (user_id).")
            if not isinstance(senders, list):
                raise ValueError(
                    f"Approved sender entry for user '{user_id}' must be a list."
                )
            for sender in senders:
                if not isinstance(sender, str):
                    raise ValueError(
                        f"Approved sender for user '{user_id}' must be a string."
                    )
                self.upsert_approved_sender(user_id=user_id, sender_email=sender)

    def is_sender_approved(self, user_id: str, sender_email: str) -> bool:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM approved_senders
                WHERE user_id = %s AND sender_email = %s
                LIMIT 1
                """,
                (user_id, normalize_email(sender_email)),
            )
            row = cursor.fetchone()
        return row is not None

    def store_case(
        self,
        intake_mode: str,
        provider_message_id: str,
        rfc822_message_id: Optional[str],
        user_id: str,
        sender_email: str,
        subject: str,
        received_at: str,
        raw_path: str,
        scan_result: Dict[str, Any],
    ) -> bool:
        rowcount = self._write(
            """
            INSERT INTO cases (
                intake_mode, provider_message_id, rfc822_message_id, user_id,
                sender_email, subject, received_at, raw_path, scan_result_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (intake_mode, provider_message_id) DO NOTHING
            """,
            (
                intake_mode,
                provider_message_id,
                rfc822_message_id,
                user_id,
                normalize_email(sender_email),
                subject,
                received_at,
                raw_path,
                json.dumps(scan_result, sort_keys=True),
            ),
        )
        return rowcount == 1

    def store_quarantine(
        self,
        intake_mode: str,
        provider_message_id: str,
        rfc822_message_id: Optional[str],
        user_id: Optional[str],
        sender_email: Optional[str],
        subject: str,
        received_at: str,
        reason: str,
        raw_path: str,
    ) -> bool:
        rowcount = self._write(
            """
            INSERT INTO quarantined_messages (
                intake_mode, provider_message_id, rfc822_message_id, user_id,
                sender_email, subject, received_at, reason, raw_path
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (intake_mode, provider_message_id) DO NOTHING
            """,
            (
                intake_mode,
                provider_message_id,
                rfc822_message_id,
                user_id,
                normalize_email(sender_email) if sender_email else None,
                subject,
                received_at,
                reason,
                raw_path,
            ),
        )
        return rowcount == 1


def create_repository(settings: Settings) -> Any:
    database_url = settings.database_url
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        LOGGER.info("Using Postgres intake repository")
        return PostgresIntakeRepository(database_url=database_url)
    LOGGER.info("Using SQLite intake repository at %s", settings.db_path)
    return IntakeRepository(settings.db_path)


class EmailScanner:
    """
    Minimal scanner placeholder. Replace/extend with your existing detector pipeline.
    """

    risky_extensions = {
        ".bat",
        ".cmd",
        ".com",
        ".exe",
        ".jar",
        ".js",
        ".ps1",
        ".scr",
        ".vbs",
    }

    def scan(self, parsed_message: Any, raw_bytes: bytes) -> Dict[str, Any]:
        attachment_names: List[str] = []
        url_count = 0
        for part in parsed_message.walk():
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            if disposition == "attachment" or filename:
                attachment_names.append(filename or "unnamed_attachment")
            if part.get_content_maintype() == "text":
                try:
                    content = part.get_content()
                except Exception:
                    content = ""
                if isinstance(content, str):
                    url_count += len(URL_RE.findall(content))
        risky_attachments = [
            name
            for name in attachment_names
            if Path(name).suffix.lower() in self.risky_extensions
        ]
        return {
            "raw_size_bytes": len(raw_bytes),
            "attachment_count": len(attachment_names),
            "url_count": url_count,
            "risky_attachment_names": risky_attachments,
        }


def extract_user_id_from_headers(parsed_message: Any) -> Optional[str]:
    headers: List[str] = []
    for header_name in ("Delivered-To", "X-Original-To", "To"):
        headers.extend(parsed_message.get_all(header_name, failobj=[]))
    for _, address in getaddresses(headers):
        normalized = normalize_email(address)
        if "@" not in normalized:
            continue
        local_part = normalized.split("@", 1)[0]
        match = USER_PLUS_RE.match(local_part)
        if match:
            return match.group(1)
    return None


def extract_sender_email(parsed_message: Any) -> Optional[str]:
    headers = parsed_message.get_all("From", failobj=[])
    for _, address in getaddresses(headers):
        normalized = normalize_email(address)
        if "@" in normalized:
            return normalized
    return None


def extract_received_at(parsed_message: Any) -> str:
    raw_date = parsed_message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


class IntakeProcessor:
    def __init__(self, settings: Settings, repository: IntakeRepository) -> None:
        self.settings = settings
        self.repository = repository
        self.scanner = EmailScanner()
        self.settings.raw_dir.mkdir(parents=True, exist_ok=True)

    def _persist_raw_email(
        self, intake_mode: str, provider_message_id: str, raw_bytes: bytes
    ) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        file_name = (
            f"{timestamp}_{safe_file_token(intake_mode)}_"
            f"{safe_file_token(provider_message_id)}.eml"
        )
        destination = self.settings.raw_dir / file_name
        destination.write_bytes(raw_bytes)
        return str(destination)

    def process_raw_message(
        self, intake_mode: str, provider_message_id: str, raw_bytes: bytes
    ) -> str:
        if self.repository.has_provider_message(intake_mode, provider_message_id):
            LOGGER.debug(
                "Skipping already-ingested message %s/%s",
                intake_mode,
                provider_message_id,
            )
            return "duplicate"

        parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        raw_path = self._persist_raw_email(intake_mode, provider_message_id, raw_bytes)

        user_id = extract_user_id_from_headers(parsed)
        sender_email = extract_sender_email(parsed)
        subject = str(parsed.get("Subject", ""))
        received_at = extract_received_at(parsed)
        rfc822_message_id = str(parsed.get("Message-Id", "")).strip() or None

        if (not user_id) and self.settings.default_user_id:
            user_id = self.settings.default_user_id
            LOGGER.info(
                "No plus-address user id found for %s/%s; using DEFAULT_USER_ID=%s",
                intake_mode,
                provider_message_id,
                user_id,
            )

        if not user_id:
            self.repository.store_quarantine(
                intake_mode=intake_mode,
                provider_message_id=provider_message_id,
                rfc822_message_id=rfc822_message_id,
                user_id=None,
                sender_email=sender_email,
                subject=subject,
                received_at=received_at,
                reason="missing_plus_address_user_id",
                raw_path=raw_path,
            )
            LOGGER.warning(
                "Quarantined message %s/%s: could not parse user_id from Delivered-To/To",
                intake_mode,
                provider_message_id,
            )
            return "quarantined"

        if not sender_email or not self.repository.is_sender_approved(user_id, sender_email):
            self.repository.store_quarantine(
                intake_mode=intake_mode,
                provider_message_id=provider_message_id,
                rfc822_message_id=rfc822_message_id,
                user_id=user_id,
                sender_email=sender_email,
                subject=subject,
                received_at=received_at,
                reason="sender_not_approved",
                raw_path=raw_path,
            )
            LOGGER.warning(
                "Quarantined message %s/%s: sender not approved (user_id=%s sender=%s)",
                intake_mode,
                provider_message_id,
                user_id,
                sender_email,
            )
            return "quarantined"

        scan_result = self.scanner.scan(parsed, raw_bytes)
        inserted = self.repository.store_case(
            intake_mode=intake_mode,
            provider_message_id=provider_message_id,
            rfc822_message_id=rfc822_message_id,
            user_id=user_id,
            sender_email=sender_email,
            subject=subject,
            received_at=received_at,
            raw_path=raw_path,
            scan_result=scan_result,
        )
        if inserted:
            LOGGER.info(
                "Stored case for %s/%s (user_id=%s sender=%s)",
                intake_mode,
                provider_message_id,
                user_id,
                sender_email,
            )
        else:
            LOGGER.info(
                "Skipped duplicate case insert for %s/%s",
                intake_mode,
                provider_message_id,
            )
        return "processed"


class GmailApiClient:
    API_BASE = "https://gmail.googleapis.com/gmail/v1"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.access_token: Optional[str] = None
        self.access_token_expiry_epoch = 0.0
        self.last_request_epoch = 0.0
        self.processed_label_id: Optional[str] = None

    def _throttle(self) -> None:
        min_interval = 1.0 / self.settings.gmail_api_rate_limit_per_sec
        now = time.time()
        elapsed = now - self.last_request_epoch
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_request_epoch = time.time()

    def _refresh_access_token(self) -> None:
        payload = urllib.parse.urlencode(
            {
                "client_id": self.settings.google_client_id or "",
                "client_secret": self.settings.google_client_secret or "",
                "refresh_token": self.settings.google_refresh_token or "",
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.TOKEN_ENDPOINT,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        token = body.get("access_token")
        expires_in = int(body.get("expires_in", 3600))
        if not token:
            raise RuntimeError("Failed to refresh access token; missing access_token.")
        self.access_token = token
        self.access_token_expiry_epoch = time.time() + max(60, expires_in - 60)

    def _ensure_access_token(self) -> str:
        if (not self.access_token) or (time.time() >= self.access_token_expiry_epoch):
            self._refresh_access_token()
        assert self.access_token is not None
        return self.access_token

    def _backoff_sleep_seconds(
        self, attempt: int, retry_after_header: Optional[str]
    ) -> float:
        if retry_after_header:
            try:
                return max(0.0, float(retry_after_header))
            except ValueError:
                pass
        base = self.settings.gmail_api_backoff_base_seconds * (2**attempt)
        capped = min(self.settings.gmail_api_backoff_max_seconds, base)
        jitter = random.uniform(0.0, capped * 0.25)
        return capped + jitter

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Gmail API path must start with '/'.")

        attempt = 0
        max_attempts = self.settings.gmail_api_max_retries + 1

        while attempt < max_attempts:
            token = self._ensure_access_token()
            query_string = urllib.parse.urlencode(query or {})
            url = f"{self.API_BASE}{path}"
            if query_string:
                url = f"{url}?{query_string}"
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                url=url,
                data=payload,
                headers=headers,
                method=method.upper(),
            )

            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as error:
                status = error.code
                response_body = error.read().decode("utf-8", errors="ignore")
                if status == 401:
                    self.access_token = None
                    if attempt + 1 < max_attempts:
                        attempt += 1
                        continue
                if status in RETRYABLE_HTTP_CODES and attempt + 1 < max_attempts:
                    sleep_seconds = self._backoff_sleep_seconds(
                        attempt=attempt,
                        retry_after_header=error.headers.get("Retry-After")
                        if error.headers
                        else None,
                    )
                    LOGGER.warning(
                        "Gmail API request failed (%s). Retrying in %.2fs. Body: %s",
                        status,
                        sleep_seconds,
                        response_body,
                    )
                    time.sleep(sleep_seconds)
                    attempt += 1
                    continue
                raise RuntimeError(
                    f"Gmail API request failed with status {status}: {response_body}"
                ) from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt + 1 < max_attempts:
                    sleep_seconds = self._backoff_sleep_seconds(
                        attempt=attempt, retry_after_header=None
                    )
                    LOGGER.warning(
                        "Gmail API request network error (%s). Retrying in %.2fs.",
                        error,
                        sleep_seconds,
                    )
                    time.sleep(sleep_seconds)
                    attempt += 1
                    continue
                raise

        raise RuntimeError("Gmail API request exceeded retry limit.")

    def list_unread_message_ids(self, query: str) -> List[str]:
        user = urllib.parse.quote(self.settings.gmail_user, safe="")
        path = f"/users/{user}/messages"
        message_ids: List[str] = []
        next_page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"q": query, "maxResults": 100}
            if next_page_token:
                params["pageToken"] = next_page_token
            response = self._request("GET", path=path, query=params)
            message_ids.extend(
                [
                    msg["id"]
                    for msg in response.get("messages", [])
                    if isinstance(msg, dict) and "id" in msg
                ]
            )
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
        return message_ids

    def get_raw_message_bytes(self, gmail_message_id: str) -> bytes:
        user = urllib.parse.quote(self.settings.gmail_user, safe="")
        message_id = urllib.parse.quote(gmail_message_id, safe="")
        path = f"/users/{user}/messages/{message_id}"
        response = self._request("GET", path=path, query={"format": "raw"})
        raw = response.get("raw")
        if not raw:
            raise RuntimeError(f"Gmail message {gmail_message_id} did not include raw body.")
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding)

    def ensure_processed_label_id(self) -> str:
        if self.processed_label_id:
            return self.processed_label_id
        user = urllib.parse.quote(self.settings.gmail_user, safe="")
        labels_path = f"/users/{user}/labels"
        labels = self._request("GET", path=labels_path).get("labels", [])
        for label in labels:
            if not isinstance(label, dict):
                continue
            if str(label.get("name", "")).lower() == self.settings.gmail_label_processed.lower():
                self.processed_label_id = str(label["id"])
                return self.processed_label_id
        create_response = self._request(
            "POST",
            path=labels_path,
            body={
                "name": self.settings.gmail_label_processed,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        label_id = create_response.get("id")
        if not label_id:
            raise RuntimeError("Failed to create/get Gmail processed label id.")
        self.processed_label_id = str(label_id)
        return self.processed_label_id

    def mark_message_processed(self, gmail_message_id: str) -> None:
        user = urllib.parse.quote(self.settings.gmail_user, safe="")
        message_id = urllib.parse.quote(gmail_message_id, safe="")
        label_id = self.ensure_processed_label_id()
        self._request(
            "POST",
            path=f"/users/{user}/messages/{message_id}/modify",
            body={"removeLabelIds": ["UNREAD"], "addLabelIds": [label_id]},
        )


class GmailApiIngestor:
    def __init__(self, settings: Settings, processor: IntakeProcessor) -> None:
        self.settings = settings
        self.processor = processor
        self.client = GmailApiClient(settings=settings)

    def run_once(self) -> int:
        message_ids = self.client.list_unread_message_ids(self.settings.gmail_query)
        if not message_ids:
            LOGGER.debug("No unread Gmail API messages found.")
            return 0

        handled = 0
        for message_id in message_ids:
            try:
                raw_bytes = self.client.get_raw_message_bytes(message_id)
                status = self.processor.process_raw_message(
                    intake_mode="gmail_api",
                    provider_message_id=message_id,
                    raw_bytes=raw_bytes,
                )
                if status in {"processed", "quarantined", "duplicate"}:
                    self.client.mark_message_processed(message_id)
                handled += 1
            except Exception:
                LOGGER.exception("Failed Gmail API ingest for message_id=%s", message_id)
        return handled


class ImapIngestor:
    UID_RE = re.compile(r"UID (\d+)")

    def __init__(self, settings: Settings, processor: IntakeProcessor) -> None:
        self.settings = settings
        self.processor = processor

    def _ensure_folder_exists(self, conn: imaplib.IMAP4_SSL, folder_name: str) -> None:
        status, _ = conn.select(f'"{folder_name}"', readonly=True)
        if status == "OK":
            return
        conn.create(f'"{folder_name}"')

    def _extract_uid_and_bytes(
        self, fetch_payload: Iterable[Any], fallback_id: str
    ) -> Tuple[str, bytes]:
        uid = fallback_id
        raw_bytes: Optional[bytes] = None
        for part in fetch_payload:
            if isinstance(part, tuple) and len(part) == 2:
                meta = (
                    part[0].decode("utf-8", errors="ignore")
                    if isinstance(part[0], (bytes, bytearray))
                    else str(part[0])
                )
                match = self.UID_RE.search(meta)
                if match:
                    uid = match.group(1)
                payload = part[1]
                if isinstance(payload, bytes):
                    raw_bytes = payload
        if raw_bytes is None:
            raise RuntimeError(f"Could not read RFC822 bytes for message {fallback_id}.")
        return uid, raw_bytes

    def run_once(self) -> int:
        conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
        conn.login(self.settings.imap_user or "", self.settings.imap_password or "")
        handled = 0
        try:
            status, _ = conn.select(f'"{self.settings.imap_folder}"', readonly=False)
            if status != "OK":
                raise RuntimeError(
                    f"Could not select IMAP folder {self.settings.imap_folder}."
                )
            self._ensure_folder_exists(conn, self.settings.imap_processed_folder)
            conn.select(f'"{self.settings.imap_folder}"', readonly=False)

            status, search_data = conn.search(None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("IMAP search UNSEEN failed.")
            message_nums = []
            if search_data and isinstance(search_data[0], (bytes, bytearray)):
                message_nums = [m for m in search_data[0].split() if m]

            for msg_num in message_nums:
                msg_num_text = msg_num.decode("utf-8")
                try:
                    fetch_status, fetch_data = conn.fetch(msg_num, "(UID RFC822)")
                    if fetch_status != "OK":
                        raise RuntimeError(f"IMAP fetch failed for msg {msg_num_text}.")
                    uid, raw_bytes = self._extract_uid_and_bytes(fetch_data, msg_num_text)
                    status = self.processor.process_raw_message(
                        intake_mode="imap",
                        provider_message_id=uid,
                        raw_bytes=raw_bytes,
                    )
                    if status in {"processed", "quarantined", "duplicate"}:
                        copy_status, _ = conn.copy(
                            msg_num, f'"{self.settings.imap_processed_folder}"'
                        )
                        if copy_status != "OK":
                            raise RuntimeError(
                                f"Failed to copy message {msg_num_text} to "
                                f"{self.settings.imap_processed_folder}."
                            )
                        conn.store(msg_num, "+FLAGS", "(\\Seen \\Deleted)")
                    handled += 1
                except Exception:
                    LOGGER.exception("Failed IMAP ingest for message_num=%s", msg_num_text)
            conn.expunge()
            return handled
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()


def run_poll_loop(worker: Any, poll_interval_seconds: int, once: bool = False) -> None:
    while True:
        start = time.time()
        try:
            handled = worker.run_once()
            LOGGER.info("Poll complete. Handled %s message(s).", handled)
        except Exception:
            LOGGER.exception("Poll cycle failed")
        if once:
            break
        elapsed = time.time() - start
        sleep_for = max(0.0, poll_interval_seconds - elapsed)
        time.sleep(sleep_for)


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dual-mode Gmail intake worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run only one poll cycle and exit.",
    )
    return parser.parse_args()


def main() -> None:
    load_env_file(Path(".env"))
    configure_logging()
    args = parse_args()
    settings = Settings.from_env()
    settings.validate()

    repository = create_repository(settings)
    try:
        if settings.approved_senders_file:
            if settings.approved_senders_file.exists():
                repository.load_approved_senders_file(settings.approved_senders_file)
                LOGGER.info(
                    "Loaded approved senders from %s",
                    settings.approved_senders_file,
                )
            else:
                repository.load_approved_senders_file(settings.approved_senders_file)

        processor = IntakeProcessor(settings=settings, repository=repository)
        if settings.gmail_mode == "gmail_api":
            worker = GmailApiIngestor(settings=settings, processor=processor)
        else:
            worker = ImapIngestor(settings=settings, processor=processor)
        run_poll_loop(
            worker=worker,
            poll_interval_seconds=settings.poll_interval_seconds,
            once=args.once,
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
