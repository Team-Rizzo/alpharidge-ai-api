"""Alpharidge AI API."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    # The tests run on their own settings, never the deployment's.
    if not os.getenv("API_SKIP_DOTENV"):
        load_dotenv(ROOT / ".env")
except ImportError:
    pass
