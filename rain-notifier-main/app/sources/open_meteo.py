"""Open-Meteo nowcast adapter (spec 4B). Free, no API key, batched coordinates.

Verified against the live API during the build:
- `minutely_15` is interpolated from hourly values in Malaysia (every non-zero run has
  exactly 4 identical slots), so per spec we rely on `current` + `hourly` only.
- `current.precipitation` is mm per 15 minutes (it equals `minutely_15` at the same
  timestamp), so the mm/h rate used by the thresholds is value * 4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .base import Observation, SourceHealth

log = logging.getLogger("rain.open_meteo")

URL = "https://api.open-meteo.com/v1/forecast"
MYT = ZoneInfo("Asia/Kuala_Lumpur")


def _parse_local(s: str | None) -> datetime:
    """Open-Meteo returns local wall-clock times (timezone=Asia/Kuala_Lumpur) -> UTC."""
    if s:
        try:
            return datetime.fromisoformat(s).replace(tzinfo=MYT).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _max_prob_next_horizon(hourly: dict, now_utc: datetime, horizon_hours: int) -> float | None:
    """Max precipitation_probability over the hourly slots covering [now, now+horizon]."""
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    if not times or not probs:
        return None
    now_local = now_utc.astimezone(MYT)
    idx = 0
    for i, t in enumerate(times):
        slot = datetime.fromisoformat(t).replace(tzinfo=MYT)
        if slot <= now_local:
            idx = i
        else:
            break
    window = [p for p in probs[idx : idx + horizon_hours + 2] if p is not None]
    return float(max(window)) if window else None


def merge_hourly(per_point: list[dict]) -> dict:
    """Collapse a zone's sample points into one hourly series (max per slot)."""
    agg: dict[str, list[float | None]] = {}
    for h in per_point:
        times = h.get("time") or []
        series = (
            (0, h.get("precipitation") or []),
            (1, h.get("precipitation_probability") or []),
        )
        for idx, values in series:
            for i, t in enumerate(times):
                if i >= len(values) or values[i] is None:
                    continue
                slot = agg.setdefault(t, [None, None])
                v = float(values[i])
                slot[idx] = v if slot[idx] is None else max(slot[idx], v)
    ordered = sorted(agg)
    return {
        "time": ordered,
        "precipitation": [agg[t][0] for t in ordered],
        "precipitation_probability": [agg[t][1] for t in ordered],
    }


def next_rain_window(
    hourly: dict, now_utc: datetime, rain_mm_h: float = 0.5, horizon_hours: int = 12
) -> tuple[str, float | None]:
    """First run of hourly slots at/above `rain_mm_h` -> ('15:00-17:00', peak) or
    ('none in next 12h', None). Includes the current hour if it is already wet."""
    times = hourly.get("time") or []
    precips = hourly.get("precipitation") or []
    now_local = now_utc.astimezone(MYT)
    first_slot = now_local.replace(minute=0, second=0, microsecond=0)
    horizon = now_local + timedelta(hours=horizon_hours)

    run: list[tuple[datetime, float]] = []
    for t, p in zip(times, precips):
        if p is None:
            continue
        slot = datetime.fromisoformat(t).replace(tzinfo=MYT)
        if slot < first_slot or slot >= horizon:
            continue
        if float(p) >= rain_mm_h:
            if run and slot - run[-1][0] > timedelta(hours=1, minutes=1):
                break                      # first run only; a gap ends the window
            run.append((slot, float(p)))
        elif run:
            break                          # dry slot ends the window
    if not run:
        return f"none in next {horizon_hours}h", None
    start = run[0][0]
    end = run[-1][0] + timedelta(hours=1)
    return f"{start:%H:%M}-{end:%H:%M}", max(p for _, p in run)


class OpenMeteoSource:
    name = "open_meteo"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.health = SourceHealth(name=self.name)

    def fetch(self, zones, rain_mm_h: float = 0.5) -> list[Observation]:
        """One batched request for every sample point of every zone.

        Returns the per-point nowcast observations plus one `rain_window`
        observation per zone (when rain is expected next) for the dashboard.
        """
        coords = [(z.name, p) for z in zones for p in z.points]
        if not coords:
            return []
        params = {
            "latitude": ",".join(f"{p.lat:g}" for _, p in coords),
            "longitude": ",".join(f"{p.lon:g}" for _, p in coords),
            "current": "precipitation,rain",
            "hourly": "precipitation,precipitation_probability",
            "timezone": "Asia/Kuala_Lumpur",
        }
        try:
            r = httpx.get(URL, params=params, timeout=self.timeout, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # source must fail without killing the scheduler
            self.health.fail(f"{type(exc).__name__}: {exc}")
            raise

        if isinstance(data, dict):
            data = [data]  # single coordinate comes back as an object, not a list
        if len(data) != len(coords):
            msg = f"expected {len(coords)} results, got {len(data)}"
            self.health.fail(msg)
            raise RuntimeError(msg)

        out: list[Observation] = []
        zone_hourly: dict[str, list[dict]] = {}
        for (zone_name, point), item in zip(coords, data):
            current = item.get("current") or {}
            hourly = item.get("hourly") or {}
            precip = None
            if current.get("precipitation") is not None:
                precip = round(float(current["precipitation"]) * 4.0, 2)  # mm/15min -> mm/h
            ts = _parse_local(current.get("time"))
            out.append(
                Observation(
                    zone=zone_name,
                    point=point.name,
                    source=self.name,
                    ts=ts,
                    precip_mm_h=precip,
                    probability_pct=_max_prob_next_horizon(hourly, ts, 2),
                )
            )
            zone_hourly.setdefault(zone_name, []).append(hourly)

        now = datetime.now(timezone.utc)
        for zone_name, series in zone_hourly.items():
            merged = merge_hourly(series)
            label, peak = next_rain_window(merged, now, rain_mm_h)
            out.append(
                Observation(
                    zone=zone_name,
                    point="next",
                    source=self.name,
                    ts=now,
                    kind="rain_window",
                    text=label,
                    precip_mm_h=peak,
                    probability_pct=_max_prob_next_horizon(merged, now, 12),
                )
            )

        self.health.touch()
        log.debug("fetched %d point observations + %d rain windows", len(out) - len(zone_hourly), len(zone_hourly))
        return out
