import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from ledgerwatch.db import get_runtime, rows, transaction
from ledgerwatch.playback import load_scenario, process_next, set_stream_state, start_stream
from ledgerwatch.reconciliation import formal_reconciliation, receive_reconciliation_batch
from ledgerwatch.ui import configure_page, page_header, program_snapshot, restriction_banner, status_badge


role = configure_page("Reconciliation")
page_header("Independent verification", "Reconciliation", "Replay both real-time feeds, roll forward bank-calculated customer balances, and reconcile the received EOD beneficial-owner batch.")
snapshot = program_snapshot()
restriction_banner(snapshot)

SCENARIOS = {
    "Normal operation": "normal",
    "Accumulating discrepancies": "discrepancy",
    "Intermediary feed failure": "feed_failure",
    "Timing difference": "timing_difference",
}

control_left, control_right = st.columns([2.2, 1])
with control_left:
    st.subheader("Scenario playback")
    current_scenario = get_runtime("scenario", "normal")
    selected_label = st.selectbox(
        "Choose a synthetic scenario",
        list(SCENARIOS),
        index=list(SCENARIOS.values()).index(current_scenario),
        help="Choose a scenario, then select Load scenario to reset its feed, batch and control state.",
    )
    current_label = next(label for label, value in SCENARIOS.items() if value == current_scenario)
    st.caption(f"Currently loaded: **{current_label}** · Selecting another option does not load it until you press **Load scenario**.")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    if c1.button("Load scenario", width="stretch"):
        load_scenario(SCENARIOS[selected_label])
        st.rerun()
    if c2.button("Start", type="primary", width="stretch"):
        start_stream(actor=role)
        st.rerun()
    if c3.button("Pause", width="stretch"):
        set_stream_state("PAUSED", actor=role)
        st.rerun()
    if c4.button("Resume", width="stretch"):
        set_stream_state("RUNNING", actor=role)
        st.rerun()
    if c5.button("Next event / deadline", width="stretch"):
        process_next()
        st.rerun()
    if c6.button("Reset", width="stretch"):
        load_scenario(get_runtime("scenario", "normal"))
        st.rerun()
    st.caption("Automatic playback uses a fixed presentation cadence. Manual advance moves to the next intermediary event, bank posting or control deadline.")
with control_right:
    st.subheader("Simulation state")
    stream_state = get_runtime("stream_state", "READY")
    st.markdown(status_badge(stream_state), unsafe_allow_html=True)
    scenario_time = get_runtime("simulation_time", "—")
    st.metric("Scenario time", scenario_time.replace("T", " ") if scenario_time != "—" else "—")
    st.caption("This is the business timeline being fast-forwarded; it is independent of the presentation delay.")

st.subheader("Live intermediary event stream")
events = rows(
    """SELECT event_timestamp, event_type, middleware_txn_id, end_user_id, amount, direction,
              customer_balance, result_status, bank_txn_id
       FROM processed_events ORDER BY rowid DESC LIMIT 100"""
)
if events:
    frame = pd.DataFrame(events).rename(columns={
        "event_timestamp": "Event time", "event_type": "Type", "middleware_txn_id": "Middleware reference",
        "end_user_id": "End user", "amount": "Amount", "direction": "Direction",
        "customer_balance": "Event-reported resulting balance (informational)", "result_status": "Result",
        "bank_txn_id": "Bank record",
    })
    st.dataframe(frame, width="stretch", hide_index=True, column_config={
        "Amount": st.column_config.NumberColumn(format="$%.2f"),
        "Event-reported resulting balance (informational)": st.column_config.NumberColumn(format="$%.2f"),
    })
    st.caption("The event-reported balance is retained as evidence but never overwrites the bank-calculated expected balances or the immutable EOD snapshot.")
else:
    st.info("Load and start a scenario to replay events.")

pending = [
    {
        "waiting_for": "Bank transaction",
        "reference": item["middleware_txn_id"],
        "amount": item["amount"],
        "direction": item["direction"],
        "first_seen": item["event_timestamp"],
        "deadline": item["match_deadline_at"],
    }
    for item in rows(
        """SELECT middleware_txn_id, amount, direction, event_timestamp, match_deadline_at
           FROM processed_events WHERE result_status='PENDING_MATCH'"""
    )
]
bank_first = rows(
    """SELECT bank.bank_txn_id, bank.middleware_reference, bank.amount, bank.direction, bank.posted_at
       FROM bank_transactions bank
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
)
grace = timedelta(seconds=int(snapshot["settings"]["match_grace_seconds"]))
pending.extend(
    {
        "waiting_for": "Intermediary event",
        "reference": item["middleware_reference"],
        "amount": item["amount"],
        "direction": item["direction"],
        "first_seen": item["posted_at"],
        "deadline": (datetime.fromisoformat(item["posted_at"]) + grace).isoformat(timespec="seconds"),
    }
    for item in bank_first
)
if pending:
    st.subheader("Two-way pending matches")
    pframe = pd.DataFrame(pending).rename(columns={
        "waiting_for": "Waiting for", "reference": "Reference", "amount": "Amount",
        "direction": "Direction", "first_seen": "First seen", "deadline": "Grace expires",
    })
    st.dataframe(pframe, width="stretch", hide_index=True, column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")})

blocked = rows(
    """SELECT event_timestamp, middleware_txn_id, end_user_id, amount, direction
       FROM processed_events WHERE result_status='BLOCKED_BY_RESTRICTION'
       ORDER BY event_timestamp DESC"""
)
if blocked:
    st.subheader("Restriction enforcement evidence")
    st.error("These attempted transactions were retained as evidence but were rejected before bank posting, matching, or any balance update.")
    blocked_frame = pd.DataFrame(blocked).rename(columns={
        "event_timestamp": "Event time", "middleware_txn_id": "Reference", "end_user_id": "End user",
        "amount": "Amount", "direction": "Direction",
    })
    st.dataframe(blocked_frame, width="stretch", hide_index=True, column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")})

st.divider()
st.subheader("Real-time bank control records")
source_left, source_right = st.columns(2)
with source_left:
    st.markdown("**Bank-calculated expected customer balances**")
    expected_rows = rows(
        """SELECT end_user_id, expected_balance, last_matched_event_id, updated_at
           FROM expected_customer_balances WHERE program_id=? ORDER BY end_user_id""",
        (snapshot["program"]["program_id"],),
    )
    expected_frame = pd.DataFrame(expected_rows).rename(columns={
        "end_user_id": "End user", "expected_balance": "Expected balance",
        "last_matched_event_id": "Last bank-matched event", "updated_at": "Updated at",
    })
    st.dataframe(expected_frame, width="stretch", hide_index=True, column_config={"Expected balance": st.column_config.NumberColumn(format="$%.2f")})
    st.metric("Expected total", f"${snapshot['expected_total']:,.2f}")
    st.caption("Updated in real time only by exact bank-matched transactions.")
with source_right:
    st.markdown("**Independent bank transaction feed**")
    bank_rows = rows(
        """SELECT bank_txn_id, account_id, middleware_reference, amount, direction, status, posted_at
           FROM bank_transactions
           WHERE program_id=? AND status IN ('POSTED','REJECTED_BY_RESTRICTION')
           ORDER BY posted_at DESC""",
        (snapshot["program"]["program_id"],),
    )
    if bank_rows:
        bank_frame = pd.DataFrame(bank_rows).rename(columns={
            "bank_txn_id": "Bank transaction", "account_id": "Bank account",
            "middleware_reference": "Middleware reference",
            "amount": "Amount", "direction": "Direction", "status": "Bank status",
            "posted_at": "Bank decision time",
        })
        st.dataframe(bank_frame, width="stretch", hide_index=True, column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")})
    else:
        st.info("No bank transactions have posted or been rejected yet.")
    st.metric("Bank FBO ledger", f"${snapshot['account']['ledger_balance']:,.2f}")

st.divider()
st.subheader("EOD beneficial-owner batch and daily reconciliation")
batch = snapshot["batch"]
batch_actions = st.columns([1, 1, 2])
with batch_actions[0]:
    if st.button("Receive EOD batch", type="primary", width="stretch", disabled=stream_state != "COMPLETE"):
        with transaction() as conn:
            receive_reconciliation_batch(conn, current_scenario, get_runtime("simulation_time"), actor=role)
        st.rerun()
with batch_actions[1]:
    if st.button("Run daily reconciliation", width="stretch", disabled=not batch):
        with transaction() as conn:
            formal_reconciliation(conn, get_runtime("simulation_time"), actor=role, batch_id=batch["batch_id"])
        st.rerun()
with batch_actions[2]:
    if stream_state != "COMPLETE":
        st.info("Complete the scenario before receiving its mocked EOD batch.")
    elif not batch:
        st.info("The mocked middleware batch is available for receipt.")

if batch:
    metadata = st.columns(5)
    metadata[0].metric("Batch", batch["batch_id"])
    metadata[1].metric("As of", batch["as_of"].replace("T", " "))
    metadata[2].metric("Records", batch["record_count"])
    metadata[3].metric("Reported total", f"${float(batch['reported_total']):,.2f}")
    metadata[4].metric("Status", batch["outcome"] or batch["status"])

    reported_rows = rows(
        """SELECT end_user_id, reported_balance FROM batch_customer_balances
           WHERE batch_id=? ORDER BY end_user_id""",
        (batch["batch_id"],),
    )
    st.markdown("**Synapse-reported beneficial-owner snapshot**")
    reported_frame = pd.DataFrame(reported_rows).rename(columns={"end_user_id": "End user", "reported_balance": "EOD reported balance"})
    st.dataframe(reported_frame, width="stretch", hide_index=True, column_config={"EOD reported balance": st.column_config.NumberColumn(format="$%.2f")})
    st.caption("These immutable rows came from the mocked EOD batch; live intermediary events cannot change them.")

    if batch["status"] == "RECONCILED":
        result_metrics = st.columns(5)
        result_metrics[0].metric("Bank FBO closing balance", f"${float(batch['bank_balance']):,.2f}")
        result_metrics[1].metric("Bank-calculated expected", f"${float(batch['expected_total']):,.2f}")
        result_metrics[2].metric("Synapse batch", f"${float(batch['reported_total']):,.2f}")
        result_metrics[3].metric("Bank minus batch", f"${float(batch['aggregate_difference']):,.2f}")
        result_metrics[4].metric("Customer mismatches", batch["customer_mismatch_count"])

        result_rows = rows(
            """SELECT end_user_id, expected_balance, reported_balance, difference, outcome
               FROM customer_reconciliation_results WHERE batch_id=? ORDER BY end_user_id""",
            (batch["batch_id"],),
        )
        result_frame = pd.DataFrame(result_rows).rename(columns={
            "end_user_id": "End user", "expected_balance": "Bank-calculated expected",
            "reported_balance": "Synapse-reported EOD", "difference": "Expected minus reported",
            "outcome": "Result",
        })
        st.markdown("**Customer-level reconciliation results**")
        st.dataframe(result_frame, width="stretch", hide_index=True, column_config={
            "Bank-calculated expected": st.column_config.NumberColumn(format="$%.2f"),
            "Synapse-reported EOD": st.column_config.NumberColumn(format="$%.2f"),
            "Expected minus reported": st.column_config.NumberColumn(format="$%.2f"),
        })
        if batch["outcome"] == "MATCHED":
            st.success("Every customer balance and the aggregate FBO balance reconciled.")
        else:
            st.error("The daily control failed. Customer-level results and the aggregate difference are retained, and the exception remains open until resolved or overridden.")

if get_runtime("stream_state") == "RUNNING":
    time.sleep(0.65)
    process_next()
    st.rerun()
