import pandas as pd
import streamlit as st

from ledgerwatch.audit import log
from ledgerwatch.db import get_runtime, row, rows, transaction
from ledgerwatch.risk_engine import evaluate_risk
from ledgerwatch.config import RISK_LEVELS, risk_band_text
from ledgerwatch.ui import (
    audit_table,
    configure_page,
    deadline_text,
    format_age,
    page_header,
    program_snapshot,
    restriction_banner,
    risk_badge,
    risk_level_cards,
    status_badge,
    style_risk_column,
)


role = configure_page("Exceptions")
page_header(
    "Owned investigation",
    "Exception management",
    "Assign, investigate and resolve confirmed control failures with a complete evidence trail.",
)
snapshot = program_snapshot()
restriction_banner(snapshot)

st.markdown("**Unresolved exceptions by risk level**")
risk_level_cards(snapshot)
st.caption(
    "Each exception's risk level is separate from the overall program status. Financial exceptions are levelled by amount "
    f"({risk_band_text(snapshot['settings'])}, editable under Configuration); "
    "a stale feed is Medium and a feed failure is Critical. "
    "The resolution deadline keeps running while an exception is OPEN or INVESTIGATING."
)
st.write("")

f1, f2, f3 = st.columns(3)
status_filter = f1.multiselect(
    "Status", ["OPEN", "INVESTIGATING", "RESOLVED", "OVERRIDDEN"], default=[]
)
risk_filter = f3.multiselect("Risk level", list(RISK_LEVELS), default=[])
type_filter = f2.selectbox(
    "Exception type",
    ["All"]
    + [
        item["exception_type"]
        for item in rows("SELECT DISTINCT exception_type FROM exceptions ORDER BY exception_type")
    ],
)

clauses, params = [], []
if status_filter:
    clauses.append("status IN (%s)" % ",".join("?" * len(status_filter)))
    params.extend(status_filter)
if type_filter != "All":
    clauses.append("exception_type = ?")
    params.append(type_filter)
if risk_filter:
    clauses.append("risk_level IN (%s)" % ",".join("?" * len(risk_filter)))
    params.extend(risk_filter)
where = " WHERE " + " AND ".join(clauses) if clauses else ""
exceptions = rows(
    f"""SELECT * FROM exceptions{where}
        ORDER BY CASE WHEN status IN ('OPEN','INVESTIGATING') THEN 1 ELSE 2 END,
            CASE risk_level WHEN 'Critical' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,
            due_at""",
    tuple(params),
)

if not exceptions:
    st.info(
        "No exceptions match the selected filters. Pending matches appear here only after their grace period expires."
    )
else:
    frame = pd.DataFrame(
        [
            {
                **exc,
                "age": format_age(exc["created_at"], snapshot["sim_time"]),
                "time_left": deadline_text(exc["due_at"], snapshot["sim_time"], exc["status"]),
                "due_at": exc["due_at"].replace("T", " "),
            }
            for exc in exceptions
        ]
    )
    table = frame[
        [
            "exception_id",
            "risk_level",
            "exception_type",
            "amount_difference",
            "status",
            "owner",
            "age",
            "due_at",
            "time_left",
        ]
    ].rename(
        columns={
            "exception_id": "ID",
            "risk_level": "Risk level",
            "exception_type": "Type",
            "amount_difference": "Impact",
            "status": "Status",
            "owner": "Owner",
            "age": "Age",
            "due_at": "Resolve by",
            "time_left": "Time remaining",
        }
    )
    st.dataframe(
        style_risk_column(table).map(
            lambda value: "color:#b42335; font-weight:700" if str(value).startswith("OVERDUE") else "",
            subset=["Time remaining"],
        ),
        width="stretch",
        hide_index=True,
        column_config={"Impact": st.column_config.NumberColumn(format="$%.2f")},
    )

    st.subheader("Investigation workspace")
    labels = {
        f"{exc['exception_id']} · {exc['risk_level'] or '—'} · {exc['exception_type']} · ${exc['amount_difference']:,.2f}": exc[
            "exception_id"
        ]
        for exc in exceptions
    }
    selected = st.selectbox("Select exception", labels)
    exc = row("SELECT * FROM exceptions WHERE exception_id = ?", (labels[selected],))
    bank = (
        row("SELECT * FROM bank_transactions WHERE bank_txn_id = ?", (exc["bank_txn_id"],))
        if exc["bank_txn_id"]
        else None
    )

    d1, d2, d3, d4, d5 = st.columns(5)
    d1.markdown(f"**Status**<br>{status_badge(exc['status'])}", unsafe_allow_html=True)
    d2.markdown(f"**Risk level**<br>{risk_badge(exc['risk_level'])}", unsafe_allow_html=True)
    d3.metric("Financial impact", f"${exc['amount_difference']:,.2f}")
    d4.metric("Age", format_age(exc["created_at"], snapshot["sim_time"]))
    d5.metric("Time remaining", deadline_text(exc["due_at"], snapshot["sim_time"], exc["status"]))
    st.caption(
        f"Synapse reference: {exc['middleware_txn_id'] or '—'} · "
        f"Evolve record: {exc['bank_txn_id'] or 'No matching record'} · "
        f"Resolve by: {exc['due_at'].replace('T', ' ')} (scenario time; the deadline keeps running while INVESTIGATING)"
    )
    if bank:
        st.write(
            f"Evolve record: {bank['direction']} ${bank['amount']:,.2f} · "
            f"{bank['account_id']} · {bank['status']}"
        )

    if exc["status"] in {"RESOLVED", "OVERRIDDEN"}:
        st.info(
            f"This exception is terminal with status {exc['status']}. "
            "It no longer contributes to unresolved exposure."
        )
        if exc["resolution_notes"]:
            st.write("**Closure notes**")
            st.write(exc["resolution_notes"])
    else:
        owners = ["Unassigned", "Reconciliation Analyst", "Risk / Compliance", "Management"]
        owner_index = owners.index(exc["owner"]) if exc["owner"] in owners else 0
        with st.form("exception_action"):
            owner = st.selectbox("Owner", owners, index=owner_index)
            action = st.selectbox("Action", ["Begin / update investigation", "Resolve"])
            notes = st.text_area(
                "Investigation notes",
                value=exc["resolution_notes"] or "",
                placeholder="Record evidence, cause and disposition. Notes are required for resolution.",
            )
            submitted = st.form_submit_button("Apply action", type="primary")
            if submitted:
                if action == "Resolve" and not notes.strip():
                    st.error("Resolution notes are required.")
                else:
                    now = get_runtime("simulation_time")
                    new_status = "RESOLVED" if action == "Resolve" else "INVESTIGATING"
                    event_type = (
                        "EXCEPTION_RESOLVED"
                        if new_status == "RESOLVED"
                        else "EXCEPTION_INVESTIGATION_STARTED"
                    )
                    resolved_at = now if new_status == "RESOLVED" else None
                    with transaction() as conn:
                        conn.execute(
                            """UPDATE exceptions
                               SET owner=?, status=?, resolution_notes=?, resolved_at=?
                               WHERE exception_id=?""",
                            (owner, new_status, notes, resolved_at, exc["exception_id"]),
                        )
                        log(
                            conn,
                            event_type,
                            f"{exc['exception_id']}: {exc['status']} → {new_status}; "
                            f"owner {exc['owner']} → {owner}. Notes: {notes or 'None'}",
                            now,
                            actor=role,
                        )
                        evaluate_risk(conn, now)
                    st.rerun()

st.divider()
st.subheader("Evidence log")
audit_table(100)
