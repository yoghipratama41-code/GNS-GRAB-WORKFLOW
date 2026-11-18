"""Per-zone state machine + alert coordinator (spec 5).

State chain:  CLEAR -> WATCH -> RAINING -> HEAVY -> CLEARING -> CLEAR

Rules implemented here:
- Alert only on a change *into* WATCH / RAINING / HEAVY.
- Hysteresis: `confirm_readings` consecutive readings are required to enter
  RAINING/HEAVY (a single noisy reading never fires).
- CLEARING after `clearing_minutes` below `clearing_mm_h`, CLEAR after twice that.
- Coordinator: `cooldown_minutes` per zone+state, active-hours filter (only HEAVY
  escapes), snooze filter, and alert merging (3+ changes -> one combined message).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .thresholds import Thresholds

log = logging.getLogger("rain.state")

CLEAR, WATCH, RAINING, HEAVY, CLEARING = "CLEAR", "WATCH", "RAINING", "HEAVY", "CLEARING"
STATES = (CLEAR, WATCH, RAINING, HEAVY, CLEARING)
ALERTABLE = {WATCH, RAINING, HEAVY}


@dataclass
class ZoneReading:
    zone: str
    ts: datetime                      # UTC
    precip_mm_h: float | None = None
    probability_pct: float | None = None
    warning_types: set[str] = field(default_factory=set)   # thunderstorm|rain|heavy_rain
    warning_labels: list[str] = field(default_factory=list)  # ['thunderstorm, Petaling']
    source: str = "open_meteo"
    sampled: bool = True              # False = built from stale data (source outage)


@dataclass
class Transition:
    zone: str
    from_state: str
    to_state: str
    ts: datetime
    reason: str

    @property
    def alertable(self) -> bool:
        return self.from_state != self.to_state and self.to_state in ALERTABLE


@dataclass
class AlertItem:
    """A transition plus the readings snapshot the message is built from."""
    transition: Transition
    reading: ZoneReading


class ZoneFSM:
    def __init__(self, zone: str, th: Thresholds):
        self.zone = zone
        self.th = th
        self.state = CLEAR
        self._wet_streak = 0
        self._watch_streak = 0      # consecutive readings with no watch conditions
        self._dry_since: datetime | None = None   # start of the current dry spell
        self._last_sample: datetime | None = None # last fresh precipitation reading

    # -- helpers ----------------------------------------------------------
    def _watch_reason(self, r: ZoneReading) -> str:
        parts = []
        if (r.probability_pct or 0) >= self.th.watch_probability_pct:
            parts.append(
                f"probability {r.probability_pct:g}% >= {self.th.watch_probability_pct:g}%"
                f" within {self.th.watch_horizon_hours}h"
            )
        if r.warning_labels:
            parts.append("MET warning: " + "; ".join(r.warning_labels))
        return " / ".join(parts) or "watch conditions"

    @staticmethod
    def _fmt(d: timedelta) -> str:
        return f"{d.total_seconds() / 60:.0f} min"

    # -- main -------------------------------------------------------------
    def update(self, r: ZoneReading) -> Transition | None:
        th = self.th
        now = r.ts
        p = float(r.precip_mm_h or 0.0)

        warn_heavy = "heavy_rain" in r.warning_types
        heavy_now = p >= th.heavy_mm_h or warn_heavy
        rain_now = p >= th.raining_mm_h or heavy_now
        watch_now = (r.probability_pct or 0) >= th.watch_probability_pct or bool(r.warning_types)

        # Wet / dry bookkeeping only advances on fresh precipitation data, so a
        # source outage can never be mistaken for "it stopped raining". A dry
        # reading after a data gap also restarts the clock: we were not watching,
        # so we cannot claim the ground stayed dry the whole time.
        has_precip = r.precip_mm_h is not None
        if has_precip and r.sampled:
            gap = (now - self._last_sample) if self._last_sample else timedelta(0)
            if p >= th.clearing_mm_h:
                self._dry_since = None          # actively wet
            elif self._dry_since is None or gap > timedelta(minutes=th.stale_gap_minutes):
                self._dry_since = now           # first dry reading (or first after missing data)
            self._last_sample = now
        dry_for = (now - self._dry_since) if self._dry_since else timedelta(0)
        if r.sampled:
            self._wet_streak = self._wet_streak + 1 if rain_now else 0
            self._watch_streak = 0 if watch_now else self._watch_streak + 1
        confirmed = self._wet_streak >= max(1, th.confirm_readings)
        # A MET heavy-rain warning is authoritative (spec 5) — no reading confirmation needed.
        heavy_confirmed = confirmed or warn_heavy

        confirm_txt = f"confirmed x{self._wet_streak}"
        target, reason = self.state, ""
        s = self.state

        if s == CLEAR:
            if heavy_now and heavy_confirmed:
                target, reason = HEAVY, f"precip {p:g} mm/h >= {th.heavy_mm_h:g} ({confirm_txt})"
            elif rain_now and confirmed:
                target, reason = RAINING, f"precip {p:g} mm/h >= {th.raining_mm_h:g} ({confirm_txt})"
            elif watch_now:
                target, reason = WATCH, self._watch_reason(r)

        elif s == WATCH:
            if heavy_now and heavy_confirmed:
                target, reason = HEAVY, f"precip {p:g} mm/h >= {th.heavy_mm_h:g} ({confirm_txt})"
            elif rain_now and confirmed:
                target, reason = RAINING, f"precip {p:g} mm/h >= {th.raining_mm_h:g} ({confirm_txt})"
            elif self._watch_streak >= th.confirm_readings:
                target, reason = CLEAR, f"no watch conditions for {self._watch_streak} readings"

        elif s == RAINING:
            if heavy_now and heavy_confirmed:
                target, reason = HEAVY, f"precip {p:g} mm/h >= {th.heavy_mm_h:g} ({confirm_txt})"
            elif has_precip and dry_for >= timedelta(minutes=th.clearing_minutes):
                target, reason = CLEARING, f"below {th.clearing_mm_h:g} mm/h for {self._fmt(dry_for)}"

        elif s == HEAVY:
            if has_precip and dry_for >= timedelta(minutes=th.clearing_minutes):
                target, reason = CLEARING, f"below {th.clearing_mm_h:g} mm/h for {self._fmt(dry_for)}"
            elif not heavy_now and rain_now and confirmed:
                target, reason = RAINING, f"precip eased to {p:g} mm/h ({confirm_txt})"

        elif s == CLEARING:
            if heavy_now and heavy_confirmed:
                target, reason = HEAVY, f"precip {p:g} mm/h >= {th.heavy_mm_h:g} ({confirm_txt})"
            elif rain_now and confirmed:
                target, reason = RAINING, f"precip {p:g} mm/h >= {th.raining_mm_h:g} ({confirm_txt})"
            elif watch_now:
                target, reason = WATCH, self._watch_reason(r)
            elif has_precip and dry_for >= 2 * timedelta(minutes=th.clearing_minutes):
                target, reason = CLEAR, f"below {th.clearing_mm_h:g} mm/h for {self._fmt(dry_for)}"

        if target == self.state:
            return None
        t = Transition(self.zone, self.state, target, now, reason)
        log.info("state %s: %s -> %s (%s)", self.zone, self.state, target, reason)
        self.state = target
        return t


class AlertCoordinator:
    """Cooldown + active hours + snooze + merge (spec 5)."""

    def __init__(
        self,
        th: Thresholds,
        send: Callable[[list[AlertItem], bool], None],  # send(items, merged)
        in_active_hours: Callable[[], bool] | None = None,
        is_snoozed: Callable[[str, datetime], bool] | None = None,
    ):
        self.th = th
        self._send = send
        self._in_active_hours = in_active_hours
        self._is_snoozed = is_snoozed
        self._last: dict[tuple[str, str], datetime] = {}
        self._pending: list[tuple[datetime, AlertItem]] = []

    @staticmethod
    def _key(item: AlertItem) -> tuple[str, str]:
        return item.transition.zone, item.transition.to_state

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def submit(self, items: list[AlertItem], now: datetime) -> None:
        th = self.th
        for it in items:
            t = it.transition
            if not t.alertable:
                continue
            key = self._key(it)
            last = self._last.get(key)
            if last and now - last < timedelta(minutes=th.cooldown_minutes):
                log.info("cooldown suppresses %s %s (%s)", t.zone, t.to_state, t.from_state)
                continue
            if self._in_active_hours is not None and not self._in_active_hours() and t.to_state != HEAVY:
                log.info("outside active hours, suppressing %s %s", t.zone, t.to_state)
                continue
            if self._is_snoozed is not None and self._is_snoozed(t.zone, now):
                log.info("snoozed, suppressing %s %s", t.zone, t.to_state)
                continue
            self._pending.append((now, it))
        self.tick(now)

    def tick(self, now: datetime) -> None:
        """Call regularly; fires merged alerts once a batch stops growing, lone
        alerts after a grace period."""
        th = self.th
        if not self._pending:
            return
        if len(self._pending) >= th.merge_min_zones:
            newest = max(ts for ts, _ in self._pending)
            oldest = min(ts for ts, _ in self._pending)
            quiet = (now - newest).total_seconds() >= th.merge_debounce_seconds
            expired = (now - oldest).total_seconds() >= th.merge_window_minutes * 60
            if quiet or expired:
                items = [it for _, it in self._pending]
                self._pending.clear()
                self._fire(items, now, merged=True)
            return  # still gathering: never split a 3+ batch into lone messages
        wait = min(th.merge_grace_seconds, th.merge_window_minutes * 60)
        due = [it for ts, it in self._pending if (now - ts).total_seconds() >= wait]
        if not due:
            return
        self._pending = [(ts, it) for ts, it in self._pending if (now - ts).total_seconds() < wait]
        for it in due:  # fewer than merge_min_zones: one message each (spec)
            self._fire([it], now, merged=False)

    def _fire(self, items: list[AlertItem], now: datetime, merged: bool) -> None:
        for it in items:
            self._last[self._key(it)] = now
        try:
            self._send(items, merged)
        except Exception:
            log.exception("sending alert failed for %s", [i.transition.zone for i in items])
