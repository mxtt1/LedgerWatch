import streamlit as st

from ledgerwatch.audit import log
from ledgerwatch.config import RISK_LEVELS, format_duration, risk_band_text
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
        st.markdown("**Resolution deadlines by risk level (hours)**")
        st.caption(
            "Each deadline is fixed when the exception is created and keeps running while it is OPEN or INVESTIGATING. "
            "A missed deadline restricts the program at the next daily checkpoint."
        )
        deadline_hours = {
            level: st.number_input(
                f"{level} risk",
                min_value=1,
                value=max(1, int(settings[f"deadline_{level.lower()}_seconds"] / 3600)),
                key=f"deadline_{level}",
            )
            for level in RISK_LEVELS
        }

    with exposure_col:
        st.markdown("**Risk level bands, per exception (USD)**")
        st.caption(
            f"Levels one exception by its own amount. Currently {risk_band_text(settings)}. "
            "Feed exceptions are levelled by type. New bands apply to exceptions created after saving."
        )
        medium_from = st.number_input(
            "Medium risk from",
            min_value=0.01,
            value=float(settings["risk_medium_amount"]),
            step=100.0,
        )
        critical_from = st.number_input(
            "Critical risk from",
            min_value=float(medium_from) + 0.01,
            value=max(float(medium_from) + 0.01, float(settings["risk_critical_amount"])),
            step=100.0,
        )

        st.markdown("**Exposure controls, whole program (USD)**")
        st.caption(
            "Uses the total of all unresolved exceptions, not any single one. Warning and high-risk change the live program label. "
            "Restriction exposure and exception count are only applied at the daily checkpoint."
        )
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
            "Daily checkpoint: restriction exposure",
            min_value=float(high),
            value=max(float(high), float(settings["restriction_discrepancy_amount"])),
            step=100.0,
        )
        count = st.number_input(
            "Daily checkpoint: maximum unresolved exception count",
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
            **{f"deadline_{level.lower()}_seconds": int(hours) * 3600 for level, hours in deadline_hours.items()},
            "risk_medium_amount": float(medium_from),
            "risk_critical_amount": float(critical_from),
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
    {"Control": "Risk level · Medium from (per exception)", "Configured value": f"${settings['risk_medium_amount']:,.2f}"},
    {"Control": "Risk level · Critical from (per exception)", "Configured value": f"${settings['risk_critical_amount']:,.2f}"},
    *[
        {"Control": f"Resolution deadline · {level} risk", "Configured value": format_duration(settings[f"deadline_{level.lower()}_seconds"])}
        for level in RISK_LEVELS
    ],
    {"Control": "Warning exposure (live label)", "Configured value": f"${settings['warning_discrepancy_amount']:,.2f}"},
    {"Control": "High-risk exposure (live label)", "Configured value": f"${settings['high_discrepancy_amount']:,.2f}"},
    {"Control": "Daily checkpoint: missed resolution deadline", "Configured value": "Any unresolved exception past its deadline"},
    {"Control": "Daily checkpoint: restriction exposure", "Configured value": f"${settings['restriction_discrepancy_amount']:,.2f}"},
    {"Control": "Daily checkpoint: unresolved exception count", "Configured value": str(settings["max_confirmed_exception_count"])},
    {"Control": "Immediate restriction", "Configured value": "Critical feed failure only"},
]
st.dataframe(policy_summary, width="stretch", hide_index=True)
