"""Deterministic synthetic seed data and reset routines."""

from pathlib import Path

import pandas as pd

from . import BATCH_DIR, DATA_DIR, DB_PATH, SCENARIO_DIR
from .audit import log
from .config import DEFAULT_SETTINGS
from .db import init_db, set_runtime, transaction

BASE_TIME = "2026-10-21T09:59:00"


def reset_database(db_path: str | Path = DB_PATH, scenario: str = "normal") -> None:
    path = Path(db_path)
    path.unlink(missing_ok=True)
    init_db(path)
    with transaction(path) as conn:

        conn.execute(
            """INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("PROG-001", "Northstar Wallet Program", "BridgeLayer (Synthetic)", "FBO-001", "USD", "ACTIVE", 1, None, None, BASE_TIME, BASE_TIME),
        )
        conn.execute(
            """INSERT INTO bank_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("FBO-001", "PROG-001", "Northstar Customer Funds FBO", "FBO", "USD", 10000.0, 10000.0, "ACTIVE", BASE_TIME),
        )
        bank_fixture = pd.read_csv(DATA_DIR / "bank_transactions.csv", dtype=str, keep_default_na=False)
        bank_transactions = [
            (
                item["bank_txn_id"], item["program_id"], item["account_id"],
                item["middleware_reference"], float(item["amount"]), item["direction"],
                item["status"], item["posted_at"],
            )
            for item in bank_fixture.loc[bank_fixture["scenario"] == scenario].to_dict(orient="records")
        ]
        conn.executemany("INSERT INTO bank_transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", bank_transactions)

        opening_balances = [("PROG-001", f"U00{i}", balance, None, BASE_TIME) for i, balance in enumerate([4000.0, 3000.0, 3000.0, 0.0, 0.0], start=1)]
        conn.executemany("INSERT INTO expected_customer_balances VALUES (?, ?, ?, ?, ?)", opening_balances)

        for key, (value, data_type, description) in DEFAULT_SETTINGS.items():
            conn.execute("INSERT INTO settings VALUES (?, ?, ?, ?)", (key, str(value), data_type, description))

        scenario_path = SCENARIO_DIR / f"{scenario}.csv"
        batch_path = BATCH_DIR / f"{scenario}.csv"
        set_runtime("scenario", scenario, conn)
        set_runtime("scenario_path", scenario_path, conn)
        set_runtime("batch_path", batch_path, conn)
        set_runtime("playback_position", 0, conn)
        set_runtime("simulation_time", BASE_TIME, conn)
        set_runtime("stream_state", "READY", conn)
        set_runtime("idle_ticks", 0, conn)
        log(conn, "SCENARIO_LOADED", f"Loaded deterministic scenario: {scenario}", BASE_TIME, actor="DEMO USER")


def ensure_database(db_path: str | Path = DB_PATH) -> None:
    path = Path(db_path)
    if not path.exists():
        reset_database(path)
        return

    init_db(path)
    with transaction(path) as conn:
        for key, (value, data_type, description) in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, data_type, description) VALUES (?, ?, ?, ?)",
                (key, str(value), data_type, description),
            )


if __name__ == "__main__":
    reset_database()
    print(f"LedgerWatch database reset at {DB_PATH}")
