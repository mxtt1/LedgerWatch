"""SQLite storage helpers."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import DB_PATH


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS programs (
    program_id TEXT PRIMARY KEY,
    program_name TEXT NOT NULL,
    middleware_name TEXT NOT NULL,
    fbo_account_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    auto_restrict_enabled INTEGER NOT NULL DEFAULT 1,
    restriction_reason TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_accounts (
    account_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    ledger_balance REAL NOT NULL,
    available_balance REAL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    bank_txn_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    middleware_reference TEXT NOT NULL,
    amount REAL NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    middleware_txn_id TEXT,
    bank_txn_id TEXT,
    amount_difference REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    owner TEXT,
    created_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id TEXT PRIMARY KEY,
    program_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    data_type TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expected_customer_balances (
    program_id TEXT NOT NULL,
    end_user_id TEXT NOT NULL,
    expected_balance REAL NOT NULL,
    last_matched_event_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (program_id, end_user_id)
);

CREATE TABLE IF NOT EXISTS reconciliation_batches (
    batch_id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    program_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    received_at TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    reported_total REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    reconciled_at TEXT,
    bank_balance REAL,
    expected_total REAL,
    aggregate_difference REAL,
    customer_mismatch_count INTEGER,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS batch_customer_balances (
    batch_id TEXT NOT NULL,
    program_id TEXT NOT NULL,
    end_user_id TEXT NOT NULL,
    reported_balance REAL NOT NULL,
    PRIMARY KEY (batch_id, end_user_id)
);

CREATE TABLE IF NOT EXISTS customer_reconciliation_results (
    batch_id TEXT NOT NULL,
    program_id TEXT NOT NULL,
    end_user_id TEXT NOT NULL,
    expected_balance REAL NOT NULL,
    reported_balance REAL NOT NULL,
    difference REAL NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (batch_id, end_user_id)
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    program_id TEXT NOT NULL,
    middleware_txn_id TEXT,
    end_user_id TEXT,
    bank_account_id TEXT,
    amount REAL,
    direction TEXT,
    customer_balance REAL,
    result_status TEXT NOT NULL,
    match_deadline_at TEXT,
    bank_txn_id TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(db_path: str | Path = DB_PATH):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path = DB_PATH) -> None:
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA)


def rows(query: str, params=(), db_path: str | Path = DB_PATH) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def row(query: str, params=(), db_path: str | Path = DB_PATH) -> dict | None:
    result = rows(query, params, db_path)
    return result[0] if result else None


def setting_value(raw: str, data_type: str):
    if data_type == "int":
        return int(float(raw))
    if data_type == "float":
        return float(raw)
    if data_type == "bool":
        return raw.lower() in {"1", "true", "yes", "on"}
    return raw


def get_settings(db_path: str | Path = DB_PATH) -> dict:
    return {item["key"]: setting_value(item["value"], item["data_type"]) for item in rows("SELECT * FROM settings", db_path=db_path)}


def get_runtime(key: str, default=None, db_path: str | Path = DB_PATH):
    item = row("SELECT value FROM runtime WHERE key = ?", (key,), db_path)
    return item["value"] if item else default


def set_runtime(key: str, value, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO runtime(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
