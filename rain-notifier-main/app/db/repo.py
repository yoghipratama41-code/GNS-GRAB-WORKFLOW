"""Data access (spec 7). One short-lived connection per call = thread-safe by construction."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import SCHEMA


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


class Repo:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init(self) -> None:
        with self.db() as c:
            c.executescript(SCHEMA)
            # migration for DBs created before text/warning_type existed
            cols = {r[1] for r in c.execute("PRAGMA table_info(observations)")}
            for col, ddl in (("warning_type", "TEXT"), ("text", "TEXT")):
                if col not in cols:
                    c.execute(f"ALTER TABLE observations ADD COLUMN {col} {ddl}")

    # ---- zones ----------------------------------------------------------
    def sync_zones(self, zones) -> None:
        with self.db() as c:
            for z in zones:
                c.execute(
                    "INSERT OR REPLACE INTO zones (name, met_districts, points, centroid) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        z.name,
                        ",".join(z.met_districts),
                        ";".join(f"{p.name}:{p.lat},{p.lon}" for p in z.points),
                        f"{z.centroid[0]},{z.centroid[1]}",
                    ),
                )

    # ---- drop-off points (importer) -------------------------------------
    def replace_dropoffs(self, rows: list[tuple[str, str]]) -> int:
        """Full reload = idempotent: re-running never duplicates rows (spec AC6)."""
        with self.db() as c:
            c.execute("DELETE FROM dropoff_points")
            c.executemany(
                "INSERT OR IGNORE INTO dropoff_points (zone, address) VALUES (?, ?)", rows
            )
            return c.execute("SELECT COUNT(*) FROM dropoff_points").fetchone()[0]

    def dropoffs(self, zone: str | None = None) -> list[tuple[str, str]]:
        q = "SELECT zone, address FROM dropoff_points"
        args: list = []
        if zone:
            q += " WHERE zone = ?"
            args.append(zone)
        q += " ORDER BY rowid"
        with self.db() as c:
            return [(r["zone"], r["address"]) for r in c.execute(q, args)]

    def dropoff_counts(self) -> dict[str, int]:
        with self.db() as c:
            rows = c.execute(
                "SELECT zone, COUNT(*) AS n FROM dropoff_points GROUP BY zone ORDER BY n DESC"
            ).fetchall()
        return {r["zone"]: r["n"] for r in rows}

    # ---- observations ---------------------------------------------------
    def save_observations(self, observations) -> None:
        rows = [
            (
                o.zone, o.point, o.source, iso(o.ts),
                o.precip_mm_h, o.probability_pct, o.kind,
                o.warning_type, o.text,
            )
            for o in observations
        ]
        if not rows:
            return
        with self.db() as c:
            c.executemany(
                "INSERT INTO observations "
                "(zone, point, source, ts, precip_mm_h, probability_pct, kind, warning_type, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def observations_since(self, since: datetime, zone: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM observations WHERE ts >= ?"
        args: list = [iso(since)]
        if zone:
            q += " AND zone = ?"
            args.append(zone)
        q += " ORDER BY ts"
        with self.db() as c:
            return list(c.execute(q, args))

    # ---- state events ---------------------------------------------------
    def save_state_event(self, zone, from_state, to_state, ts, reason) -> None:
        with self.db() as c:
            c.execute(
                "INSERT INTO state_events (zone, from_state, to_state, ts, reason) VALUES (?, ?, ?, ?, ?)",
                (zone, from_state, to_state, iso(ts), reason),
            )

    def latest_states(self) -> dict[str, dict]:
        with self.db() as c:
            rows = c.execute(
                "SELECT e.zone, e.to_state AS state, e.ts, e.reason FROM state_events e "
                "JOIN (SELECT zone, MAX(id) AS id FROM state_events GROUP BY zone) m "
                "ON e.id = m.id"
            ).fetchall()
        return {r["zone"]: dict(r) for r in rows}

    def events_since(self, since: datetime, to_states: tuple[str, ...] | None = None) -> list[dict]:
        q = "SELECT * FROM state_events WHERE ts >= ?"
        args: list = [iso(since)]
        if to_states:
            q += " AND to_state IN (%s)" % ",".join("?" * len(to_states))
            args.extend(to_states)
        q += " ORDER BY ts"
        with self.db() as c:
            return [dict(r) for r in c.execute(q, args)]

    # ---- alerts / acks / incentives -------------------------------------
    def save_alert(self, zone, state, ts, text, message_id=None, merged=False) -> int | None:
        with self.db() as c:
            cur = c.execute(
                "INSERT INTO alerts_sent (zone, state, ts, text, message_id, merged) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (zone, state, iso(ts), text, message_id, 1 if merged else 0),
            )
            return cur.lastrowid

    def recent_alerts(self, zone: str | None = None, limit: int = 20) -> list[dict]:
        q = "SELECT * FROM alerts_sent"
        args: list = []
        if zone:
            q += " WHERE zone = ?"
            args.append(zone)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.db() as c:
            return [dict(r) for r in c.execute(q, args)]

    def latest_alert_id(self, zone: str) -> int | None:
        with self.db() as c:
            row = c.execute(
                "SELECT id FROM alerts_sent WHERE zone = ? ORDER BY id DESC LIMIT 1", (zone,)
            ).fetchone()
        return int(row["id"]) if row else None

    def add_ack(self, alert_id, zone, user, ts) -> None:
        with self.db() as c:
            c.execute(
                "INSERT INTO acks (alert_id, zone, user, ts) VALUES (?, ?, ?, ?)",
                (alert_id, zone, user, iso(ts)),
            )

    def add_incentive(self, zone, ts, source: str = "manual") -> None:
        with self.db() as c:
            c.execute(
                "INSERT INTO incentive_log (zone, ts, source) VALUES (?, ?, ?)",
                (zone, iso(ts), source),
            )

    # ---- source health --------------------------------------------------
    def save_source_health(self, health) -> None:
        with self.db() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_health (name, ok, last_ok, last_error, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    health.name,
                    1 if health.ok else 0,
                    iso(health.last_ok) if health.last_ok else None,
                    health.last_error,
                    iso(health.updated),
                ),
            )

    def source_health(self) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM source_health ORDER BY name")]

    # ---- dashboard reads --------------------------------------------------
    def latest_rain_windows(self) -> dict[str, dict]:
        """Latest 'when is rain coming' row per zone (kind='rain_window')."""
        with self.db() as c:
            rows = c.execute(
                "SELECT o.zone, o.text, o.precip_mm_h, o.probability_pct, o.ts "
                "FROM observations o JOIN (SELECT zone, MAX(id) AS id FROM observations "
                "WHERE kind = 'rain_window' GROUP BY zone) m ON o.id = m.id"
            ).fetchall()
        return {r["zone"]: dict(r) for r in rows}

    def latest_forecasts(self) -> dict[str, dict]:
        """Latest MET daily forecast per district (kind='forecast', zone is empty)."""
        with self.db() as c:
            rows = c.execute(
                "SELECT o.point, o.text, o.ts FROM observations o JOIN "
                "(SELECT point, MAX(id) AS id FROM observations "
                "WHERE kind = 'forecast' GROUP BY point) m ON o.id = m.id"
            ).fetchall()
        return {r["point"]: dict(r) for r in rows}

    def latest_zone_warnings(self) -> dict[str, dict]:
        """Latest MET warning snapshot per zone (kind='zone_warning')."""
        with self.db() as c:
            rows = c.execute(
                "SELECT o.zone, o.text, o.ts FROM observations o JOIN "
                "(SELECT zone, MAX(id) AS id FROM observations "
                "WHERE kind = 'zone_warning' GROUP BY zone) m ON o.id = m.id"
            ).fetchall()
        return {r["zone"]: dict(r) for r in rows}

    # ---- snooze ---------------------------------------------------------
    def snooze(self, zone: str, until: datetime) -> None:
        with self.db() as c:
            c.execute(
                "INSERT OR REPLACE INTO snoozes (zone, until_utc) VALUES (?, ?)",
                (zone, iso(until)),
            )

    def is_snoozed(self, zone: str, now: datetime) -> bool:
        with self.db() as c:
            row = c.execute("SELECT until_utc FROM snoozes WHERE zone = ?", (zone,)).fetchone()
        if not row:
            return False
        until = _dt(row["until_utc"])
        if until and until <= now:
            with self.db() as c:
                c.execute("DELETE FROM snoozes WHERE zone = ?", (zone,))
            return False
        return True
