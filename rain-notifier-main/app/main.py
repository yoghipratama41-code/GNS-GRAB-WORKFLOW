"""Scheduler entrypoint (spec 1, 9, 11).

    python -m app.main
    python -m app.main --simulate "Petaling Jaya=3.0"

Polls both sources on schedule, runs the per-zone state machine, records one
notification per state change (merged/coalesced per spec 5), serves /health and
launches the Streamlit dashboard (the app's UI).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config, load_config
from .db.repo import Repo
from .engine.state_machine import AlertCoordinator, AlertItem, ZoneFSM, ZoneReading
from .notify.formatter import format_alert, format_combined
from .sources.base import Observation
from .sources.met_api import MetSource
from .sources.open_meteo import OpenMeteoSource

MYT = ZoneInfo("Asia/Kuala_Lumpur")
ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("rain")


# --------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    """Structured (JSON lines) logging, spec 9."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    # Windows consoles default to cp1252, which cannot encode the alert emoji;
    # a UnicodeEncodeError there would abort a send and lose the alert.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


# --------------------------------------------------------------------------- app
class RainApp:
    def __init__(self, cfg: Config, simulate: dict[str, float] | None = None):
        self.cfg = cfg
        self.simulate = simulate or {}
        self.repo = Repo(cfg.db_path)
        self.repo.init()
        self.repo.sync_zones(cfg.zones)

        self.om = OpenMeteoSource()
        self.met = MetSource()

        self.fsms = {z.name: ZoneFSM(z.name, cfg.thresholds) for z in cfg.zones}
        self.coordinator = AlertCoordinator(
            th=cfg.thresholds,
            send=self._send_alerts,
            in_active_hours=self._in_active_hours,
            is_snoozed=self.repo.is_snoozed,
        )

        self.nowcast: dict[str, tuple[float | None, float | None]] = {}
        self.nowcast_ts: datetime | None = None
        self.met_ok = False
        self.last_cycle: datetime | None = None
        self.started = datetime.now(timezone.utc)

    # ---- scheduling helpers --------------------------------------------
    def _nowcast_fresh(self, now: datetime) -> bool:
        if self.nowcast_ts is None:
            return False
        return (now - self.nowcast_ts) <= timedelta(seconds=self.cfg.poll_open_meteo * 3)

    def _in_active_hours(self) -> bool:
        ah = self.cfg.active_hours
        return ah.contains(datetime.now(MYT).time()) if ah else True

    # ---- polls -----------------------------------------------------------
    def poll_open_meteo(self) -> None:
        try:
            obs = self.om.fetch(self.cfg.zones, rain_mm_h=self.cfg.thresholds.raining_mm_h)
        except Exception as exc:
            log.warning("open-meteo poll failed, keeping last known readings: %s", exc)
        else:
            nowcast: dict[str, tuple[float | None, float | None]] = {}
            for o in obs:
                if o.kind == "rain_window":
                    continue                # future window, not current conditions
                pr, pb = nowcast.get(o.zone, (None, None))
                if o.precip_mm_h is not None:
                    pr = o.precip_mm_h if pr is None else max(pr, o.precip_mm_h)
                if o.probability_pct is not None:
                    pb = o.probability_pct if pb is None else max(pb, o.probability_pct)
                nowcast[o.zone] = (pr, pb)
            self.nowcast = nowcast
            self.nowcast_ts = datetime.now(timezone.utc)
            self.repo.save_observations(obs)
        self._evaluate()

    def poll_met_warning(self) -> None:
        try:
            obs = self.met.fetch()
            self.met_ok = True
            self.repo.save_observations(obs)
        except Exception as exc:
            self.met_ok = False
            log.warning("MET warning poll failed, running without warnings: %s", exc)
        self._evaluate()

    def poll_met_forecast(self) -> None:
        districts = [d for z in self.cfg.zones for d in z.met_districts]
        try:
            self.repo.save_observations(self.met.fetch_forecast(districts))
        except Exception as exc:
            log.warning("MET forecast poll failed: %s", exc)

    # ---- evaluation -------------------------------------------------------
    def _build_reading(self, zone, now: datetime) -> ZoneReading:
        fresh = self._nowcast_fresh(now)
        precip = prob = None
        if fresh:
            precip, prob = self.nowcast.get(zone.name, (None, None))
        if zone.name in self.simulate:
            precip = self.simulate[zone.name]
            fresh = True
        return ZoneReading(
            zone=zone.name,
            ts=now,
            precip_mm_h=precip,
            probability_pct=prob,
            warning_types=self.met.warning_types_for(zone.met_districts),
            warning_labels=self.met.warning_labels_for(zone.met_districts),
            source="simulated" if zone.name in self.simulate else "open_meteo",
            sampled=fresh,
        )

    def _evaluate(self) -> None:
        now = datetime.now(timezone.utc)
        if not self._nowcast_fresh(now) and not self.met_ok:
            log.warning("both sources are down — holding zone states")
            self._persist_health()
            return
        self.last_cycle = now
        items: list[AlertItem] = []
        warning_rows = []
        for zone in self.cfg.zones:
            reading = self._build_reading(zone, now)
            if reading.warning_labels:
                # snapshot per zone so the dashboard can show warnings without
                # re-querying MET; absence of a fresh row means "no warning"
                warning_rows.append(
                    Observation(
                        zone=zone.name, point="met", source="met", ts=now,
                        kind="zone_warning", text="; ".join(reading.warning_labels),
                    )
                )
            transition = self.fsms[zone.name].update(reading)
            if transition:
                self.repo.save_state_event(
                    transition.zone, transition.from_state, transition.to_state,
                    transition.ts, transition.reason,
                )
                items.append(AlertItem(transition=transition, reading=reading))
        if warning_rows:
            self.repo.save_observations(warning_rows)
        if items:
            self.coordinator.submit(items, now)
        self._persist_health()

    def flush_alerts(self) -> None:
        self.coordinator.tick(datetime.now(timezone.utc))
        self._persist_health()

    # ---- output -----------------------------------------------------------
    def _send_alerts(self, items: list[AlertItem], merged: bool) -> None:
        """Record notifications; the dashboard is the delivery channel (spec 6 revision)."""
        text = format_combined(items) if merged else format_alert(items[0])
        for it in items:
            self.repo.save_alert(
                it.transition.zone, it.transition.to_state, it.transition.ts,
                text, None, merged,
            )
        log.info(
            "notification recorded merged=%s zones=%s", merged,
            [i.transition.zone for i in items],
        )

    def _persist_health(self) -> None:
        for health in (self.om.health, self.met.health):
            try:
                self.repo.save_source_health(health)
            except Exception:
                log.exception("saving source health failed")

    # ---- /health ----------------------------------------------------------
    def health_payload(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "status": "ok",
            "zones": len(self.cfg.zones),
            "simulate": self.simulate or None,
            "uptime_seconds": int((now - self.started).total_seconds()),
            "last_cycle_utc": self.last_cycle.isoformat() if self.last_cycle else None,
            "nowcast_age_seconds": (
                int((now - self.nowcast_ts).total_seconds()) if self.nowcast_ts else None
            ),
            "pending_alerts": self.coordinator.pending_count,
            "sources": self.repo.source_health(),
        }

    def _start_health_server(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(owner.health_payload(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # keep the access log out of our logs
                pass

        try:
            server = ThreadingHTTPServer(("0.0.0.0", self.cfg.health_port), Handler)
        except OSError as exc:
            log.error("health endpoint not started on port %s: %s", self.cfg.health_port, exc)
            return
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 1}, daemon=True).start()
        log.info("health endpoint listening on :%s/health", self.cfg.health_port)

    # ---- dashboard --------------------------------------------------------
    def _start_dashboard(self) -> None:
        """Launch the Streamlit UI as a child process (laptop: one command, whole app)."""
        port = int(os.getenv("DASHBOARD_PORT", "8501"))
        log_path = self.cfg.db_path.parent / "dashboard.log"
        try:
            out = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            out = subprocess.DEVNULL
        cmd = [
            sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
            "--server.port", str(port), "--browser.gatherUsageStats", "false",
        ]
        try:
            self._dashboard = subprocess.Popen(
                cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001 - dashboard is optional, scheduler continues
            log.error("dashboard failed to start: %s (run: streamlit run dashboard/app.py)", exc)
            return
        log.info("dashboard: http://localhost:%s  (log: %s)", port, log_path)

    # ---- run --------------------------------------------------------------
    def start_background(self) -> BackgroundScheduler:
        """Start polling in-process: no health server, no dashboard, no blocking loop.

        Used by `run()` and by hosts that already have a UI (the Streamlit hub page).
        """
        cfg, th = self.cfg, self.cfg.thresholds
        log.info(
            "starting: zones=%d open_meteo=%ss met_warning=%ss cooldown=%smin merge>=%s zones/%ss",
            len(cfg.zones), cfg.poll_open_meteo, cfg.poll_met_warning,
            th.cooldown_minutes, th.merge_min_zones, th.merge_grace_seconds,
        )
        if self.simulate:
            log.info("SIMULATION active: %s", self.simulate)

        scheduler = BackgroundScheduler(timezone=str(MYT))
        scheduler.add_job(self.poll_open_meteo, "interval", seconds=cfg.poll_open_meteo,
                          id="open_meteo", max_instances=1, coalesce=True)
        scheduler.add_job(self.poll_met_warning, "interval", seconds=cfg.poll_met_warning,
                          id="met_warning", max_instances=1, coalesce=True)
        scheduler.add_job(self.poll_met_forecast, "cron", hour=7, minute=0,
                          id="met_forecast", max_instances=1)
        scheduler.add_job(self.flush_alerts, "interval", seconds=15,
                          id="flush", max_instances=1)

        # First real cycle before scheduling so the app is useful immediately.
        self.poll_met_warning()
        self.poll_open_meteo()
        if self.simulate:
            # satisfy confirm_readings=2 with two consecutive injected readings,
            # so --simulate fires without waiting for the next poll (spec AC2)
            self.poll_open_meteo()

        scheduler.start()
        return scheduler

    def run(self) -> None:
        scheduler = self.start_background()
        self._start_health_server()
        disabled = os.getenv("NO_DASHBOARD", "").strip().lower() in ("1", "true", "yes")
        if not disabled:
            self._start_dashboard()

        try:
            while True:
                time.sleep(60)
        finally:
            scheduler.shutdown(wait=False)
            dashboard = getattr(self, "_dashboard", None)
            if dashboard and dashboard.poll() is None:
                dashboard.terminate()


# --------------------------------------------------------------------------- cli
def parse_simulate(values: list[str], cfg: Config) -> dict[str, float]:
    sim: dict[str, float] = {}
    for value in values:
        name, sep, raw = value.partition("=")
        name = name.strip()
        if not sep or not raw.strip():
            raise SystemExit(f'--simulate expects ZONE=MM_H, e.g. --simulate "Petaling Jaya=3.0" (got {value!r})')
        zone = cfg.zone(name) or next(
            (z for z in cfg.zones if z.name.casefold() == name.casefold()), None
        )
        if zone is None:
            raise SystemExit(
                f"unknown zone {name!r}; valid zones: {', '.join(z.name for z in cfg.zones)}"
            )
        try:
            sim[zone.name] = float(raw)
        except ValueError:
            raise SystemExit(f"invalid mm/h value in --simulate: {raw!r}") from None
    return sim


def main() -> None:
    parser = argparse.ArgumentParser(description="Klang Valley rain notifier")
    parser.add_argument(
        "--simulate", action="append", default=[], metavar="ZONE=MM_H",
        help='inject fake rain, e.g. --simulate "Petaling Jaya=3.0" (repeatable)',
    )
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    simulate = parse_simulate(args.simulate, cfg)
    RainApp(cfg, simulate).run()


if __name__ == "__main__":
    main()
