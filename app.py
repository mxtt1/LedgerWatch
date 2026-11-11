import pandas as pd
import streamlit as st

from ledgerwatch.config import format_duration
from ledgerwatch.db import rows
from ledgerwatch.ui import audit_table, configure_page, page_header, program_snapshot, restriction_banner, risk_level_cards, status_badge


configure_page("Dashboard")
page_header("Bank-side assurance", "Programme overview")
snapshot = program_snapshot()
restriction_banner(snapshot)

program = snapshot["program"]
account = snapshot["account"]
batch = snapshot["batch"]

identity, state = st.columns([3, 1])
with identity:
    st.subheader(program["program_name"])
    st.markdown(
        f"**Middleware:** {program['middleware_name']} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"**Evolve account:** {account['account_id']} &nbsp;&nbsp;|&nbsp;&nbsp; **Currency:** {account['currency']}"
    )
with state:
    st.markdown("**Programme state**")
    st.markdown(status_badge(program["status"]), unsafe_allow_html=True)

st.subheader("Balance assurance")
balance_metrics = st.columns(3)
balance_metrics[0].metric("Cash at Evolve (FBO account)", f"${account['ledger_balance']:,.2f}")
balance_metrics[1].metric("Customer total calculated by Evolve", f"${snapshot['expected_total']:,.2f}")
balance_metrics[2].metric(
    "Customer total reported by Synapse (EOD)",
    f"${snapshot['reported_total']:,.2f}" if batch else "Not received",
)

if not batch:
    st.info("EOD batch not received. Real-time matching remains active; daily reconciliation has not run.")
elif batch["status"] == "RECEIVED":
    st.info(f"EOD batch {batch['batch_id']} was received and is awaiting reconciliation.")
elif batch["outcome"] == "MATCHED":
    st.success(f"EOD batch {batch['batch_id']} matched at both customer and aggregate levels.")
else:
    st.error(
        f"EOD reconciliation failed: Evolve minus Synapse ${float(batch['aggregate_difference']):,.2f}; "
        f"{batch['customer_mismatch_count']} customer mismatch(es)."
    )

st.subheader("Items requiring attention")
attention_metrics = st.columns(5)
attention_metrics[0].metric("Unresolved exposure", f"${snapshot['exposure']:,.2f}")
attention_metrics[1].metric("Open / investigating", snapshot["confirmed"])
attention_metrics[2].metric("Overdue", snapshot["overdue"])
attention_metrics[3].metric("Pending matches", snapshot["pending"])
attention_metrics[4].metric("Blocked attempts", snapshot["blocked"])

if snapshot["confirmed"]:
    st.markdown("**Unresolved exceptions by risk level**")
    risk_level_cards(snapshot)
elif not snapshot["pending"]:
    st.success("No unresolved exceptions or pending matches.")

checkpoint = snapshot["checkpoint"]
if checkpoint:
    breached = checkpoint["breached_rules"] or "None"
    outcome_labels = {
        "NO_RESTRICTION": "No restriction required",
        "RESTRICTED": "Programme restricted",
        "ALREADY_RESTRICTED": "Programme already restricted",
        "BREACHED_AUTO_RESTRICT_OFF": "Rules breached; automatic restriction was off",
    }
    checkpoint_message = (
        f"Last daily checkpoint: {checkpoint['run_at'].replace('T', ' ')} · "
        f"{outcome_labels.get(checkpoint['outcome'], checkpoint['outcome'])} · breached rules: {breached}"
    )
    if checkpoint["outcome"] == "NO_RESTRICTION":
        st.success(checkpoint_message)
    elif checkpoint["outcome"] == "BREACHED_AUTO_RESTRICT_OFF":
        st.warning(checkpoint_message)
    else:
        st.error(checkpoint_message)
else:
    st.info("Daily risk checkpoint not yet run for this scenario.")

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
with st.expander("Supporting Evolve records"):
    ledger_left, ledger_right = st.columns(2)
    with ledger_left:
        st.markdown("**Bank-calculated customer balances**")
        expected_frame = pd.DataFrame(expected_balances).rename(columns={
            "end_user_id": "End user", "expected_balance": "Expected balance",
            "last_matched_event_id": "Last matched event", "updated_at": "Updated at",
        })
        st.dataframe(expected_frame, width="stretch", hide_index=True, column_config={
            "Expected balance": st.column_config.NumberColumn(format="$%.2f")
        })
    with ledger_right:
        st.markdown("**Independent Evolve transactions**")
        if bank_transactions:
            bank_frame = pd.DataFrame(bank_transactions).rename(columns={
                "bank_txn_id": "Evolve transaction", "account_id": "Evolve account",
                "middleware_reference": "Synapse reference",
                "amount": "Amount", "direction": "Direction", "status": "Status",
                "posted_at": "Decision time",
            })
            st.dataframe(bank_frame, width="stretch", hide_index=True, column_config={
                "Amount": st.column_config.NumberColumn(format="$%.2f")
            })
        else:
            st.info("No Evolve transactions have posted or been rejected in this scenario.")

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
        "but were rejected before Evolve posting, matching, or any balance update."
    )
    blocked_frame = pd.DataFrame(blocked_events).rename(columns={
        "event_timestamp": "Event time", "middleware_txn_id": "Reference", "end_user_id": "End user",
        "amount": "Amount", "direction": "Direction", "result_status": "Result",
    })
    st.dataframe(blocked_frame, width="stretch", hide_index=True, column_config={
        "Amount": st.column_config.NumberColumn(format="$%.2f")
    })

with st.expander("Control monitoring details"):
    feed_col, threshold_col = st.columns([1, 1.6])
    with feed_col:
        st.markdown("**Synapse feed health**")
        age = "No heartbeat received" if snapshot["heartbeat_age"] is None else f"Last heartbeat {format_duration(snapshot['heartbeat_age'])} ago in scenario time"
        st.markdown(f'<div class="info-card"><small>Current signal</small><b>{status_badge(snapshot["feed_health"])}</b><p>{age}</p></div>', unsafe_allow_html=True)
    with threshold_col:
        st.markdown("**Exposure thresholds**")
        settings = snapshot["settings"]
        exposure = snapshot["exposure"]
        threshold_data = pd.DataFrame([
            {"Control level": "Warning label", "Threshold": settings["warning_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["warning_discrepancy_amount"]},
            {"Control level": "High-risk label", "Threshold": settings["high_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["high_discrepancy_amount"]},
            {"Control level": "Restriction at checkpoint", "Threshold": settings["restriction_discrepancy_amount"], "Current exposure": exposure, "Breached": exposure >= settings["restriction_discrepancy_amount"]},
        ])
        st.dataframe(threshold_data, width="stretch", hide_index=True, column_config={
            "Threshold": st.column_config.NumberColumn(format="$%.2f"),
            "Current exposure": st.column_config.NumberColumn(format="$%.2f"),
            "Breached": st.column_config.CheckboxColumn(),
        })

st.subheader("Recent control activity")
audit_table(8)
