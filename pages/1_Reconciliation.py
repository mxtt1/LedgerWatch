import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from ledgerwatch.db import get_runtime, rows, transaction
from ledgerwatch.playback import (
    advance_clock,
    load_scenario,
    process_next,
    scenario_events,
    set_stream_state,
    simulate_customer_transfer,
    start_stream,
)
from ledgerwatch.reconciliation import formal_reconciliation, receive_reconciliation_batch
from ledgerwatch.risk_engine import checkpoint_rules, run_daily_checkpoint
from ledgerwatch.ui import configure_page, page_header, program_snapshot, restriction_banner, status_badge


role = configure_page("Reconciliation")
page_header("Independent verification", "Reconciliation")
snapshot = program_snapshot()
restriction_banner(snapshot)

SCENARIOS = {
    "1 · Normal operation": "normal",
    "2 · Risk levels: one Low, one Medium, one Critical": "risk_levels",
    "3 · Missed resolution deadline": "overdue",
    "4 · Lockdown at the daily checkpoint": "lockdown",
    "5 · Timing difference": "timing_difference",
    "6 · Synapse feed failure": "feed_failure",
}
SCENARIO_NOTES = {
    "normal": "One 1:1 match and one two-event grouped match; no exceptions.",
    "risk_levels": "Low \\$200, Medium \\$1,500 and Critical \\$6,000 discrepancies; no restriction.",
    "overdue": "A Critical \\$5,000 missing transfer becomes overdue; no checkpoint runs.",
    "lockdown": "The 17:00 checkpoint restricts service; a later \\$125 transfer is blocked.",
    "timing_difference": "A \\$700 event matches within the five-minute grace period.",
    "feed_failure": "Heartbeats stop; feed failure restricts the program immediately.",
}

control_left, control_right = st.columns([2.2, 1])
with control_left:
    st.subheader("Scenario playback")
    current_scenario = get_runtime("scenario", "normal")
    if current_scenario not in SCENARIOS.values():
        # A database saved before the scenarios were split may still name a removed scenario.
        load_scenario("normal")
        st.rerun()
    selected_label = st.selectbox(
        "Choose a synthetic scenario",
        list(SCENARIOS),
        index=list(SCENARIOS.values()).index(current_scenario),
        help="Choose a scenario, then press Start. Load scenario resets its feed, batch and control state without playing it.",
    )
    selected_scenario = SCENARIOS[selected_label]
    current_label = next(label for label, value in SCENARIOS.items() if value == current_scenario)
    st.caption(f"{SCENARIO_NOTES[selected_scenario]} Loaded: **{current_label}**.")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    if c1.button("Load scenario", width="stretch"):
        load_scenario(selected_scenario)
        st.rerun()
    if c2.button("Start", type="primary", width="stretch"):
        if selected_scenario != current_scenario or get_runtime("stream_state") == "COMPLETE":
            load_scenario(selected_scenario)
        start_stream(actor=role)
        st.rerun()
    if c3.button("Pause", width="stretch"):
        set_stream_state("PAUSED", actor=role)
        st.rerun()
    if c4.button("Resume", width="stretch"):
        set_stream_state("RUNNING", actor=role)
        st.rerun()
    if c5.button("Next event / deadline", width="stretch"):
        if selected_scenario != current_scenario:
            load_scenario(selected_scenario)
        process_next()
        st.rerun()
    if c6.button("Reset", width="stretch"):
        load_scenario(get_runtime("scenario", "normal"))
        st.rerun()
with control_right:
    st.subheader("Simulation state")
    stream_state = get_runtime("stream_state", "READY")
    st.markdown(status_badge(stream_state), unsafe_allow_html=True)
    scenario_time = get_runtime("simulation_time", "—")
    st.metric("Scenario time", scenario_time.replace("T", " ") if scenario_time != "—" else "—")
st.subheader("Live event stream")
events = rows(
    """SELECT event_timestamp, event_type, middleware_txn_id, end_user_id, amount, direction,
              customer_balance, result_status, bank_txn_id
       FROM processed_events ORDER BY rowid DESC LIMIT 100"""
)
if events:
    frame = pd.DataFrame(events).rename(columns={
        "event_timestamp": "Event time", "event_type": "Type", "middleware_txn_id": "Synapse reference",
        "end_user_id": "End user", "amount": "Amount", "direction": "Direction",
        "customer_balance": "Event-reported resulting balance (informational)", "result_status": "Result",
        "bank_txn_id": "Evolve record",
    })
    st.dataframe(frame, width="stretch", hide_index=True, height=245, column_config={
        "Amount": st.column_config.NumberColumn(format="$%.2f"),
        "Event-reported resulting balance (informational)": st.column_config.NumberColumn(format="$%.2f"),
    })
else:
    st.info("Load and start a scenario to replay events.")

pending = [
    {
        "waiting_for": item["waiting_for"],
        "reference": item["middleware_txn_id"],
        "events": item["event_count"],
        "amount": item["group_total"],
        "direction": item["direction"],
        "first_seen": item["first_seen"],
        "deadline": item["deadline"],
    }
    for item in rows(
        """SELECT event.middleware_txn_id,
                  COUNT(*) AS event_count,
                  SUM(event.amount) AS group_total,
                  MIN(event.direction) AS direction,
                  MIN(event.event_timestamp) AS first_seen,
                  MIN(event.match_deadline_at) AS deadline,
                  CASE WHEN EXISTS (
                      SELECT 1 FROM bank_transactions bank
                      WHERE bank.middleware_reference=event.middleware_txn_id
                        AND bank.status='POSTED'
                  ) THEN 'Additional Synapse event(s)'
                  ELSE 'Evolve transaction' END AS waiting_for
           FROM processed_events event
           WHERE event.result_status='PENDING_MATCH'
           GROUP BY event.middleware_txn_id"""
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
        "waiting_for": "Synapse event",
        "reference": item["middleware_reference"],
        "events": 0,
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
        "waiting_for": "Waiting for", "reference": "Reference", "events": "Events", "amount": "Event total / bank amount",
        "direction": "Direction", "first_seen": "First seen", "deadline": "Grace expires",
    })
    st.dataframe(pframe, width="stretch", hide_index=True, column_config={"Event total / bank amount": st.column_config.NumberColumn(format="$%.2f")})

blocked = rows(
    """SELECT event_timestamp, middleware_txn_id, end_user_id, amount, direction
       FROM processed_events WHERE result_status='BLOCKED_BY_RESTRICTION'
       ORDER BY event_timestamp DESC"""
)
if blocked:
    st.subheader("Restriction enforcement evidence")
    st.error("These attempted transactions were retained as evidence but were rejected before Evolve posting, matching, or any balance update.")
    blocked_frame = pd.DataFrame(blocked).rename(columns={
        "event_timestamp": "Event time", "middleware_txn_id": "Reference", "end_user_id": "End user",
        "amount": "Amount", "direction": "Direction",
    })
    st.dataframe(blocked_frame, width="stretch", hide_index=True, column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")})

st.divider()
st.subheader("Real-time Evolve records")
source_left, source_right = st.columns(2)
with source_left:
    st.markdown("**Evolve-calculated expected customer balances**")
    expected_rows = rows(
        """SELECT end_user_id, expected_balance, last_matched_event_id, updated_at
           FROM expected_customer_balances WHERE program_id=? ORDER BY end_user_id""",
        (snapshot["program"]["program_id"],),
    )
    expected_frame = pd.DataFrame(expected_rows).rename(columns={
        "end_user_id": "End user", "expected_balance": "Expected balance",
        "last_matched_event_id": "Last Evolve-matched event", "updated_at": "Updated at",
    })
    st.dataframe(expected_frame, width="stretch", hide_index=True, height=220, column_config={"Expected balance": st.column_config.NumberColumn(format="$%.2f")})
    st.metric("Expected total", f"${snapshot['expected_total']:,.2f}")
with source_right:
    st.markdown("**Evolve bank transaction feed (independent)**")
    bank_rows = rows(
        """SELECT bank_txn_id, account_id, middleware_reference, amount, direction, status, posted_at
           FROM bank_transactions
           WHERE program_id=? AND status IN ('POSTED','REJECTED_BY_RESTRICTION')
           ORDER BY posted_at DESC""",
        (snapshot["program"]["program_id"],),
    )
    if bank_rows:
        bank_frame = pd.DataFrame(bank_rows).rename(columns={
            "bank_txn_id": "Evolve transaction", "account_id": "Evolve account",
            "middleware_reference": "Synapse reference",
            "amount": "Amount", "direction": "Direction", "status": "Evolve status",
            "posted_at": "Evolve decision time",
        })
        st.dataframe(bank_frame, width="stretch", hide_index=True, height=220, column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")})
    else:
        st.info("No Evolve bank transactions have posted or been rejected yet.")
    st.metric("Evolve FBO account balance", f"${snapshot['account']['ledger_balance']:,.2f}")

with st.expander("Simulate a customer transfer"):
    customers = [item["end_user_id"] for item in rows("SELECT end_user_id FROM expected_customer_balances ORDER BY end_user_id")]
    BANK_OUTCOMES = {
        "Same amount (should match)": "same",
        "A different amount": "different",
        "Nothing yet (Evolve posting missing)": "none",
    }
    with st.form("simulate_transfer"):
        t1, t2, t3, t4 = st.columns(4)
        sim_user = t1.selectbox("Customer", customers)
        sim_kind = t2.selectbox("Fintech transfer", ["Deposit into wallet", "Withdrawal from wallet"])
        sim_amount = t3.number_input("Amount (USD)", min_value=0.01, value=50.0, step=10.0, format="%.2f")
        sim_outcome = t4.selectbox("Evolve outcome", list(BANK_OUTCOMES))
        sim_bank_amount = st.number_input(
            "Different Evolve amount (USD)", min_value=0.01, value=40.0, step=10.0, format="%.2f",
            help="Used only for 'A different amount'.",
        )
        if st.form_submit_button("Simulate transfer", type="primary", disabled=stream_state == "RUNNING"):
            try:
                st.session_state["last_transfer"] = simulate_customer_transfer(
                    sim_user, float(sim_amount),
                    "CREDIT" if sim_kind.startswith("Deposit") else "DEBIT",
                    BANK_OUTCOMES[sim_outcome], float(sim_bank_amount), actor=role,
                )
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    transfer = st.session_state.get("last_transfer")
    if transfer:
        verb = "deposits" if transfer["direction"] == "CREDIT" else "withdraws"
        bank = transfer["bank"]
        exc = transfer["exception"]
        if transfer["event_result"] == "BLOCKED_BY_RESTRICTION":
            event_text, event_class = "Restricted: event retained but blocked.", "fail"
        else:
            event_text, event_class = "Event received and evaluated for matching.", "wait"
        if not bank:
            bank_title, bank_text, bank_class = "No posting", "Nothing reached the FBO account.", "fail"
        elif bank["status"] == "REJECTED_BY_RESTRICTION":
            bank_title, bank_text, bank_class = bank["bank_txn_id"], "Rejected; FBO unchanged.", "fail"
        else:
            different = abs(float(bank["amount"]) - transfer["amount"]) > 0.001
            bank_title = bank["bank_txn_id"]
            bank_text = f"${bank['amount']:,.2f} {bank['direction']} · {bank['middleware_reference']}"
            bank_class = "fail" if different else ""
        final = transfer["final_result"]
        if final == "MATCHED":
            match_text, match_class = f"{transfer['end_user_id']}: ${transfer['balance_before']:,.2f} → ${transfer['balance_after']:,.2f}", ""
        elif final == "CONFIRMED_EXCEPTION" and exc:
            match_text, match_class = f"{exc['exception_type']} · {exc['risk_level']} · due {exc['due_at'].replace('T', ' ')}", "fail"
        elif final == "PENDING_MATCH":
            match_text, match_class = f"Waiting until {transfer['match_deadline_at'].replace('T', ' ')}", "wait"
        else:
            match_text, match_class = "No balance update.", "fail"
        steps = [
            ("", "1 · Fintech", f"{transfer['end_user_id']} {verb} ${transfer['amount']:,.2f}", "Customer instruction"),
            (event_class, "2 · Synapse", transfer["event_id"], event_text),
            (bank_class, "3 · Evolve", bank_title, bank_text),
            (match_class, "4 · LedgerWatch", final, match_text),
        ]
        for column, (css, label, title, text) in zip(st.columns(4), steps):
            column.markdown(f'<div class="flow-step {css}"><small>{label}</small><b>{title}</b><p>{text}</p></div>', unsafe_allow_html=True)

st.divider()
st.subheader("EOD batch reconciliation")
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
        st.info("The mocked Synapse batch is available for receipt.")

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

    if batch["status"] == "RECONCILED":
        result_metrics = st.columns(5)
        result_metrics[0].metric("Evolve FBO closing balance", f"${float(batch['bank_balance']):,.2f}")
        result_metrics[1].metric("Evolve-calculated expected", f"${float(batch['expected_total']):,.2f}")
        result_metrics[2].metric("Synapse batch", f"${float(batch['reported_total']):,.2f}")
        result_metrics[3].metric("Evolve minus Synapse", f"${float(batch['aggregate_difference']):,.2f}")
        result_metrics[4].metric("Customer mismatches", batch["customer_mismatch_count"])

        result_rows = rows(
            """SELECT end_user_id, expected_balance, reported_balance, difference, outcome
               FROM customer_reconciliation_results WHERE batch_id=? ORDER BY end_user_id""",
            (batch["batch_id"],),
        )
        result_frame = pd.DataFrame(result_rows).rename(columns={
            "end_user_id": "End user", "expected_balance": "Evolve-calculated expected",
            "reported_balance": "Synapse-reported EOD", "difference": "Expected minus reported",
            "outcome": "Result",
        })
        st.markdown("**Customer-level reconciliation results**")
        st.dataframe(result_frame, width="stretch", hide_index=True, column_config={
            "Evolve-calculated expected": st.column_config.NumberColumn(format="$%.2f"),
            "Synapse-reported EOD": st.column_config.NumberColumn(format="$%.2f"),
            "Expected minus reported": st.column_config.NumberColumn(format="$%.2f"),
        })
        if batch["outcome"] == "MATCHED":
            st.success("Every customer balance and the aggregate FBO balance reconciled.")
        else:
            st.error("The daily control failed. Customer-level results and the aggregate difference are retained, and the exception remains open until resolved or overridden.")

st.divider()
st.subheader("Daily risk checkpoint")
st.caption(
    "Amount, count and overdue rules are enforced only when this checkpoint runs. "
    "A critical feed failure is evaluated immediately; restriction still depends on the automatic-restriction setting."
)
playback_done = int(get_runtime("playback_position", "0")) >= len(scenario_events(current_scenario))
with transaction() as conn:
    rules = checkpoint_rules(conn, get_runtime("simulation_time"))
rule_frame = pd.DataFrame([
    {"Rule": item["rule"], "Current value": item["value"], "Restricts at": item["limit"], "Breached": item["breached"]}
    for item in rules
])
st.dataframe(rule_frame, width="stretch", hide_index=True, column_config={"Breached": st.column_config.CheckboxColumn()})
clock_actions = st.columns([1, 1, 1.3, 1.7])
clock_error = None
for column, (label, seconds) in zip(clock_actions[:2], [("Advance clock 4 hours", 4 * 3600), ("Advance clock 1 day", 86400)]):
    if column.button(label, width="stretch", disabled=not playback_done or stream_state == "RUNNING"):
        try:
            advance_clock(seconds, actor=role)
            st.rerun()
        except ValueError as error:
            clock_error = str(error)
if clock_actions[2].button("Run daily risk checkpoint", type="primary", width="stretch"):
    with transaction() as conn:
        run_daily_checkpoint(conn, get_runtime("simulation_time"), role)
    st.rerun()
with clock_actions[3]:
    if not playback_done:
        st.caption("Finish scenario playback to advance the clock. Deadlines are measured in scenario time.")
    else:
        st.caption("Advancing the clock lets resolution deadlines pass. Heartbeats are assumed to continue, except in the feed-failure scenario.")
if clock_error:
    st.error(clock_error)
last_checkpoint = snapshot["checkpoint"]
if last_checkpoint:
    outcome_text = {
        "NO_RESTRICTION": "no rule breached; the program was not restricted",
        "RESTRICTED": f"rules breached ({last_checkpoint['breached_rules']}); the program was restricted",
        "ALREADY_RESTRICTED": f"rules breached ({last_checkpoint['breached_rules']}); the program was already restricted",
        "BREACHED_AUTO_RESTRICT_OFF": f"rules breached ({last_checkpoint['breached_rules']}) but automatic restriction is turned off",
    }[last_checkpoint["outcome"]]
    message = f"Last checkpoint at {last_checkpoint['run_at'].replace('T', ' ')} by {last_checkpoint['actor']}: {outcome_text}."
    if last_checkpoint["outcome"] == "NO_RESTRICTION":
        st.success(message)
    else:
        st.error(message)

if get_runtime("stream_state") == "RUNNING":
    time.sleep(0.65)
    process_next()
    st.rerun()
