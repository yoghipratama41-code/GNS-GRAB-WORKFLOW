import runpy
from pathlib import Path

APP = (
    Path(__file__).resolve().parents[1]
    / "green-table-automation-main"
    / "green-table-automation-main"
    / "Full code automation greentable.py"
)
runpy.run_path(str(APP), run_name="__main__")
