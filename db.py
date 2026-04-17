import sqlite3
import json
from pathlib import Path
from datetime import date, datetime

DB_PATH = Path.home() / ".boingo" / "boingo.db"


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                period_date   DATE    NOT NULL,
                account_id    TEXT    NOT NULL,
                qb_account    TEXT    NOT NULL,
                balance       REAL,
                basis         REAL,
                gain_loss     REAL GENERATED ALWAYS AS (
                                  CASE WHEN basis IS NOT NULL THEN balance - basis ELSE NULL END
                              ) STORED,
                source        TEXT    NOT NULL DEFAULT 'csv_upload',
                uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(period_date, account_id)
            );

            CREATE TABLE IF NOT EXISTS je_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                period_date  DATE    NOT NULL,
                qbo_je_id    TEXT,
                je_lines     TEXT,
                status       TEXT    NOT NULL DEFAULT 'draft',
                posted_at    TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_period
                ON balance_snapshots(period_date);
            CREATE INDEX IF NOT EXISTS idx_snapshots_account
                ON balance_snapshots(account_id);

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)


def get_config(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(key: str, value: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))


def upsert_snapshot(period_date: date, account_id: str, qb_account: str,
                    balance: float, basis: float | None = None,
                    source: str = "csv_upload"):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO balance_snapshots (period_date, account_id, qb_account, balance, basis, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(period_date, account_id) DO UPDATE SET
                balance     = excluded.balance,
                basis       = excluded.basis,
                source      = excluded.source,
                uploaded_at = CURRENT_TIMESTAMP
        """, (period_date.isoformat(), account_id, qb_account, balance, basis, source))


def get_snapshots_for_period(period_date: date) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM balance_snapshots WHERE period_date = ?
            ORDER BY qb_account
        """, (period_date.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def get_prior_snapshot(account_id: str, before_date: date) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM balance_snapshots
            WHERE account_id = ? AND period_date < ?
            ORDER BY period_date DESC LIMIT 1
        """, (account_id, before_date.isoformat())).fetchone()
    return dict(row) if row else None


def get_all_periods() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT period_date FROM balance_snapshots
            ORDER BY period_date DESC
        """).fetchall()
    return [r["period_date"] for r in rows]


def get_history(account_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT period_date, balance, basis, gain_loss
            FROM balance_snapshots
            WHERE account_id = ?
            ORDER BY period_date
        """, (account_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_history() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM balance_snapshots ORDER BY period_date, qb_account
        """).fetchall()
    return [dict(r) for r in rows]


def log_je(period_date: date, je_lines: list, status: str = "draft",
           qbo_je_id: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO je_log (period_date, qbo_je_id, je_lines, status, posted_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            period_date.isoformat(),
            qbo_je_id,
            json.dumps(je_lines),
            status,
            datetime.utcnow().isoformat() if status == "posted" else None,
        ))
        return cur.lastrowid


def get_je_log(period_date: date) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM je_log WHERE period_date = ? ORDER BY created_at DESC
        """, (period_date.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


init_db()
