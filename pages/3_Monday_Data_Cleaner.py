import runpy
from pathlib import Path

APP = (
    Path(__file__).resolve().parents[1]
    / "monday-automation-pipeline-clean-main"
    / "monday-automation-pipeline-clean-main"
    / "streamlit_app.py"
)
runpy.run_path(str(APP), run_name="__main__")
