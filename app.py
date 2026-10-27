import pandas as pd
import streamlit as st

from ledgerwatch.config import format_duration
from ledgerwatch.db import rows
from ledgerwatch.ui import audit_table, configure_page, format_age, page_header, program_snapshot, restriction_banner, status_badge


configure_page("Dashboard")
page_header("Program oversight", "Control room", "Independent visibility into middleware records, bank transactions, daily beneficial-owner controls and service risk.")
snapshot = program_snapshot()
restriction_banner(snapshot)

program = snapshot["program"]
account = snapshot["account"]
batch = snapshot["batch"]

left, right = st.columns([2, 1])
with left:
    st.subheader(program["program_name"])
    st.caption(f"{program['middleware_name']}  ·  {account['account_id']}  ·  {account['currency']}")
with right:
    st.markdown("**Program state**")
    st.markdown(status_badge(program["status"]), unsafe_allow_html=True)

st.subheader("Daily beneficial-owner control")
control_metrics = st.columns(4)
control_metrics[0].metric("Bank FBO ledger", f"${account['ledger_balance']:,.2f}")
control_metrics[1].metric("Bank-calculated expected balances", f"${snapshot['expected_total']:,.2f}")
control_metrics[2].metric("Latest Synapse batch", f"${snapshot['reported_total']:,.2f}" if batch else "Not received")
control_metrics[3].metric("Daily reconciliation", batch["outcome"] if batch and batch["outcome"] else ("Batch received" if batch else "Awaiting batch"))

if not batch:
    st.info("No current EOD beneficial-owner batch has been received. Expected balances are rolling forward from the prior validated opening snapshot using only bank-matched transactions.")
elif batch["status"] == "RECEIVED":
    st.info(f"Batch {batch['batch_id']} was received for {batch['as_of']} and is awaiting daily reconciliation.")
elif batch["outcome"] == "MATCHED":
    st.success(f"Batch {batch['batch_id']} reconciled with no customer-level or aggregate differences.")
else:
    st.error(
        f"Batch {batch['batch_id']} failed daily reconciliation: "
        f"bank-minus-batch difference ${float(batch['aggregate_difference']):,.2f}; "
        f"{batch['customer_mismatch_count']} customer record(s) differ."
    )

risk_metrics = st.columns(5)
risk_metrics[0].metric("Unresolved exposure", f"${snapshot['exposure']:,.2f}")
risk_metrics[1].metric("Unresolved exceptions", snapshot["confirmed"])
risk_metrics[2].metric("Pending matches", snapshot["pending"])
risk_metrics[3].metric("Blocked events", snapshot["blocked"])
risk_metrics[4].metric("Oldest unresolved", format_age(snapshot["oldest"], snapshot["sim_time"]))

expected_balances = rows(
    """SELECT end_user_id, expected_balance, last_matched_event_id, updated_at
       FROM expected_customer_balances WHERE program_id=? ORDER BY end_user_id""",
    (program["program_id"],),
)
bank_transactions = rows(
    """SELECT bank_txn_id, account_id, middleware_reference, amount, direction, status, posted_at
       FROM bank_transactions
       WHERE program_id=? AND status IN ('POSTED','REJECTED_BY_RESTRICTION')
       ORDER BY posted_at DESC""",
    (program["program_id"],),
)
ledger_left, ledger_right = st.columns(2)
with ledger_left:
    st.subheader("Bank-calculated expected balances")
    expected_frame = pd.DataFrame(expected_balances).rename(columns={
        "end_user_id": "End user", "expected_balance": "Expected balance",
        "last_matched_event_id": "Last bank-matched event", "updated_at": "Updated at",
    })
    st.dataframe(expected_frame, width="stretch", hide_index=True, column_config={
        "Expected balance": st.column_config.NumberColumn(format="$%.2f")
    })
    st.caption("This table updates in real time only after an exact bank match. It is not the Synapse-reported ledger.")
with ledger_right:
    st.subheader("Independent bank transactions")
    if bank_transactions:
        bank_frame = pd.DataFrame(bank_transactions).rename(columns={
            "bank_txn_id": "Bank transaction", "account_id": "Bank account",
            "middleware_reference": "Middleware reference",
            "amount": "Amount", "direction": "Direction", "status": "Bank status",
            "posted_at": "Bank decision time",
        })
        st.dataframe(bank_frame, width="stretch", hide_index=True, column_config={
            "Amount": st.column_config.NumberColumn(format="$%.2f")
        })
    else:
        st.info("No bank transactions have posted or been rejected yet for this scenario.")
    st.caption("Rejected transactions remain visible as evidence but never change the FBO balance. Scheduled future records remain invisible.")

blocked_events = rows(
    """SELECT event_timestamp, middleware_txn_id, end_user_id, amount, direction, result_status
       FROM processed_events WHERE program_id=? AND result_status='BLOCKED_BY_RESTRICTION'
       ORDER BY event_timestamp DESC""",
    (program["program_id"],),
)
if blocked_events:
    st.subheader("Restriction enforcement evidence")
    st.error(
        f"{len(blocked_events)} transaction attempt(s) arrived after restriction and were retained as evidence, "
        "but were rejected before bank posting, matching, or any balance update."
    )
    blocked_frame = pd.DataFrame(blocked_events).rename(columns={
        "event_timestamp": "Event time", "middleware_txn_id": "Reference", "end_user_id": "End user",
        "amount": "Amount", "direction": "Direction", "result_status": "Result",
    })
    st.dataframe(blocked_frame, width="stretch", hide_index=True, column_config={
        "Amount": st.column_config.NumberColumn(format="$%.2f")
    })

st.write("")
feed_col, threshold_col = st.columns([1, 1.6])
with feed_col:
    st.subheader("Feed health")
    age = "No heartbeat received" if snapshot["heartbeat_age"] is None else f"Last heartbeat {format_duration(snapshot['heartbeat_age'])} ago in scenario time"
    st.markdown(f'<div class="info-card"><small>Current signal</small><b>{status_badge(snapshot["feed_health"])}</b><p>{age}</p></div>', unsafe_allow_html=True)
    st.caption(
        f"Warning after {format_duration(snapshot['settings']['heartbeat_warning_seconds'])} "
        f"· critical after {format_duration(snapshot['settings']['heartbeat_critical_seconds'])}"
    )
with threshold_col:
    st.subheader("Exposure thresholds")
    settings = snapshot["settings"]
    exposure = snapshot["exposure"]
    threshold_data = pd.DataFrame([
        {"Control level": "Warning", "Threshold": settings["warning_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["warning_discrepancy_amount"]},
        {"Control level": "High risk", "Threshold": settings["high_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["high_discrepancy_amount"]},
        {"Control level": "Restriction", "Threshold": settings["restriction_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["restriction_discrepancy_amount"]},
    ])
    st.dataframe(threshold_data, width="stretch", hide_index=True, column_config={
        "Threshold": st.column_config.NumberColumn(format="$%.2f"),
        "Current exposure": st.column_config.NumberColumn(format="$%.2f"),
        "Breached": st.column_config.CheckboxColumn(),
    })

st.subheader("Recent control activity")
audit_table(8)

with st.expander("Control design note"):
    st.write("Real-time expected balances and Synapse EOD batch snapshots are separate records. Daily reconciliation compares them customer by customer, then compares the batch total with the bank FBO balance.")
