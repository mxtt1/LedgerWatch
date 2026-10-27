"""Shared Streamlit presentation helpers."""

from datetime import datetime

import pandas as pd
import streamlit as st

from . import DB_PATH
from .config import format_duration
from .db import get_runtime, get_settings, row, rows
from .seed import ensure_database


def configure_page(title: str, icon: str = "◉") -> str:
    st.set_page_config(page_title=f"{title} · LedgerWatch", page_icon="◉", layout="wide", initial_sidebar_state="expanded")
    ensure_database()
    inject_css()
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-mark">LW</div><div><b>LedgerWatch</b><small>Bank assurance console</small></div></div>', unsafe_allow_html=True)
        st.caption("SYNTHETIC DATA · PROTOTYPE")
        role = st.selectbox("Acting role", ["Operations", "Risk / Compliance", "Management"], key="active_role")
        st.divider()
        program = row("SELECT program_name, status FROM programs WHERE program_id='PROG-001'")
        st.caption("MONITORED PROGRAM")
        st.markdown(f"**{program['program_name']}**")
        st.markdown(status_badge(program["status"]), unsafe_allow_html=True)
        st.divider()
        st.caption("All names, accounts and values are synthetic. No banking rails are connected.")
    return role


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root { --navy:#102a43; --ink:#172b3a; --teal:#087f8c; --paper:#f4f7f9; --line:#dce5ea; }
        .stApp { background: #f4f7f9; color: #172b3a; }
        [data-testid="stSidebar"] { background: #102a43; color:#eaf2f6; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
        [data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] span { color:#eaf2f6 !important; }
        [data-testid="stSidebar"] .stSelectbox label { color:#9fb3c8 !important; }
        .brand { display:flex; align-items:center; gap:.75rem; margin:.4rem 0 1.2rem; }
        .brand-mark { width:38px; height:38px; display:grid; place-items:center; background:#2ec4b6; color:#102a43 !important; font-weight:900; border-radius:10px; }
        .brand b { font-size:1.2rem; color:white !important; }
        .brand small { display:block; color:#9fb3c8 !important; font-size:.72rem; }
        .eyebrow { color:#087f8c; text-transform:uppercase; letter-spacing:.13em; font-weight:800; font-size:.72rem; margin-bottom:.25rem; }
        h1 { color:#102a43 !important; letter-spacing:-.035em !important; }
        h2, h3 { color:#243b53 !important; }
        [data-testid="stMetric"] { background:white; border:1px solid #dce5ea; border-radius:12px; padding:1rem 1.05rem; box-shadow:0 2px 9px rgba(16,42,67,.04); }
        [data-testid="stMetricLabel"] { text-transform:uppercase; letter-spacing:.06em; font-size:.72rem; color:#627d98; }
        .status-badge { display:inline-block; padding:.28rem .62rem; border-radius:999px; font-size:.72rem; font-weight:800; letter-spacing:.05em; }
        .ACTIVE,.HEALTHY,.MATCHED,.RESOLVED { background:#d9f7ee; color:#086f59; }
        .WARNING,.STALE,.PENDING_MATCH,.INVESTIGATING { background:#fff0c2; color:#8c5b00; }
        .HIGH_RISK { background:#ffe1c7; color:#a23e00; }
        .RESTRICTED,.CRITICAL,.CONFIRMED_EXCEPTION,.FEED_FAILURE,.BLOCKED_BY_RESTRICTION { background:#ffe0e3; color:#b42335; }
        .AWAITING_HEARTBEAT,.OPEN { background:#e7edf3; color:#486581; }
        .OVERRIDDEN { background:#ece7f7; color:#5b3a86; }
        .restriction { background:#7f1d2d; color:white; border-radius:12px; padding:1rem 1.2rem; margin:.4rem 0 1.2rem; border-left:6px solid #ffb4bc; }
        .restriction strong { color:white; display:block; margin-bottom:.2rem; }
        .info-card { background:white; border:1px solid #dce5ea; border-radius:12px; padding:1rem 1.1rem; min-height:105px; }
        .info-card small { color:#627d98; text-transform:uppercase; letter-spacing:.06em; }
        .info-card b { display:block; color:#102a43; font-size:1.05rem; margin-top:.35rem; }
        div[data-testid="stDataFrame"] { border:1px solid #dce5ea; border-radius:10px; overflow:hidden; }
        [data-testid="stWidgetLabel"] p { color:#243b53 !important; font-weight:650; }
        [data-baseweb="select"] > div { background:#ffffff !important; border-color:#9fb3c8 !important; }
        [data-baseweb="select"] span,
        [data-baseweb="select"] input { color:#172b3a !important; -webkit-text-fill-color:#172b3a !important; }
        [data-baseweb="input"] > div,
        [data-baseweb="textarea"] > div { background:#ffffff !important; }
        [data-baseweb="input"] input,
        [data-baseweb="textarea"] textarea { color:#172b3a !important; -webkit-text-fill-color:#172b3a !important; }
        .stButton > button,
        [data-testid="stFormSubmitButton"] > button { background:#ffffff !important; color:#102a43 !important; border:1px solid #7890a4 !important; border-radius:8px; font-weight:750; }
        .stButton > button p,
        .stButton > button span,
        [data-testid="stFormSubmitButton"] > button p,
        [data-testid="stFormSubmitButton"] > button span { color:inherit !important; }
        .stButton > button:hover,
        [data-testid="stFormSubmitButton"] > button:hover { background:#e8f5f6 !important; color:#065f68 !important; border-color:#087f8c !important; }
        button[data-testid="stBaseButton-primary"],
        [data-testid="stFormSubmitButton"] button[kind="primary"] { background:#087f8c !important; color:#ffffff !important; border-color:#087f8c !important; }
        button[data-testid="stBaseButton-primary"] p,
        button[data-testid="stBaseButton-primary"] span { color:#ffffff !important; }
        button[data-testid="stBaseButton-primary"]:hover { background:#066a75 !important; color:#ffffff !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(eyebrow: str, title: str, description: str) -> None:
    st.markdown(f'<div class="eyebrow">{eyebrow}</div>', unsafe_allow_html=True)
    st.title(title)
    st.caption(description)


def status_badge(status: str) -> str:
    safe = (status or "UNKNOWN").replace(" ", "_")
    return f'<span class="status-badge {safe}">{status or "UNKNOWN"}</span>'


def program_snapshot() -> dict:
    program = row("SELECT * FROM programs WHERE program_id='PROG-001'")
    account = row("SELECT * FROM bank_accounts WHERE program_id='PROG-001'")
    settings = get_settings()
    sim_time = get_runtime("simulation_time")
    event_pending = row("SELECT COUNT(*) AS n FROM processed_events WHERE result_status='PENDING_MATCH'")["n"]
    bank_pending = row(
        """SELECT COUNT(*) AS n FROM bank_transactions bank
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
    )["n"]
    pending = event_pending + bank_pending
    confirmed = row("SELECT COUNT(*) AS n FROM exceptions WHERE status IN ('OPEN','INVESTIGATING')")["n"]
    blocked = row("SELECT COUNT(*) AS n FROM processed_events WHERE result_status='BLOCKED_BY_RESTRICTION'")["n"]
    expected_total = row(
        "SELECT COALESCE(SUM(expected_balance),0) AS total FROM expected_customer_balances WHERE program_id=?",
        (program["program_id"],),
    )["total"]
    batch = row(
        "SELECT * FROM reconciliation_batches WHERE program_id=? ORDER BY rowid DESC LIMIT 1",
        (program["program_id"],),
    )
    transaction_exposure = row("SELECT COALESCE(SUM(ABS(amount_difference)),0) AS total FROM exceptions WHERE exception_type IN ('MISSING_BANK_TRANSACTION','MISSING_INTERMEDIARY_EVENT','AMOUNT_MISMATCH','DIRECTION_MISMATCH','ACCOUNT_MISMATCH') AND status IN ('OPEN','INVESTIGATING')")["total"]
    aggregate_exposure = row("SELECT COALESCE(MAX(ABS(amount_difference)),0) AS total FROM exceptions WHERE exception_type='AGGREGATE_BALANCE_MISMATCH' AND status IN ('OPEN','INVESTIGATING')")["total"]
    exposure = max(float(transaction_exposure or 0), float(aggregate_exposure or 0))
    oldest = row("SELECT created_at FROM exceptions WHERE status IN ('OPEN','INVESTIGATING') ORDER BY created_at LIMIT 1")
    if not program["last_heartbeat_at"]:
        health, heartbeat_age = "AWAITING_HEARTBEAT", None
    else:
        heartbeat_age = (datetime.fromisoformat(sim_time) - datetime.fromisoformat(program["last_heartbeat_at"])).total_seconds()
        if heartbeat_age >= settings["heartbeat_critical_seconds"]:
            health = "CRITICAL"
        elif heartbeat_age >= settings["heartbeat_warning_seconds"]:
            health = "STALE"
        else:
            health = "HEALTHY"
    return {
        "program": program, "account": account, "settings": settings, "sim_time": sim_time,
        "pending": pending, "confirmed": confirmed, "blocked": blocked,
        "expected_total": float(expected_total or 0), "batch": batch,
        "reported_total": float(batch["reported_total"]) if batch else None,
        "ledger_difference": float(batch["aggregate_difference"]) if batch and batch["aggregate_difference"] is not None else None,
        "exposure": float(exposure or 0),
        "oldest": oldest["created_at"] if oldest else None, "feed_health": health, "heartbeat_age": heartbeat_age,
    }


def restriction_banner(snapshot: dict) -> None:
    program = snapshot["program"]
    if program["status"] == "RESTRICTED":
        st.markdown(
            f'<div class="restriction"><strong>PROGRAM PROCESSING RESTRICTED</strong>{program["restriction_reason"] or "A critical control threshold was breached."}</div>',
            unsafe_allow_html=True,
        )


def audit_table(limit: int = 100):
    data = rows("SELECT created_at, event_type, actor, details FROM audit_log ORDER BY rowid DESC LIMIT ?", (limit,))
    if not data:
        st.info("No evidence entries yet.")
        return
    frame = pd.DataFrame(data).rename(columns={"created_at": "Time", "event_type": "Event", "actor": "Actor", "details": "Evidence"})
    st.dataframe(frame, width="stretch", hide_index=True, height=min(420, 38 + len(frame) * 35))


def format_age(timestamp: str | None, now: str) -> str:
    if not timestamp:
        return "—"
    seconds = max(0, int((datetime.fromisoformat(now) - datetime.fromisoformat(timestamp)).total_seconds()))
    return format_duration(seconds)

