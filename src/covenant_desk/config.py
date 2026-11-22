"""Project settings, read from environment variables so nothing is hardcoded."""

from __future__ import annotations

import os
from pathlib import Path

# Project root = two levels above this file (src/covenant_desk/config.py -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Where downloaded filings are cached. Override with COVENANT_DESK_DATA.
DATA_DIR = Path(os.environ.get("COVENANT_DESK_DATA", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"

# SEC asks every automated client to identify itself with a name and contact email:
# https://www.sec.gov/os/accessing-edgar-data
# Set SEC_USER_AGENT="Your Name your@email.com" in your shell or .env file.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "covenant-desk research-script contact@example.com")
