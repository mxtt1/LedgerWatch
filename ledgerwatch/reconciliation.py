"""Transaction matching, bank-calculated balances, and daily batch reconciliation."""

from datetime import datetime, timedelta

import pandas as pd

from . import BATCH_DIR
from .audit import log
from .risk_engine import create_exception, evaluate_risk, get_settings_from_conn


REQUIRED_TRANSACTION_FIELDS = ("middleware_txn_id", "end_user_id", "bank_account_id", "amount", "direction")
REQUIRED_BATCH_FIELDS = ("batch_id", "as_of", "program_id", "end_user_id", "currency", "ending_balance")


def _insert_processed(
    conn, event: dict, result_status: str, now: str,
    bank_txn_id=None, match_deadline_at=None,
) -> None:
    conn.execute(
        """INSERT INTO processed_events(event_id, event_type, event_timestamp, program_id, middleware_txn_id,
           end_user_id, bank_account_id, amount, direction, customer_balance, result_status,
           match_deadline_at, bank_txn_id, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.get("event_id"), event.get("event_type"), event.get("event_timestamp"), event.get("program_id"),
            event.get("middleware_txn_id") or None, event.get("end_user_id") or None,
            event.get("bank_account_id") or None, float(event["amount"]) if event.get("amount") not in (None, "") else None,
            event.get("direction") or None, float(event["customer_balance"]) if event.get("customer_balance") not in (None, "") else None,
            result_status, match_deadline_at, bank_txn_id, now,
        ),
    )


def _mismatch(bank, event) -> tuple[str | None, float]:
    if bank["program_id"] != event["program_id"] or bank["account_id"] != event["bank_account_id"]:
        return "ACCOUNT_MISMATCH", float(event["amount"])
    if abs(float(bank["amount"]) - float(event["amount"])) > 0.001:
        return "AMOUNT_MISMATCH", abs(float(bank["amount"]) - float(event["amount"]))
    if bank["direction"] != event["direction"]:
        return "DIRECTION_MISMATCH", float(event["amount"])
    return None, 0.0


def _apply_expected_balance(conn, event, now: str) -> None:
    """Roll forward the bank-calculated customer balance after an exact bank match."""
    amount = float(event["amount"])
    change = amount if event["direction"] == "CREDIT" else -amount
    existing = conn.execute(
        "SELECT expected_balance FROM expected_customer_balances WHERE program_id=? AND end_user_id=?",
        (event["program_id"], event["end_user_id"]),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE expected_customer_balances
               SET expected_balance=expected_balance+?, last_matched_event_id=?, updated_at=?
               WHERE program_id=? AND end_user_id=?""",
            (change, event["event_id"], now, event["program_id"], event["end_user_id"]),
        )
    else:
        conn.execute(
            "INSERT INTO expected_customer_balances VALUES (?, ?, ?, ?, ?)",
            (event["program_id"], event["end_user_id"], change, event["event_id"], now),
        )


def _validation_errors(event: dict) -> list[str]:
    missing = [field for field in REQUIRED_TRANSACTION_FIELDS if event.get(field) in (None, "")]
    errors = [f"missing {field}" for field in missing]
    if event.get("direction") and event["direction"] not in {"CREDIT", "DEBIT"}:
        errors.append("direction must be CREDIT or DEBIT")
    try:
        if event.get("amount") not in (None, "") and float(event["amount"]) < 0:
            errors.append("amount must be non-negative")
    except (TypeError, ValueError):
        errors.append("amount must be numeric")
    return errors


def process_transaction(conn, event: dict, now: str) -> str:
    errors = _validation_errors(event)
    if errors:
        detail = "; ".join(errors)
        _insert_processed(conn, event, "INPUT_REJECTED", now)
        log(conn, "INPUT_REJECTED", f"Malformed intermediary record rejected: {detail}.", now, event.get("program_id"))
        return "INPUT_REJECTED"

    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (event["program_id"],)).fetchone()
    if program and program["status"] == "RESTRICTED":
        _insert_processed(conn, event, "BLOCKED_BY_RESTRICTION", now)
        log(conn, "EVENT_BLOCKED", f"{event['middleware_txn_id']} retained for evidence but blocked from normal processing.", now, event["program_id"])
        return "BLOCKED_BY_RESTRICTION"

    bank = conn.execute(
        "SELECT * FROM bank_transactions WHERE middleware_reference = ? AND status = 'POSTED'",
        (event["middleware_txn_id"],),
    ).fetchone()

    if not bank:
        settings = get_settings_from_conn(conn)
        expires = (datetime.fromisoformat(now) + timedelta(seconds=int(settings["match_grace_seconds"]))).isoformat(timespec="seconds")
        _insert_processed(
            conn, event, "PENDING_MATCH", now,
            match_deadline_at=expires,
        )
        log(conn, "PENDING_MATCH_CREATED", f"{event['middleware_txn_id']} awaiting a bank record until {expires}.", now, event["program_id"])
        return "PENDING_MATCH"

    mismatch_type, difference = _mismatch(bank, event)

    if mismatch_type:
        create_exception(conn, mismatch_type, difference, now, middleware_txn_id=event["middleware_txn_id"], bank_txn_id=bank["bank_txn_id"])
        _insert_processed(conn, event, "CONFIRMED_EXCEPTION", now, bank["bank_txn_id"])
        evaluate_risk(conn, now, event["program_id"])
        return "CONFIRMED_EXCEPTION"

    _apply_expected_balance(conn, event, now)
    _insert_processed(conn, event, "MATCHED", now, bank["bank_txn_id"])
    late_exception = conn.execute(
        """SELECT exception_id FROM exceptions
           WHERE exception_type='MISSING_INTERMEDIARY_EVENT' AND bank_txn_id=?
             AND status IN ('OPEN','INVESTIGATING')""",
        (bank["bank_txn_id"],),
    ).fetchone()
    if late_exception:
        conn.execute(
            """UPDATE exceptions SET status='RESOLVED', resolved_at=?,
               resolution_notes='Late intermediary event arrived and matched the bank transaction.'
               WHERE exception_id=?""",
            (now, late_exception["exception_id"]),
        )
        log(conn, "EXCEPTION_RESOLVED", f"{late_exception['exception_id']} resolved by late matching intermediary event.", now, event["program_id"])
    log(conn, "TRANSACTION_MATCHED", f"{event['middleware_txn_id']} matched {bank['bank_txn_id']}; expected customer balance rolled forward.", now, event["program_id"])
    evaluate_risk(conn, now, event["program_id"])
    return "MATCHED"


def reconcile_pending(conn, now: str, program_id: str = "PROG-001") -> None:
    current = datetime.fromisoformat(now)
    for pending in conn.execute("SELECT * FROM processed_events WHERE result_status='PENDING_MATCH'").fetchall():
        bank = conn.execute(
            "SELECT * FROM bank_transactions WHERE middleware_reference = ? AND status = 'POSTED'",
            (pending["middleware_txn_id"],),
        ).fetchone()
        if bank:
            mismatch_type, difference = _mismatch(bank, pending)
            if mismatch_type:
                create_exception(
                    conn, mismatch_type, difference, now,
                    program_id=pending["program_id"], middleware_txn_id=pending["middleware_txn_id"],
                    bank_txn_id=bank["bank_txn_id"],
                )
                conn.execute(
                    """UPDATE processed_events SET result_status='CONFIRMED_EXCEPTION',
                       bank_txn_id=? WHERE event_id=?""",
                    (bank["bank_txn_id"], pending["event_id"]),
                )
            else:
                _apply_expected_balance(conn, pending, now)
                conn.execute(
                    "UPDATE processed_events SET result_status='MATCHED', bank_txn_id=? WHERE event_id=?",
                    (bank["bank_txn_id"], pending["event_id"]),
                )
                log(conn, "TRANSACTION_MATCHED", f"{pending['middleware_txn_id']} matched {bank['bank_txn_id']} within grace period; expected customer balance rolled forward.", now, program_id)
        elif current >= datetime.fromisoformat(pending["match_deadline_at"]):
            create_exception(
                conn, "MISSING_BANK_TRANSACTION", pending["amount"], now,
                program_id=pending["program_id"], middleware_txn_id=pending["middleware_txn_id"],
            )
            conn.execute(
                "UPDATE processed_events SET result_status='CONFIRMED_EXCEPTION' WHERE event_id=?",
                (pending["event_id"],),
            )

    settings = get_settings_from_conn(conn)
    grace = timedelta(seconds=int(settings["match_grace_seconds"]))
    unmatched_banks = conn.execute(
        """SELECT bank.* FROM bank_transactions bank
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
    ).fetchall()
    for bank in unmatched_banks:
        deadline = datetime.fromisoformat(bank["posted_at"]) + grace
        if current >= deadline:
            create_exception(
                conn, "MISSING_INTERMEDIARY_EVENT", bank["amount"], now,
                program_id=bank["program_id"], middleware_txn_id=bank["middleware_reference"],
                bank_txn_id=bank["bank_txn_id"],
            )
            log(
                conn, "BANK_TRANSACTION_UNMATCHED",
                f"{bank['bank_txn_id']} had no intermediary event by {deadline.isoformat(timespec='seconds')}.",
                now, bank["program_id"],
            )
    evaluate_risk(conn, now, program_id)


def receive_reconciliation_batch(conn, scenario: str, now: str, actor: str = "DEMO USER") -> dict:
    """Validate and persist the scenario's mocked immutable EOD middleware batch."""
    frame = pd.read_csv(BATCH_DIR / f"{scenario}.csv", dtype=str, keep_default_na=False)
    missing = [field for field in REQUIRED_BATCH_FIELDS if field not in frame.columns]
    if missing:
        raise ValueError(f"Batch is missing required columns: {', '.join(missing)}")
    if frame.empty:
        raise ValueError("Batch contains no customer records")
    for field in REQUIRED_BATCH_FIELDS:
        if frame[field].astype(str).str.strip().eq("").any():
            raise ValueError(f"Batch contains a blank {field}")
    if frame["end_user_id"].duplicated().any():
        raise ValueError("Batch contains duplicate end-user records")
    for field in ("batch_id", "as_of", "program_id", "currency"):
        if frame[field].nunique() != 1:
            raise ValueError(f"Batch must contain exactly one {field}")
    try:
        frame["ending_balance"] = pd.to_numeric(frame["ending_balance"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("Batch ending balances must be numeric") from exc
    if (frame["ending_balance"] < 0).any():
        raise ValueError("Batch ending balances must be non-negative")

    header = frame.iloc[0]
    batch_id = str(header["batch_id"])
    existing = conn.execute("SELECT * FROM reconciliation_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if existing:
        return dict(existing)
    program = conn.execute("SELECT currency FROM programs WHERE program_id=?", (header["program_id"],)).fetchone()
    if not program or program["currency"] != header["currency"]:
        raise ValueError("Batch program or currency does not match the monitored program")

    reported_total = float(frame["ending_balance"].sum())
    conn.execute(
        """INSERT INTO reconciliation_batches(
               batch_id, scenario, program_id, as_of, received_at, record_count,
               reported_total, currency, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RECEIVED')""",
        (batch_id, scenario, header["program_id"], header["as_of"], now, len(frame), reported_total, header["currency"]),
    )
    conn.executemany(
        "INSERT INTO batch_customer_balances VALUES (?, ?, ?, ?)",
        [(batch_id, item["program_id"], item["end_user_id"], float(item["ending_balance"])) for item in frame.to_dict(orient="records")],
    )
    log(conn, "EOD_BATCH_RECEIVED", f"{batch_id} received with {len(frame)} customer records totaling ${reported_total:,.2f} as of {header['as_of']}.", now, header["program_id"], actor)
    return dict(conn.execute("SELECT * FROM reconciliation_batches WHERE batch_id=?", (batch_id,)).fetchone())


def formal_reconciliation(conn, now: str, actor: str = "DEMO USER", program_id: str = "PROG-001", batch_id: str | None = None) -> dict:
    """Compare expected balances, the received EOD batch, and the bank FBO balance."""
    if batch_id:
        batch = conn.execute("SELECT * FROM reconciliation_batches WHERE batch_id=? AND program_id=?", (batch_id, program_id)).fetchone()
    else:
        batch = conn.execute("SELECT * FROM reconciliation_batches WHERE program_id=? ORDER BY rowid DESC LIMIT 1", (program_id,)).fetchone()
    if not batch:
        raise ValueError("Receive the EOD middleware batch before running formal reconciliation")

    bank = float(conn.execute("SELECT ledger_balance FROM bank_accounts WHERE program_id=?", (program_id,)).fetchone()["ledger_balance"])
    expected_total = float(conn.execute(
        "SELECT COALESCE(SUM(expected_balance),0) AS total FROM expected_customer_balances WHERE program_id=?",
        (program_id,),
    ).fetchone()["total"])
    reported_total = float(batch["reported_total"])
    aggregate_difference = bank - reported_total
    tolerance = float(get_settings_from_conn(conn)["aggregate_tolerance"])

    conn.execute("DELETE FROM customer_reconciliation_results WHERE batch_id=?", (batch["batch_id"],))
    customer_ids = conn.execute(
        """SELECT end_user_id FROM expected_customer_balances WHERE program_id=?
           UNION SELECT end_user_id FROM batch_customer_balances WHERE batch_id=?""",
        (program_id, batch["batch_id"]),
    ).fetchall()
    mismatches = 0
    customer_abs_difference = 0.0
    for item in customer_ids:
        end_user_id = item["end_user_id"]
        expected = conn.execute(
            "SELECT expected_balance FROM expected_customer_balances WHERE program_id=? AND end_user_id=?",
            (program_id, end_user_id),
        ).fetchone()
        reported = conn.execute(
            "SELECT reported_balance FROM batch_customer_balances WHERE batch_id=? AND end_user_id=?",
            (batch["batch_id"], end_user_id),
        ).fetchone()
        expected_balance = float(expected["expected_balance"]) if expected else 0.0
        reported_balance = float(reported["reported_balance"]) if reported else 0.0
        difference = expected_balance - reported_balance
        row_outcome = "MATCHED" if abs(difference) <= tolerance else "EXCEPTION"
        if row_outcome == "EXCEPTION":
            mismatches += 1
            customer_abs_difference += abs(difference)
        conn.execute(
            "INSERT INTO customer_reconciliation_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch["batch_id"], program_id, end_user_id, expected_balance, reported_balance, difference, row_outcome),
        )

    outcome = "MATCHED" if abs(aggregate_difference) <= tolerance and mismatches == 0 else "EXCEPTION"
    conn.execute(
        """UPDATE reconciliation_batches
           SET status='RECONCILED', reconciled_at=?, bank_balance=?, expected_total=?,
               aggregate_difference=?, customer_mismatch_count=?, outcome=?
           WHERE batch_id=?""",
        (now, bank, expected_total, aggregate_difference, mismatches, outcome, batch["batch_id"]),
    )
    log(
        conn,
        "FORMAL_RECON_RUN",
        f"{batch['batch_id']}: bank ${bank:,.2f}; expected ${expected_total:,.2f}; Synapse batch ${reported_total:,.2f}; bank-minus-batch ${aggregate_difference:,.2f}; {mismatches} customer mismatch(es); {outcome}.",
        now,
        program_id,
        actor,
    )
    if outcome == "EXCEPTION":
        control_exposure = max(abs(aggregate_difference), customer_abs_difference / 2)
        existing = conn.execute(
            "SELECT exception_id FROM exceptions WHERE exception_type='AGGREGATE_BALANCE_MISMATCH' AND status IN ('OPEN','INVESTIGATING')"
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE exceptions SET amount_difference=?, resolution_notes=? WHERE exception_id=?",
                (control_exposure, f"Latest failed batch: {batch['batch_id']}", existing["exception_id"]),
            )
        else:
            create_exception(conn, "AGGREGATE_BALANCE_MISMATCH", control_exposure, now, program_id=program_id, owner="Reconciliation Analyst")
        evaluate_risk(conn, now, program_id)

    return {
        "batch_id": batch["batch_id"], "as_of": batch["as_of"],
        "bank_balance": bank, "expected_total": expected_total,
        "reported_total": reported_total,
        "difference": aggregate_difference, "customer_mismatch_count": mismatches,
        "outcome": outcome,
    }
