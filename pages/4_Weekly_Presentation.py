import runpy
from pathlib import Path

APP = (
    Path(__file__).resolve().parents[1]
    / "weekly-presentation-main"
    / "weekly-presentation-main"
    / "app.py"
)
runpy.run_path(str(APP), run_name="__main__")
