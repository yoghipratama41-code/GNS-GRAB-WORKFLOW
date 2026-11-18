"""Alert thresholds. All values are configurable from zones.yaml (spec 5)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    # WATCH: rain probability within the next N hours, or a MET rain/thunderstorm warning.
    watch_probability_pct: float = 60.0
    watch_horizon_hours: int = 2
    # RAINING / HEAVY: current precipitation at any sample point, in mm/h.
    raining_mm_h: float = 0.5
    heavy_mm_h: float = 7.5
    # CLEARING: precipitation below clearing_mm_h for clearing_minutes consecutive minutes.
    clearing_mm_h: float = 0.1
    clearing_minutes: int = 30
    # Hysteresis: consecutive readings required before entering RAINING/HEAVY.
    confirm_readings: int = 2
    # Data-gap safety: a fresh dry reading arriving after a gap longer than this
    # restarts the clearing clock (we cannot claim it stayed dry while missing data).
    stale_gap_minutes: int = 20
    # Cooldown: no re-alert for the same zone + state within this many minutes.
    cooldown_minutes: int = 30
    # Merge: N zone changes inside the window collapse into one combined message.
    merge_window_minutes: int = 5
    merge_min_zones: int = 3
    # How long a lone alert waits for company before being sent on its own.
    merge_grace_seconds: int = 60
    # Once merge_min_zones changes are pending, wait this long for the batch to
    # stop growing before sending one combined message (two polls seconds apart
    # must not produce two back-to-back messages).
    merge_debounce_seconds: int = 10
