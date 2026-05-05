"""Shared configuration constants for RennMarkt and RennAuktion."""
from pathlib import Path

# Project root (one level above this file)
PROJECT_ROOT = Path(__file__).parent.parent

DATA_DIR  = PROJECT_ROOT / "data"
DOCS_DIR  = PROJECT_ROOT / "docs"
LOGS_DIR  = PROJECT_ROOT / "logs"

DB_PATH   = DATA_DIR / "inventory.db"

# Year range hard rule — owner decision required to change YEAR_MAX
YEAR_MIN = 1984
YEAR_MAX = 2024  # HARD RULE: do not change until Jan 1 2027

# Sources
AUCTION_SOURCES = frozenset({
    "bring a trailer",
    "cars and bids",
    "pcarmarket",
})

RETAIL_SOURCES = frozenset({
    "dupont registry",
    "ebay motors",
    "cars.com",
    "autotrader",
    "pca mart",
    "rennlist",
    "built for backroads",
})
