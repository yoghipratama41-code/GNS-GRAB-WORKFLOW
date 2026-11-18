"""Klang Valley Rain Notifier — live weather dashboard (page 5).

Starts the rain poller once per server process (state machine, SQLite logging,
notification merging), then renders the dashboard UI. The poller runs in the
background of this same Streamlit process — no separate console needed.
"""
import runpy
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1] / "rain-notifier-main"
sys.path.insert(0, str(ROOT))


@st.cache_resource(show_spinner=False)
def start_backend() -> bool:
    """Poll every 10 min / MET warnings every 5 min, for the life of the process."""
    from app.config import load_config
    from app.main import RainApp

    RainApp(load_config()).start_background()
    return True


start_backend()
runpy.run_path(str(ROOT / "dashboard" / "app.py"), run_name="__main__")
