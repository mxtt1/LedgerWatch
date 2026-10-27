"""Central rules and default prototype thresholds."""

DEFAULT_SETTINGS = {
    "match_grace_seconds": (300, "int", "Five-minute business-time grace period before confirmation"),
    "heartbeat_warning_seconds": (120, "int", "Two minutes without a heartbeat before feed warning"),
    "heartbeat_critical_seconds": (300, "int", "Five minutes without a heartbeat before critical feed failure"),
    "warning_discrepancy_amount": (1000.0, "float", "Cumulative unresolved exposure warning"),
    "high_discrepancy_amount": (5000.0, "float", "High-risk escalation threshold"),
    "restriction_discrepancy_amount": (10000.0, "float", "Automatic service restriction threshold"),
    "max_open_exception_age_seconds": (86400, "int", "Maximum age of an uninvestigated open exception"),
    "max_confirmed_exception_count": (10, "int", "Count-based restriction trigger"),
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

