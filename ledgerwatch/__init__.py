"""LedgerWatch prototype business logic."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "ledgerwatch.db"
SCENARIO_DIR = DATA_DIR / "scenarios"
BATCH_DIR = DATA_DIR / "batches"

