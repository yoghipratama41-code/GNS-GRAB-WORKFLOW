"""Notification text formatting (spec 6, revised: notifications render in the dashboard).
Times are stored in UTC and always shown in MYT."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..engine.state_machine import AlertItem, HEAVY, RAINING, WATCH

MYT = ZoneInfo("Asia/Kuala_Lumpur")

EMOJI = {WATCH: "🌦", RAINING: "🌧", HEAVY: "⛈"}
SOURCE_LABEL = {"open_meteo": "Open-Meteo", "met": "MET Malaysia", "simulated": "simulated"}


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def myt(ts: datetime | None) -> str:
    if ts is None:
        return "--:--"
    return _utc(ts).astimezone(MYT).strftime("%H:%M")


def myt_full(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    return _utc(ts).astimezone(MYT).strftime("%d %b %Y %H:%M")


def _now_line(item: AlertItem) -> str:
    r = item.reading
    src = SOURCE_LABEL.get(r.source, r.source)
    if r.precip_mm_h is None:
        now = "Now: no data"
    else:
        now = f"Now: {r.precip_mm_h:g} mm/h ({src})"
    met = "MET warning: Yes (" + "; ".join(r.warning_labels) + ")" if r.warning_labels else "MET warning: No"
    return f"{now} | {met}"


def format_alert(item: AlertItem) -> str:
    t, r = item.transition, item.reading
    lines = [f"{EMOJI.get(t.to_state, '🌧')} {t.to_state} — {t.zone}", _now_line(item)]
    if r.probability_pct is not None:
        lines.append(f"Next 2h: {r.probability_pct:g}% chance")
    lines.append(f"Since: {myt(t.ts)} MYT")
    return "\n".join(lines)


def format_combined(items: list[AlertItem]) -> str:
    zones = len({i.transition.zone for i in items})
    lines = [f"🌧 MULTI-ZONE ALERT — {zones} zone(s), {len(items)} change(s)"]
    for it in items:
        t, r = it.transition, it.reading
        bits = []
        if r.precip_mm_h is not None:
            bits.append(f"{r.precip_mm_h:g} mm/h")
        if r.probability_pct is not None:
            bits.append(f"{r.probability_pct:g}% next 2h")
        if r.warning_labels:
            bits.append("MET: " + "; ".join(r.warning_labels))
        detail = ", ".join(bits) or t.reason
        lines.append(f"• {EMOJI.get(t.to_state, '🌧')} {t.to_state} — {t.zone}: {detail}")
    return "\n".join(lines)

