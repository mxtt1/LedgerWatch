import streamlit as st

from ledgerwatch.audit import log
from ledgerwatch.config import format_duration
from ledgerwatch.db import get_runtime, get_settings, row, transaction
from ledgerwatch.risk_engine import resume_program_with_override
from ledgerwatch.ui import configure_page, page_header, program_snapshot, restriction_banner


role = configure_page("Configuration")
page_header(
    "Risk controls",
    "Configuration & service controls",
    "Adjust illustrative business-time policies and manage a restricted program through role-gated controls.",
)
snapshot = program_snapshot()
restriction_banner(snapshot)
settings = get_settings()

st.subheader("Threshold policy")
st.caption(
    "These values are illustrative prototype controls—not legal requirements or actual thresholds used by any named institution. "
    "Playback fast-forwards through this business timeline."
)

with st.form("settings_form"):
    time_col, exposure_col = st.columns(2)
    with time_col:
        st.markdown("**Business-time controls**")
        grace_minutes = st.number_input(
            "Match grace period (minutes)",
            min_value=1,
            value=max(1, int(settings["match_grace_seconds"] / 60)),
        )
        heartbeat_warning_minutes = st.number_input(
            "Heartbeat warning timeout (minutes)",
            min_value=1,
            value=max(1, int(settings["heartbeat_warning_seconds"] / 60)),
        )
        heartbeat_critical_minutes = st.number_input(
            "Heartbeat critical timeout (minutes)",
            min_value=int(heartbeat_warning_minutes) + 1,
            value=max(
                int(heartbeat_warning_minutes) + 1,
                int(settings["heartbeat_critical_seconds"] / 60),
            ),
        )
        max_open_age_hours = st.number_input(
            "Maximum open exception age (hours)",
            min_value=1,
            value=max(1, int(settings["max_open_exception_age_seconds"] / 3600)),
            help="An exception that remains OPEN beyond this limit restricts the program. INVESTIGATING exceptions are excluded.",
        )

    with exposure_col:
        st.markdown("**Exposure controls (USD)**")
        warning = st.number_input(
            "Warning exposure",
            min_value=0.0,
            value=float(settings["warning_discrepancy_amount"]),
            step=100.0,
        )
        high = st.number_input(
            "High-risk exposure",
            min_value=float(warning),
            value=max(float(warning), float(settings["high_discrepancy_amount"])),
            step=100.0,
        )
        restriction = st.number_input(
            "Restriction exposure",
            min_value=float(high),
            value=max(float(high), float(settings["restriction_discrepancy_amount"])),
            step=100.0,
        )
        count = st.number_input(
            "Maximum confirmed exception count",
            min_value=1,
            value=int(settings["max_confirmed_exception_count"]),
        )
        tolerance = st.number_input(
            "Aggregate tolerance",
            min_value=0.0,
            value=float(settings["aggregate_tolerance"]),
            step=0.01,
            format="%.2f",
        )
        auto_restrict = st.toggle(
            "Automatic restriction enabled",
            value=bool(snapshot["program"]["auto_restrict_enabled"]),
        )

    if st.form_submit_button("Save control policy", type="primary"):
        updates = {
            "match_grace_seconds": int(grace_minutes) * 60,
            "heartbeat_warning_seconds": int(heartbeat_warning_minutes) * 60,
            "heartbeat_critical_seconds": int(heartbeat_critical_minutes) * 60,
            "max_open_exception_age_seconds": int(max_open_age_hours) * 3600,
            "warning_discrepancy_amount": warning,
            "high_discrepancy_amount": high,
            "restriction_discrepancy_amount": restriction,
            "max_confirmed_exception_count": count,
            "aggregate_tolerance": tolerance,
        }
        changed = {key: value for key, value in updates.items() if str(value) != str(settings[key])}
        now = get_runtime("simulation_time")
        with transaction() as conn:
            for key, value in updates.items():
                conn.execute("UPDATE settings SET value=? WHERE key=?", (str(value), key))
            conn.execute(
                "UPDATE programs SET auto_restrict_enabled=?, updated_at=? WHERE program_id='PROG-001'",
                (int(auto_restrict), now),
            )
            detail = "; ".join(
                f"{key}: {settings[key]} → {value}" for key, value in changed.items()
            ) or "Policy reviewed; no numeric changes"
            detail += (
                f"; auto restriction: {bool(snapshot['program']['auto_restrict_enabled'])} "
                f"→ {auto_restrict}"
            )
            log(conn, "CONFIG_CHANGED", detail, now, actor=role)
        st.success("Control policy saved to SQLite and recorded in the evidence log.")
        st.rerun()

st.divider()
st.subheader("Restriction override & resumption")
program = row("SELECT * FROM programs WHERE program_id='PROG-001'")
if program["status"] != "RESTRICTED":
    st.info("The program is not restricted. Its status is rule-driven and cannot be manually changed here.")
else:
    st.write(program["restriction_reason"])
    with st.form("resume_form"):
        reason = st.text_area(
            "Mandatory decision rationale",
            placeholder="Document compensating controls, reviewer and reason for resumption.",
        )
        resume = st.form_submit_button("Resume simulated service", type="primary")
        if resume:
            if role != "Management":
                st.error("Only Management can resume service after a critical restriction.")
            elif not reason.strip():
                st.error("A decision rationale is required.")
            else:
                now = get_runtime("simulation_time")
                with transaction() as conn:
                    resume_program_with_override(conn, reason, now, role)
                st.rerun()

st.divider()
st.subheader("Current policy summary")
policy_summary = [
    {"Control": "Match grace period", "Configured value": format_duration(settings["match_grace_seconds"])},
    {"Control": "Heartbeat warning", "Configured value": format_duration(settings["heartbeat_warning_seconds"])},
    {"Control": "Heartbeat critical failure", "Configured value": format_duration(settings["heartbeat_critical_seconds"])},
    {"Control": "Maximum open exception age", "Configured value": format_duration(settings["max_open_exception_age_seconds"])},
    {"Control": "Warning exposure", "Configured value": f"${settings['warning_discrepancy_amount']:,.2f}"},
    {"Control": "High-risk exposure", "Configured value": f"${settings['high_discrepancy_amount']:,.2f}"},
    {"Control": "Restriction exposure", "Configured value": f"${settings['restriction_discrepancy_amount']:,.2f}"},
]
st.dataframe(policy_summary, width="stretch", hide_index=True)
