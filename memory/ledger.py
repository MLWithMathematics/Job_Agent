from __future__ import annotations

import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import settings


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            job_title         TEXT,
            company           TEXT,
            platform          TEXT,
            apply_url         TEXT UNIQUE,
            apply_type        TEXT DEFAULT 'easy_apply',
            match_score       INTEGER,
            status            TEXT DEFAULT 'pending',
            resume_path       TEXT,
            applied_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            outreach_sent     BOOLEAN DEFAULT 0,
            notes             TEXT
        );

        CREATE TABLE IF NOT EXISTS outreach_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id    INTEGER,
            recruiter_name    TEXT,
            company           TEXT,
            message_text      TEXT,
            sent_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (application_id) REFERENCES applications(id)
        );

        CREATE TABLE IF NOT EXISTS refresh_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            platform          TEXT DEFAULT 'naukri',
            refreshed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success           BOOLEAN,
            notes             TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # ── Migration: add apply_type column to existing databases ────────────
    conn = _get_conn()
    try:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN apply_type TEXT DEFAULT 'easy_apply'"
        )
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.close()


def upsert_application(
    job_title: str,
    company: str,
    platform: str,
    apply_url: str,
    apply_type: str = "easy_apply",
    match_score: int = 0,
    status: str = "pending",
    resume_path: str = "",
    outreach_sent: bool = False,
    notes: str = "",
) -> int:
    """Insert or update an application record. Returns row id."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM applications WHERE apply_url = ?", (apply_url,))
    row = cur.fetchone()
    if row:
        cur.execute(
            """UPDATE applications
               SET job_title=?, company=?, platform=?, apply_type=?, match_score=?,
                   status=?, resume_path=?, outreach_sent=?, notes=?,
                   applied_at=CURRENT_TIMESTAMP
               WHERE apply_url=?""",
            (
                job_title,
                company,
                platform,
                apply_type,
                match_score,
                status,
                resume_path,
                int(outreach_sent),
                notes,
                apply_url,
            ),
        )
        row_id = row["id"]
    else:
        cur.execute(
            """INSERT INTO applications
               (job_title, company, platform, apply_url, apply_type, match_score,
                status, resume_path, outreach_sent, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_title,
                company,
                platform,
                apply_url,
                apply_type,
                match_score,
                status,
                resume_path,
                int(outreach_sent),
                notes,
            ),
        )
        row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_status(apply_url: str, status: str, notes: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE applications SET status=?, notes=? WHERE apply_url=?",
        (status, notes, apply_url),
    )
    conn.commit()
    conn.close()


def mark_outreach_sent(apply_url: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE applications SET outreach_sent=1 WHERE apply_url=?",
        (apply_url,),
    )
    conn.commit()
    conn.close()


def log_outreach(
    application_id: int,
    recruiter_name: str,
    company: str,
    message_text: str,
) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO outreach_log (application_id, recruiter_name, company, message_text)
           VALUES (?, ?, ?, ?)""",
        (application_id, recruiter_name, company, message_text),
    )
    conn.commit()
    conn.close()


def log_refresh(success: bool, notes: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO refresh_log (success, notes) VALUES (?, ?)",
        (int(success), notes),
    )
    conn.commit()
    conn.close()


def is_already_applied(apply_url: str) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM applications WHERE apply_url=?",
        (apply_url,),
    )
    row = cur.fetchone()
    conn.close()
    if row and row["status"] in ("applied", "skipped"):
        return True
    return False


def get_all_applications() -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM applications ORDER BY applied_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def update_status_by_id(app_id: int, status: str) -> None:
    conn = _get_conn()
    conn.execute("UPDATE applications SET status=? WHERE id=?", (status, app_id))
    conn.commit()
    conn.close()


# Auto-init on import
init_db()
