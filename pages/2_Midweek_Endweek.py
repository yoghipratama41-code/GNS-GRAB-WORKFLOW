import runpy
from pathlib import Path

APP = (
    Path(__file__).resolve().parents[1]
    / "Midweek-Endweek-Qualitative-V1-main"
    / "Midweek-Endweek-Qualitative-V1-main"
    / "app_gns_censor_to_endweek.py"
)
runpy.run_path(str(APP), run_name="__main__")
