"""Exposure, risk levels, resolution deadlines, daily checkpoint, feed-health and override rules."""

from datetime import datetime, timedelta
from uuid import uuid4

from .audit import log
from .config import (
    FINANCIAL_EXCEPTION_TYPES,
    OWNER_RISK,
    UNRESOLVED_STATUSES,
    deadline_seconds_for,
    feed_health,
    format_duration,
    risk_level_for,
    status_for_exposure,
)
from .db import get_settings


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def unresolved_exposure(conn, program_id: str = "PROG-001") -> float:
    statuses = ",".join("?" for _ in UNRESOLVED_STATUSES)
    transactional_types = tuple(FINANCIAL_EXCEPTION_TYPES - {"AGGREGATE_BALANCE_MISMATCH"})
    transaction_placeholders = ",".join("?" for _ in transactional_types)
    transaction_total = conn.execute(
        f"""SELECT COALESCE(SUM(ABS(amount_difference)), 0) AS total
            FROM exceptions WHERE program_id = ?
            AND exception_type IN ({transaction_placeholders}) AND status IN ({statuses})""",
        (program_id, *transactional_types, *UNRESOLVED_STATUSES),
    ).fetchone()
    aggregate_total = conn.execute(
        f"""SELECT COALESCE(MAX(ABS(amount_difference)), 0) AS total
            FROM exceptions WHERE program_id = ?
            AND exception_type = 'AGGREGATE_BALANCE_MISMATCH' AND status IN ({statuses})""",
        (program_id, *UNRESOLVED_STATUSES),
    ).fetchone()
    return max(float(transaction_total["total"] or 0), float(aggregate_total["total"] or 0))


def open_exception_count(conn, program_id: str = "PROG-001") -> int:
    placeholders = ",".join("?" for _ in UNRESOLVED_STATUSES)
    return int(conn.execute(
        f"SELECT COUNT(*) AS count FROM exceptions WHERE program_id = ? AND status IN ({placeholders})",
        (program_id, *UNRESOLVED_STATUSES),
    ).fetchone()["count"])


def create_exception(
    conn,
    exception_type: str,
    amount_difference: float,
    now: str,
    *,
    program_id: str = "PROG-001",
    middleware_txn_id: str | None = None,
    bank_txn_id: str | None = None,
    owner: str | None = "Unassigned",
) -> str:
    settings = get_settings_from_conn(conn)
    risk_level = risk_level_for(exception_type, amount_difference, settings)
    allowed = deadline_seconds_for(risk_level, settings)
    due_at = (_dt(now) + timedelta(seconds=allowed)).isoformat(timespec="seconds")
    exception_id = f"EXC-{uuid4().hex[:10].upper()}"
    conn.execute(
        """INSERT INTO exceptions(exception_id, program_id, exception_type, middleware_txn_id, bank_txn_id,
           amount_difference, status, owner, created_at, due_at, risk_level)
           VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)""",
        (exception_id, program_id, exception_type, middleware_txn_id, bank_txn_id, abs(float(amount_difference)), owner, now, due_at, risk_level),
    )
    detail = (
        f"{exception_id}: {exception_type} created; "
        f"Synapse reference {middleware_txn_id or 'none'}; bank record {bank_txn_id or 'none'}; "
        f"impact ${abs(float(amount_difference)):,.2f}; "
        f"{risk_level} risk; resolve within {format_duration(allowed)} (by {due_at})."
    )
    log(conn, "EXCEPTION_CREATED", detail, now, program_id)
    return exception_id


def reclassify_exception(conn, exception_id: str, amount_difference: float, now: str) -> None:
    """Re-level an exception whose amount changed. A higher level can only shorten the deadline."""
    exc = conn.execute("SELECT * FROM exceptions WHERE exception_id = ?", (exception_id,)).fetchone()
    settings = get_settings_from_conn(conn)
    risk_level = risk_level_for(exc["exception_type"], amount_difference, settings)
    allowed = deadline_seconds_for(risk_level, settings)
    due_at = min(exc["due_at"], (_dt(exc["created_at"]) + timedelta(seconds=allowed)).isoformat(timespec="seconds"))
    conn.execute(
        "UPDATE exceptions SET amount_difference = ?, risk_level = ?, due_at = ? WHERE exception_id = ?",
        (abs(float(amount_difference)), risk_level, due_at, exception_id),
    )
    if risk_level != exc["risk_level"]:
        log(conn, "EXCEPTION_RECLASSIFIED", f"{exception_id}: {exc['risk_level']} → {risk_level} risk; resolve by {due_at}.", now, exc["program_id"])


def overdue_exceptions(conn, now: str, program_id: str = "PROG-001") -> list:
    """Unresolved exceptions past their resolution deadline. The clock runs while OPEN or INVESTIGATING."""
    placeholders = ",".join("?" for _ in UNRESOLVED_STATUSES)
    return conn.execute(
        f"""SELECT * FROM exceptions WHERE program_id = ? AND status IN ({placeholders}) AND due_at < ?
            ORDER BY due_at""",
        (program_id, *UNRESOLVED_STATUSES, now),
    ).fetchall()


def get_settings_from_conn(conn) -> dict:
    values = {}
    for item in conn.execute("SELECT * FROM settings").fetchall():
        raw = item["value"]
        if item["data_type"] == "int":
            values[item["key"]] = int(float(raw))
        elif item["data_type"] == "float":
            values[item["key"]] = float(raw)
        elif item["data_type"] == "bool":
            values[item["key"]] = raw.lower() in {"true", "1", "yes"}
        else:
            values[item["key"]] = raw
    return values


def restrict_program(conn, reason: str, now: str, program_id: str = "PROG-001") -> bool:
    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()
    if program["status"] == "RESTRICTED" or not program["auto_restrict_enabled"]:
        return False
    conn.execute(
        "UPDATE programs SET status = 'RESTRICTED', restriction_reason = ?, updated_at = ? WHERE program_id = ?",
        (reason, now, program_id),
    )
    conn.execute("UPDATE bank_accounts SET status = 'RESTRICTED', updated_at = ? WHERE program_id = ?", (now, program_id))
    paused = conn.execute(
        "UPDATE runtime SET value='PAUSED' WHERE key='stream_state' AND value='RUNNING'"
    ).rowcount
    log(conn, "THRESHOLD_BREACHED", reason, now, program_id)
    log(conn, "SERVICE_RESTRICTED", reason, now, program_id)
    if paused:
        log(conn, "STREAM_PAUSED", "Scenario playback paused at the restriction decision point.", now, program_id)
    return True


def evaluate_risk(conn, now: str, program_id: str = "PROG-001") -> dict:
    """Live monitoring: update the warning/high-risk label only. Financial restriction waits for the daily checkpoint."""
    settings = get_settings_from_conn(conn)
    exposure = unresolved_exposure(conn, program_id)
    count = open_exception_count(conn, program_id)
    desired_status = status_for_exposure(exposure, settings)
    if desired_status == "RESTRICTED":
        desired_status = "HIGH_RISK"
    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()

    if program["status"] != "RESTRICTED" and program["status"] != desired_status:
        old = program["status"]
        conn.execute("UPDATE programs SET status = ?, updated_at = ? WHERE program_id = ?", (desired_status, now, program_id))
        if desired_status in {"WARNING", "HIGH_RISK"}:
            log(conn, "THRESHOLD_BREACHED", f"Program moved {old} → {desired_status}; unresolved exposure ${exposure:,.2f}. Restriction is decided at the daily checkpoint.", now, program_id)
    return {"exposure": exposure, "count": count}


def checkpoint_rules(conn, now: str, program_id: str = "PROG-001") -> list[dict]:
    """The restriction rules applied at the daily checkpoint, each with its current value and limit."""
    settings = get_settings_from_conn(conn)
    exposure = unresolved_exposure(conn, program_id)
    count = open_exception_count(conn, program_id)
    overdue = overdue_exceptions(conn, now, program_id)
    return [
        {
            "rule": "Missed resolution deadline",
            "value": f"{len(overdue)} overdue",
            "limit": "0 overdue",
            "breached": bool(overdue),
            "reason": (
                f"{len(overdue)} unresolved exception(s) passed their resolution deadline: "
                + ", ".join(f"{item['exception_id']} ({item['risk_level']}, due {item['due_at']})" for item in overdue)
            ) if overdue else "",
        },
        {
            "rule": "Unresolved financial exposure",
            "value": f"${exposure:,.2f}",
            "limit": f"${float(settings['restriction_discrepancy_amount']):,.2f}",
            "breached": exposure >= float(settings["restriction_discrepancy_amount"]),
            "reason": f"Unresolved exposure ${exposure:,.2f} reached the ${float(settings['restriction_discrepancy_amount']):,.2f} limit.",
        },
        {
            "rule": "Unresolved exception count",
            "value": str(count),
            "limit": str(settings["max_confirmed_exception_count"]),
            "breached": count >= int(settings["max_confirmed_exception_count"]),
            "reason": f"{count} unresolved exceptions reached the limit of {settings['max_confirmed_exception_count']}.",
        },
    ]


def run_daily_checkpoint(conn, now: str, actor: str, program_id: str = "PROG-001") -> dict:
    """Explicit end-of-day evaluation that applies the agreed restriction rules."""
    rules = checkpoint_rules(conn, now, program_id)
    breached = [rule for rule in rules if rule["breached"]]
    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()
    if not breached:
        outcome = "NO_RESTRICTION"
    elif program["status"] == "RESTRICTED":
        outcome = "ALREADY_RESTRICTED"
    elif not program["auto_restrict_enabled"]:
        outcome = "BREACHED_AUTO_RESTRICT_OFF"
    else:
        outcome = "RESTRICTED"

    overdue = overdue_exceptions(conn, now, program_id)
    conn.execute(
        "INSERT INTO daily_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"CHK-{uuid4().hex[:8].upper()}", program_id, now, actor,
            unresolved_exposure(conn, program_id), open_exception_count(conn, program_id), len(overdue),
            "; ".join(rule["rule"] for rule in breached), outcome,
        ),
    )
    summary = "; ".join(f"{rule['rule']}: {rule['value']} (limit {rule['limit']}){' BREACHED' if rule['breached'] else ''}" for rule in rules)
    log(conn, "DAILY_CHECKPOINT_RUN", f"{summary}. Outcome: {outcome}.", now, program_id, actor)
    if outcome == "RESTRICTED":
        restrict_program(conn, "Daily checkpoint: " + " ".join(rule["reason"] for rule in breached), now, program_id)
    return {"outcome": outcome, "rules": rules}


def resume_program_with_override(
    conn,
    reason: str,
    now: str,
    actor: str,
    program_id: str = "PROG-001",
) -> list[str]:
    if actor != "Management":
        raise PermissionError("Only Management can resume a restricted program.")
    if not reason.strip():
        raise ValueError("A Management rationale is required.")

    unresolved = conn.execute(
        "SELECT * FROM exceptions WHERE program_id = ? AND status IN ('OPEN', 'INVESTIGATING')",
        (program_id,),
    ).fetchall()
    log(conn, "OVERRIDE_APPROVED", f"Management approved resumption. Rationale: {reason}", now, program_id, actor)
    overridden_ids = []
    for exc in unresolved:
        existing_notes = (exc["resolution_notes"] or "").strip()
        override_note = f"Management override: {reason}"
        notes = f"{existing_notes}\n\n{override_note}" if existing_notes else override_note
        conn.execute(
            "UPDATE exceptions SET status='OVERRIDDEN', resolved_at=?, resolution_notes=? WHERE exception_id=?",
            (now, notes, exc["exception_id"]),
        )
        log(
            conn,
            "EXCEPTION_OVERRIDDEN",
            f"{exc['exception_id']} changed from {exc['status']} to OVERRIDDEN during service resumption. Rationale: {reason}",
            now,
            program_id,
            actor,
        )
        overridden_ids.append(exc["exception_id"])

    conn.execute(
        "UPDATE programs SET status='ACTIVE', restriction_reason=NULL, updated_at=? WHERE program_id=?",
        (now, program_id),
    )
    conn.execute(
        "UPDATE bank_accounts SET status='ACTIVE', updated_at=? WHERE program_id=?",
        (now, program_id),
    )
    log(
        conn,
        "SERVICE_RESUMED",
        f"Simulated service resumed; {len(overridden_ids)} unresolved exception(s) were overridden. Rationale: {reason}",
        now,
        program_id,
        actor,
    )
    return overridden_ids


def monitor_feed(conn, now: str, program_id: str = "PROG-001") -> str:
    settings = get_settings_from_conn(conn)
    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()
    if not program["last_heartbeat_at"]:
        return "AWAITING_HEARTBEAT"
    age = (_dt(now) - _dt(program["last_heartbeat_at"])).total_seconds()
    health = feed_health(age, settings)

    if health == "STALE":
        exists = conn.execute(
            "SELECT 1 FROM exceptions WHERE program_id = ? AND exception_type = 'STALE_FEED' AND status IN ('OPEN','INVESTIGATING')",
            (program_id,),
        ).fetchone()
        if not exists:
            create_exception(conn, "STALE_FEED", 0, now, program_id=program_id, owner=OWNER_RISK)
    elif health == "CRITICAL":
        exists = conn.execute(
            "SELECT 1 FROM exceptions WHERE program_id = ? AND exception_type = 'FEED_FAILURE' AND status IN ('OPEN','INVESTIGATING')",
            (program_id,),
        ).fetchone()
        if not exists:
            create_exception(conn, "FEED_FAILURE", 0, now, program_id=program_id, owner=OWNER_RISK)
        restrict_program(
            conn,
            f"Critical feed failure: no heartbeat for {format_duration(age)}; configured threshold {format_duration(settings['heartbeat_critical_seconds'])}.",
            now,
            program_id,
        )
    return health

