"""Exposure, exception-age, feed-health, override, and circuit-breaker rules."""

from datetime import datetime, timedelta
from uuid import uuid4

from .audit import log
from .config import FINANCIAL_EXCEPTION_TYPES, UNRESOLVED_STATUSES, feed_health, format_duration, status_for_exposure
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
    due_at = (
        _dt(now) + timedelta(seconds=float(settings["max_open_exception_age_seconds"]))
    ).isoformat(timespec="seconds")
    exception_id = f"EXC-{uuid4().hex[:10].upper()}"
    conn.execute(
        """INSERT INTO exceptions(exception_id, program_id, exception_type, middleware_txn_id, bank_txn_id,
           amount_difference, status, owner, created_at, due_at)
           VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
        (exception_id, program_id, exception_type, middleware_txn_id, bank_txn_id, abs(float(amount_difference)), owner, now, due_at),
    )
    detail = f"{exception_type} created; impact ${abs(float(amount_difference)):,.2f}."
    log(conn, "EXCEPTION_CREATED", detail, now, program_id)
    return exception_id


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
    settings = get_settings_from_conn(conn)
    exposure = unresolved_exposure(conn, program_id)
    count = open_exception_count(conn, program_id)
    desired_status = status_for_exposure(exposure, settings)
    program = conn.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()

    restricted = False
    if exposure >= float(settings["restriction_discrepancy_amount"]):
        reason = (
            f"Cumulative confirmed discrepancy ${exposure:,.2f} reached the configured "
            f"${float(settings['restriction_discrepancy_amount']):,.2f} restriction threshold."
        )
        restricted = restrict_program(conn, reason, now, program_id)
    elif count >= int(settings["max_confirmed_exception_count"]):
        reason = f"Open exception count {count} reached configured limit {settings['max_confirmed_exception_count']}."
        restricted = restrict_program(conn, reason, now, program_id)
    elif program["status"] != "RESTRICTED" and program["status"] != desired_status:
        old = program["status"]
        conn.execute("UPDATE programs SET status = ?, updated_at = ? WHERE program_id = ?", (desired_status, now, program_id))
        if desired_status in {"WARNING", "HIGH_RISK"}:
            log(conn, "THRESHOLD_BREACHED", f"Program moved {old} → {desired_status}; unresolved exposure ${exposure:,.2f}.", now, program_id)

    restrict_aged_open_exception(conn, now, program_id)
    return {"exposure": exposure, "count": count, "restricted": restricted}


def restrict_aged_open_exception(conn, now: str, program_id: str = "PROG-001") -> None:
    settings = get_settings_from_conn(conn)
    maximum_age = float(settings["max_open_exception_age_seconds"])
    for exc in conn.execute(
        "SELECT * FROM exceptions WHERE program_id = ? AND status = 'OPEN'",
        (program_id,),
    ).fetchall():
        age = (_dt(now) - _dt(exc["created_at"])).total_seconds()
        if age >= maximum_age:
            restrict_program(
                conn,
                f"Open exception {exc['exception_id']} ({exc['exception_type']}) was not taken into investigation within {format_duration(maximum_age)}.",
                now,
                program_id,
            )


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
            create_exception(conn, "STALE_FEED", 0, now, program_id=program_id, owner="Risk / Compliance")
    elif health == "CRITICAL":
        exists = conn.execute(
            "SELECT 1 FROM exceptions WHERE program_id = ? AND exception_type = 'FEED_FAILURE' AND status IN ('OPEN','INVESTIGATING')",
            (program_id,),
        ).fetchone()
        if not exists:
            create_exception(conn, "FEED_FAILURE", 0, now, program_id=program_id, owner="Risk / Compliance")
        restrict_program(
            conn,
            f"Critical feed failure: no heartbeat for {format_duration(age)}; configured threshold {format_duration(settings['heartbeat_critical_seconds'])}.",
            now,
            program_id,
        )
    return health

