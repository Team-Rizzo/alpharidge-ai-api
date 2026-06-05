import os
import sys

# Make the API package importable (main.py, utils/, etc. live at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disabling auth keeps endpoint tests from needing a live metagraph.
os.environ.setdefault("AUTH_ENABLED", "false")
