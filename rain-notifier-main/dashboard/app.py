"""Dashboard — the app's UI (spec 8, revised: notifications live here).

    python -m app.main          # starts the scheduler AND this dashboard
    streamlit run dashboard/app.py   # dashboard alone (needs the scheduler running)

Layout principle: the top banner answers "do I need to do anything?" — everything
below is detail. Every section carries a one-line explanation of what it is.
Reads only from SQLite.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.db.repo import Repo  # noqa: E402
from app.notify.formatter import myt, myt_full  # noqa: E402

st.set_page_config(page_title="Klang Valley Rain Notifier", layout="wide")

STATE_EMOJI = {
    "CLEAR": "🟣", "WATCH": "🟠", "RAINING": "🔴", "HEAVY": "🔻", "CLEARING": "🔵",
}
STATE_MEANING = {
    "CLEAR": "No rain expected right now — nothing to do.",
    "WATCH": "Rain is likely within the next 2 hours, but nothing is falling yet — keep an eye on it.",
    "RAINING": "Rain is falling in this zone right now.",
    "HEAVY": "Heavy rain (≥ 7.5 mm/h) — highest risk of ponding and flash floods.",
    "CLEARING": "Rain has stopped; the zone is drying up.",
}
STATE_RANK = {"HEAVY": 0, "RAINING": 1, "WATCH": 2, "CLEARING": 3, "CLEAR": 4}
FALLBACK_COLORS = {
    "CLEAR": "#7c3aed", "WATCH": "#f59e0b", "RAINING": "#ef4444",
    "HEAVY": "#7f1d1d", "CLEARING": "#0ea5e9",
}


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fresh(ts, max_age_s: int) -> bool:
    dt = _parse_ts(ts)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_s


@st.cache_data(ttl=30)
def load():
    cfg = load_config()
    repo = Repo(cfg.db_path)
    states = repo.latest_states()
    for z in cfg.zones:  # zones with no event yet are CLEAR
        states.setdefault(z.name, {"state": "CLEAR", "ts": None, "reason": None})
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    obs = pd.DataFrame([dict(r) for r in repo.observations_since(since)])
    if not obs.empty:
        obs["ts"] = pd.to_datetime(obs["ts"], utc=True).dt.tz_convert("Asia/Kuala_Lumpur")
        obs["mm_h"] = pd.to_numeric(obs["precip_mm_h"], errors="coerce")
        obs["prob"] = pd.to_numeric(obs["probability_pct"], errors="coerce")
    return {
        "cfg": cfg,
        "repo": repo,
        "states": states,
        "obs": obs,
        "windows": repo.latest_rain_windows(),
        "forecasts": repo.latest_forecasts(),
        "warnings": repo.latest_zone_warnings(),
        "health": repo.source_health(),
        "alerts": repo.recent_alerts(limit=15),
    }


data = load()
cfg, repo, states = data["cfg"], data["repo"], data["states"]
obs, windows, forecasts, warnings = data["obs"], data["windows"], data["forecasts"], data["warnings"]
colors = {**FALLBACK_COLORS, **cfg.state_colors}
nowcast_age_s = cfg.poll_open_meteo * 3

nowcast = obs[obs["kind"] == "nowcast"] if not obs.empty else obs
current = pd.DataFrame(columns=["zone", "precip", "prob"])
if not nowcast.empty:
    latest = nowcast[nowcast["ts"] >= nowcast["ts"].max() - pd.Timedelta(seconds=60)]
    current = latest.groupby("zone", as_index=False).agg(precip=("mm_h", "max"), prob=("prob", "max"))
cur_by_zone = {r["zone"]: r for r in current.to_dict("records")}

st.title("🌧️ Klang Valley Rain Notifier")
data_age = myt(nowcast["ts"].max().to_pydatetime()) if not nowcast.empty else "--:--"
st.caption(
    f"Updated {myt_full(datetime.now(timezone.utc))} MYT  •  "
    f"observations as of {data_age} MYT  •  MET Malaysia → Open-Meteo  •  {cfg.db_path.name}"
)

# ------------------------------------------------- 1. attention banner (top)
by_state: dict[str, list[str]] = {}
for name, row in states.items():
    by_state.setdefault(row.get("state", "CLEAR"), []).append(name)

heavy = sorted(by_state.get("HEAVY", []))
raining = sorted(by_state.get("RAINING", []))
watching = sorted(by_state.get("WATCH", []), key=lambda n: states[n].get("ts") or "")
clearing = sorted(by_state.get("CLEARING", []))

if heavy or raining:
    parts = []
    if heavy:
        parts.append(f"**HEAVY — {', '.join(heavy)}** (heavy rain now)")
    if raining:
        parts.append(f"**RAINING — {', '.join(raining)}**")
    st.error("⛈️ Act now: " + "  |  ".join(parts))
    if watching:
        st.caption(f"Also expected soon (WATCH): {', '.join(watching)}")
elif watching:
    st.warning(
        f"🟠 Rain likely within 2 hours in **{len(watching)} zone(s)**: {', '.join(watching)} "
        "— nothing falling yet, no action needed"
    )
elif clearing:
    st.info(f"🔵 Rain just stopped in: {', '.join(clearing)} — drying up")
else:
    st.success(f"✅ All {len(cfg.zones)} zones clear — no rain expected right now")

with st.expander("ℹ️ What should I look at?"):
    st.markdown(
        """
1. **The banner above** — the only part that needs a decision. It names the zones
   where rain is falling or coming soon. If it's green, you're done.
2. **Zone tiles** — one per zone, colour = what is happening *right now*.
   Each tile also shows *since when* and *when rain is expected next*.
3. **Zone detail** — pick a zone for the full numbers: current intensity,
   chance of rain, official MET warning, and why the state changed.
4. **Notifications** — automatic log of every state change (nothing to do here;
   it exists so you can look back at what happened and when).

**States:** 🟣 CLEAR (nothing expected) → 🟠 WATCH (rain likely ≤ 2h) →
🔴 RAINING (falling now) → 🔻 HEAVY (≥ 7.5 mm/h) → 🔵 CLEARING (stopped, drying) → 🟣 CLEAR.

**Sources:** MET Malaysia = official warnings + daily forecast (authoritative).
Open-Meteo = current rain and the next-12h window (updated every 10 min).

**Times:** always MYT. A zone stays silent for 30 min after its last notification,
and 3+ zones changing together are reported as one combined entry.
    """
    )

# ---------------------------------------------------------------- zone tiles
st.subheader("Zone status")
st.caption(
    "All 10 zones, colour-coded by current state. Tiles needing attention are "
    "listed in the banner above — the rest is background."
)
cols = st.columns(5)
for i, zone in enumerate(sorted(cfg.zones, key=lambda z: STATE_RANK.get(
        states.get(z.name, {}).get("state", "CLEAR"), 9))):
    row = states.get(zone.name, {"state": "CLEAR", "ts": None})
    state = row.get("state", "CLEAR")
    color = colors.get(state, "#64748b")
    warn = warnings.get(zone.name)
    warn_line = f"⚠️ {warn['text']}" if warn and _fresh(warn["ts"], nowcast_age_s) else ""
    with cols[i % 5]:
        st.markdown(
            f"""
            <div style="
                background:{color}; border-radius:10px; padding:14px 12px;
                color:white; text-align:center; margin-bottom:8px;
                {'box-shadow:0 0 0 3px #fff, 0 0 0 6px ' + color + ';' if state in ('HEAVY','RAINING') else ''}">
                <div style="font-size:15px; font-weight:600;">{zone.name}</div>
                <div style="font-size:22px; font-weight:700; margin:4px 0;">
                    {STATE_EMOJI.get(state, '🟣')} {state}
                </div>
                <div style="font-size:12px; opacity:.9;">since {myt(_parse_ts(row.get('ts')))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        # tile footer (plain markdown, keeps text selectable)
        w = windows.get(zone.name)
        if w and w.get("text"):
            if w["text"].startswith("none"):
                st.caption("no rain 12h")
            else:
                peak = f", peak {w['precip_mm_h']:g} mm/h" if w.get("precip_mm_h") is not None else ""
                st.caption(f"rain {w['text']} MYT{peak}")
        if warn_line:
            st.caption(warn_line)

# ---------------------------------------------------------------- zone detail
st.subheader("Zone detail")
st.caption("Full picture for one zone: what is happening, when rain is coming, and why the state changed.")
zone_names = [z.name for z in cfg.zones]
selected = st.selectbox("Zone", zone_names)
zone = cfg.zone(selected)
row = states.get(selected, {"state": "CLEAR", "ts": None, "reason": None})
state = row.get("state", "CLEAR")
cur = cur_by_zone.get(selected, {})
warn = warnings.get(selected)
warn_active = bool(warn and _fresh(warn["ts"], nowcast_age_s))
win = windows.get(selected, {})

st.info(f"{STATE_EMOJI.get(state, '🟣')} **{state}** — {STATE_MEANING.get(state, '')}")

c1, c2, c3 = st.columns(3)
with c1:
    st.markdown("**Right now**")
    precip = cur.get("precip")
    st.write(f"Rain intensity: {f'{precip:g} mm/h' if pd.notna(precip) else 'no data'}")
    prob = cur.get("prob")
    st.write(f"Chance of rain in next 2h: {f'{prob:g}%' if pd.notna(prob) else 'no data'}")
    st.caption(f"State since {myt_full(_parse_ts(row.get('ts')))} MYT")
    if row.get("reason"):
        st.caption(f"Why it changed: {row['reason']}")
with c2:
    st.markdown("**When rain is coming (next 12h)**")
    if win.get("text", "").startswith("none"):
        st.write("No rain expected in the next 12 hours")
    elif win.get("text"):
        peak = f" (peak {win['precip_mm_h']:g} mm/h)" if win.get("precip_mm_h") is not None else ""
        st.write(f"🌧 Rain from {win['text']} MYT{peak}")
        st.caption(f"Forecast recorded {myt_full(_parse_ts(win.get('ts')))} MYT")
    else:
        st.write("Waiting for first forecast…")
    p12 = win.get("probability_pct")
    if p12 is not None:
        st.write(f"Chance of any rain next 12h: {p12:g}%")
with c3:
    st.markdown("**MET Malaysia (official)**")
    if warn_active:
        st.error(f"⚠️ Active warning: {warn['text']}")
    else:
        st.write("No active warning for this zone")
    district_lines = [
        f"{d}: {forecasts[d]['text']}" for d in zone.met_districts if d in forecasts
    ]
    if district_lines:
        st.caption("Today's forecast — " + " • ".join(district_lines))
    else:
        st.caption("Today's forecast: not fetched yet (daily at 07:00)")

with st.expander("ℹ️ What do these numbers mean?"):
    st.markdown(
        """
- **Rain intensity (mm/h):** 0.5 = light rain, 2–5 = steady, 7.5+ = heavy
  (the HEAVY threshold). Measured at the zone's sample points, highest point shown.
- **Chance next 2h / 12h:** probability from Open-Meteo's hourly model — how
  likely *any* rain is, not how much.
- **Rain window (e.g. 13:00-18:00):** the first continuous run of hours at or
  above 0.5 mm/h in the forecast. "peak" = the strongest hour in that run.
- **MET warning:** official warning covering this zone's districts — overrides
  the model (a warning forces HEAVY regardless of the numbers).
    """
    )

if st.button("🔕 Snooze 1h", key=f"snooze_{selected}"):
    repo.snooze(selected, datetime.now(timezone.utc) + timedelta(hours=1))
    st.cache_data.clear()
    st.toast(f"{selected} muted for 1h — no notifications for this zone")
if repo.is_snoozed(selected, datetime.now(timezone.utc)):
    st.caption(f"🔕 {selected} is snoozed — notifications suppressed for now")

# ---------------------------------------------------------------- 24h history
st.subheader("Last 24 hours")
st.caption(
    "What actually happened: rain intensity per zone over 24h. Spikes = rain fell. "
    "The peak table shows the strongest value each zone reached."
)
if nowcast.empty:
    st.info("No observations yet — waiting for the first poll cycle.")
else:
    wide = nowcast.pivot_table(index="ts", columns="zone", values="mm_h", aggfunc="max").sort_index()
    st.line_chart(wide, height=300)
    prob_chart = nowcast.pivot_table(index="ts", columns="zone", values="prob", aggfunc="max")
    if not prob_chart.empty and prob_chart.notna().any().any():
        with st.expander("Rain probability % over time"):
            st.area_chart(prob_chart.sort_index(), height=220)
    peaks = nowcast[nowcast["mm_h"].notna()].groupby("zone")["mm_h"].max().sort_values(ascending=False)
    if not peaks.empty:
        st.dataframe(peaks.rename("peak mm/h (24h)").to_frame(), width="stretch")

# ---------------------------------------------------------------- notifications
left, right = st.columns([2, 1])
with left:
    st.subheader("Notifications")
    st.caption(
        "Automatic log — one entry per state change, newest first. "
        "“merged” = several zones changed at once and were reported together."
    )
    if data["alerts"]:
        for a in data["alerts"]:
            title = (
                f"{myt_full(_parse_ts(a['ts']))} MYT — {a['zone']} → {a['state']}"
                + ("  🌧 merged" if a["merged"] else "")
            )
            with st.expander(title):
                st.text(a["text"] or "(no text)")
    else:
        st.caption("No notifications yet.")
with right:
    st.subheader("Source health")
    st.caption("Did each data source answer the last poll? A failing source keeps the last known data and is flagged here.")
    if data["health"]:
        for h in data["health"]:
            ok = bool(h["ok"])
            st.metric(h["name"], "✅ OK" if ok else "❌ failing",
                      delta=f"last ok {myt(_parse_ts(h['last_ok']))} MYT" if h["last_ok"] else None)
            if h.get("last_error"):
                st.caption(h["last_error"][:160])
    else:
        st.caption("No source health recorded yet.")
