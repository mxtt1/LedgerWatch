"""Immutable evidence log helpers."""

from uuid import uuid4


def log(conn, event_type: str, details: str, created_at: str, program_id: str = "PROG-001", actor: str = "SYSTEM") -> str:
    event_id = f"AUD-{uuid4().hex[:12].upper()}"
    conn.execute(
        "INSERT INTO audit_log(event_id, program_id, event_type, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, program_id, event_type, actor, details, created_at),
    )
    return event_id
