"""Deterministic CSV replay using an application-owned simulation clock."""

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import DB_PATH, SCENARIO_DIR
from .audit import log
from .db import get_runtime, set_runtime, transaction
from .reconciliation import process_transaction, reconcile_pending
from .risk_engine import get_settings_from_conn, monitor_feed
from .seed import reset_database


def load_scenario(name: str, db_path=DB_PATH) -> None:
    if name not in {"normal", "discrepancy", "feed_failure", "timing_difference"}:
        raise ValueError(f"Unknown scenario: {name}")
    reset_database(db_path, name)


def scenario_events(name: str) -> list[dict]:
    frame = pd.read_csv(SCENARIO_DIR / f"{name}.csv", dtype=str, keep_default_na=False)
    return frame.to_dict(orient="records")


def start_stream(db_path=DB_PATH, actor="DEMO USER") -> None:
    with transaction(db_path) as conn:
        now = get_runtime_from_conn(conn, "simulation_time")
        set_runtime("stream_state", "RUNNING", conn)
        log(conn, "STREAM_STARTED", "Scenario replay started.", now, actor=actor)


def set_stream_state(state: str, db_path=DB_PATH, actor="DEMO USER") -> None:
    with transaction(db_path) as conn:
        now = get_runtime_from_conn(conn, "simulation_time")
        set_runtime("stream_state", state, conn)
        log(conn, f"STREAM_{state}", f"Scenario replay changed to {state}.", now, actor=actor)


def get_runtime_from_conn(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM runtime WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _promote_scheduled_bank_records(conn, now: str) -> list[str]:
    due_records = conn.execute(
        "SELECT * FROM bank_transactions WHERE status = 'SCHEDULED' AND posted_at <= ?",
        (now,),
    ).fetchall()
    posted_ids = []
    for bank_txn in due_records:
        program = conn.execute(
            "SELECT status, restriction_reason FROM programs WHERE program_id=?",
            (bank_txn["program_id"],),
        ).fetchone()
        if program and program["status"] == "RESTRICTED":
            conn.execute(
                "UPDATE bank_transactions SET status='REJECTED_BY_RESTRICTION' WHERE bank_txn_id=?",
                (bank_txn["bank_txn_id"],),
            )
            log(
                conn,
                "BANK_TRANSACTION_REJECTED",
                f"{bank_txn['bank_txn_id']} was rejected before posting because the program was restricted. FBO balance unchanged.",
                now,
                bank_txn["program_id"],
            )
            continue
        balance_change = float(bank_txn["amount"]) if bank_txn["direction"] == "CREDIT" else -float(bank_txn["amount"])
        conn.execute(
            """UPDATE bank_accounts
               SET ledger_balance = ledger_balance + ?,
                   available_balance = CASE
                       WHEN available_balance IS NULL THEN NULL
                       ELSE available_balance + ?
                   END,
                   updated_at = ?
               WHERE account_id = ?""",
            (balance_change, balance_change, now, bank_txn["account_id"]),
        )
        conn.execute(
            "UPDATE bank_transactions SET status = 'POSTED' WHERE bank_txn_id = ? AND status = 'SCHEDULED'",
            (bank_txn["bank_txn_id"],),
        )
        log(
            conn,
            "BANK_TRANSACTION_POSTED",
            f"{bank_txn['bank_txn_id']} posted; {bank_txn['direction']} ${float(bank_txn['amount']):,.2f} applied to {bank_txn['account_id']}.",
            now,
            bank_txn["program_id"],
        )
        posted_ids.append(bank_txn["bank_txn_id"])
    return posted_ids


def _next_control_checkpoint(conn, current_time: str, scenario: str) -> str | None:
    """Return the next business-time deadline without iterating through fake time ticks."""
    current = datetime.fromisoformat(current_time)
    candidates: list[datetime] = []

    for pending in conn.execute(
        "SELECT match_deadline_at FROM processed_events WHERE result_status='PENDING_MATCH'"
    ).fetchall():
        candidates.append(datetime.fromisoformat(pending["match_deadline_at"]))

    for scheduled in conn.execute(
        "SELECT posted_at FROM bank_transactions WHERE status = 'SCHEDULED'"
    ).fetchall():
        candidates.append(datetime.fromisoformat(scheduled["posted_at"]))

    settings = get_settings_from_conn(conn)
    grace = timedelta(seconds=int(settings["match_grace_seconds"]))
    for bank in conn.execute(
        """SELECT bank.posted_at FROM bank_transactions bank
           WHERE bank.status='POSTED'
             AND NOT EXISTS (
                 SELECT 1 FROM processed_events event
                 WHERE event.event_type='TRANSACTION'
                   AND event.middleware_txn_id=bank.middleware_reference
             )
             AND NOT EXISTS (
                 SELECT 1 FROM exceptions exc
                 WHERE exc.exception_type='MISSING_INTERMEDIARY_EVENT'
                   AND exc.bank_txn_id=bank.bank_txn_id
             )"""
    ).fetchall():
        candidates.append(datetime.fromisoformat(bank["posted_at"]) + grace)

    if scenario == "feed_failure":
        program = conn.execute(
            "SELECT last_heartbeat_at FROM programs WHERE program_id = 'PROG-001'"
        ).fetchone()
        if program and program["last_heartbeat_at"]:
            heartbeat = datetime.fromisoformat(program["last_heartbeat_at"])
            candidates.extend(
                [
                    heartbeat + timedelta(seconds=int(settings["heartbeat_warning_seconds"])),
                    heartbeat + timedelta(seconds=int(settings["heartbeat_critical_seconds"])),
                ]
            )

    future = [candidate for candidate in candidates if candidate > current]
    return min(future).isoformat(timespec="seconds") if future else None


def process_next(db_path=DB_PATH) -> dict:
    with transaction(db_path) as conn:
        scenario = get_runtime_from_conn(conn, "scenario", "normal")
        position = int(get_runtime_from_conn(conn, "playback_position", "0"))
        events = scenario_events(scenario)
        current_time = get_runtime_from_conn(conn, "simulation_time", "2026-10-21T09:59:00")

        if position < len(events):
            event = events[position]
            next_event_time = event["event_timestamp"]
            checkpoint = _next_control_checkpoint(conn, current_time, scenario)
            if checkpoint and checkpoint < next_event_time:
                now = checkpoint
                set_runtime("simulation_time", now, conn)
                idle_ticks = int(get_runtime_from_conn(conn, "idle_ticks", "0")) + 1
                set_runtime("idle_ticks", idle_ticks, conn)
                posted_ids = _promote_scheduled_bank_records(conn, now)
                reconcile_pending(conn, now)
                health = monitor_feed(conn, now)
                if posted_ids:
                    return {
                        "done": False,
                        "event": None,
                        "result": "BANK_TRANSACTION_POSTED",
                        "bank_transactions": posted_ids,
                        "time": now,
                    }
                log(conn, "SCENARIO_TIME_ADVANCED", f"Scenario clock advanced to the next control deadline at {now}.", now)
                return {"done": False, "event": None, "result": "CONTROL_DEADLINE_REACHED", "time": now, "feed_health": health}

            now = event["event_timestamp"]
            set_runtime("simulation_time", now, conn)
            set_runtime("playback_position", position + 1, conn)
            _promote_scheduled_bank_records(conn, now)
            reconcile_pending(conn, now)

            if event["event_type"] == "HEARTBEAT":
                conn.execute("UPDATE programs SET last_heartbeat_at = ?, updated_at = ? WHERE program_id = ?", (now, now, event["program_id"]))
                conn.execute(
                    """INSERT INTO processed_events(event_id,event_type,event_timestamp,program_id,result_status,processed_at)
                       VALUES (?, ?, ?, ?, 'HEARTBEAT_RECEIVED', ?)""",
                    (event["event_id"], event["event_type"], event["event_timestamp"], event["program_id"], now),
                )
                log(conn, "HEARTBEAT_RECEIVED", f"Valid heartbeat {event['event_id']} received.", now, event["program_id"])
                result = "HEARTBEAT_RECEIVED"
            else:
                result = process_transaction(conn, event, now)

            monitor_feed(conn, now, event["program_id"])
            reconcile_pending(conn, now)
            return {"done": False, "event": event["event_id"], "result": result, "time": now}

        # Jump to the next meaningful business deadline; never loop over artificial seconds.
        feed_demo = scenario == "feed_failure"
        next_checkpoint = _next_control_checkpoint(conn, current_time, scenario)
        if next_checkpoint:
            now = next_checkpoint
            set_runtime("simulation_time", now, conn)
            idle_ticks = int(get_runtime_from_conn(conn, "idle_ticks", "0")) + 1
            set_runtime("idle_ticks", idle_ticks, conn)
            posted_ids = _promote_scheduled_bank_records(conn, now)
            reconcile_pending(conn, now)
            health = monitor_feed(conn, now)
            if posted_ids:
                return {
                    "done": False,
                    "event": None,
                    "result": "BANK_TRANSACTION_POSTED",
                    "bank_transactions": posted_ids,
                    "time": now,
                }
            log(conn, "SCENARIO_TIME_ADVANCED", f"Scenario clock advanced to the next control deadline at {now}.", now)
            if feed_demo and health == "CRITICAL":
                set_runtime("stream_state", "COMPLETE", conn)
                return {"done": True, "event": None, "result": "FEED_FAILURE", "time": now}
            return {"done": False, "event": None, "result": "CONTROL_DEADLINE_REACHED", "time": now}

        set_runtime("stream_state", "COMPLETE", conn)
        return {"done": True, "event": None, "result": "COMPLETE", "time": current_time}
