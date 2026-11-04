"""Central rules and default prototype thresholds."""

DEFAULT_SETTINGS = {
    "match_grace_seconds": (300, "int", "Five-minute business-time grace period before confirmation"),
    "heartbeat_warning_seconds": (120, "int", "Two minutes without a heartbeat before feed warning"),
    "heartbeat_critical_seconds": (300, "int", "Five minutes without a heartbeat before critical feed failure"),
    "warning_discrepancy_amount": (1000.0, "float", "Cumulative unresolved exposure warning"),
    "high_discrepancy_amount": (5000.0, "float", "High-risk escalation threshold"),
    "restriction_discrepancy_amount": (10000.0, "float", "Daily checkpoint exposure restriction threshold"),
    "max_confirmed_exception_count": (10, "int", "Daily checkpoint count restriction threshold"),
    "risk_medium_amount": (1000.0, "float", "Single-exception amount from which an exception is Medium risk"),
    "risk_critical_amount": (5000.0, "float", "Single-exception amount from which an exception is Critical risk"),
    "deadline_low_seconds": (259200, "int", "Time allowed to resolve a Low risk exception"),
    "deadline_medium_seconds": (86400, "int", "Time allowed to resolve a Medium risk exception"),
    "deadline_critical_seconds": (14400, "int", "Time allowed to resolve a Critical risk exception"),
    "aggregate_tolerance": (0.01, "float", "Allowed difference in formal reconciliation"),
    "formal_reconciliation_interval": ("Manual / EOD simulation", "string", "Prototype schedule"),
}

FINANCIAL_EXCEPTION_TYPES = {
    "MISSING_BANK_TRANSACTION",
    "MISSING_INTERMEDIARY_EVENT",
    "AMOUNT_MISMATCH",
    "DIRECTION_MISMATCH",
    "ACCOUNT_MISMATCH",
    "AGGREGATE_BALANCE_MISMATCH",
}

UNRESOLVED_STATUSES = ("OPEN", "INVESTIGATING")

RISK_LEVELS = ("Critical", "Medium", "Low")
# Feed exceptions carry no amount, so their level comes from the exception type.
FEED_RISK_LEVELS = {"STALE_FEED": "Medium", "FEED_FAILURE": "Critical"}


def risk_level_for(exception_type: str, amount: float, settings: dict) -> str:
    """Level one exception by its own amount, using the configured bands (lower bound inclusive)."""
    if exception_type in FEED_RISK_LEVELS:
        return FEED_RISK_LEVELS[exception_type]
    amount = abs(float(amount))
    if amount >= float(settings["risk_critical_amount"]):
        return "Critical"
    if amount >= float(settings["risk_medium_amount"]):
        return "Medium"
    return "Low"


def risk_band_text(settings: dict) -> str:
    """Band description for Markdown captions; dollar signs are escaped so Streamlit does not render LaTeX."""
    medium, critical = float(settings["risk_medium_amount"]), float(settings["risk_critical_amount"])
    return f"Low under \\${medium:,.0f} · Medium from \\${medium:,.0f} · Critical from \\${critical:,.0f}"


def deadline_seconds_for(risk_level: str, settings: dict) -> int:
    return int(settings[f"deadline_{risk_level.lower()}_seconds"])


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds == 0:
        return "0 seconds"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day" if days == 1 else f"{days} days"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def status_for_exposure(exposure: float, settings: dict) -> str:
    if exposure >= float(settings["restriction_discrepancy_amount"]):
        return "RESTRICTED"
    if exposure >= float(settings["high_discrepancy_amount"]):
        return "HIGH_RISK"
    if exposure >= float(settings["warning_discrepancy_amount"]):
        return "WARNING"
    return "ACTIVE"


def feed_health(age_seconds: float | None, settings: dict) -> str:
    if age_seconds is None:
        return "AWAITING_HEARTBEAT"
    if age_seconds >= float(settings["heartbeat_critical_seconds"]):
        return "CRITICAL"
    if age_seconds >= float(settings["heartbeat_warning_seconds"]):
        return "STALE"
    return "HEALTHY"

