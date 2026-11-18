"""Config loading: .env (secrets) + zones.yaml (zones, thresholds, colours)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .engine.thresholds import Thresholds

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Point:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Zone:
    name: str
    met_districts: tuple[str, ...]
    points: tuple[Point, ...]
    centroid: tuple[float, float]


@dataclass(frozen=True)
class ActiveHours:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end  # window wraps midnight


@dataclass
class Config:
    zones: list[Zone]
    aliases: dict[str, str]
    thresholds: Thresholds
    state_colors: dict[str, str]
    active_hours: ActiveHours | None
    poll_open_meteo: int
    poll_met_warning: int
    bbox: dict[str, float]
    db_path: Path
    health_port: int
    root: Path = ROOT

    def zone(self, name: str) -> Zone | None:
        for z in self.zones:
            if z.name == name:
                return z
        return None


def _parse_hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "zones.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    th_fields = Thresholds.__dataclass_fields__
    thresholds = Thresholds(**{k: v for k, v in (raw.get("thresholds") or {}).items() if k in th_fields})

    ah = raw.get("active_hours")
    active_hours = ActiveHours(_parse_hhmm(ah["start"]), _parse_hhmm(ah["end"])) if ah else None

    zones: list[Zone] = []
    for z in raw.get("zones") or []:
        points = tuple(
            Point(str(p["name"]), float(p["lat"]), float(p["lon"]))
            for p in z.get("points") or []
        )
        zones.append(
            Zone(
                name=str(z["name"]),
                met_districts=tuple(str(d) for d in z.get("met_districts") or []),
                points=points,
                centroid=tuple(float(c) for c in z.get("centroid") or (0.0, 0.0)),
            )
        )

    poll = raw.get("poll_interval_seconds") or {}
    return Config(
        zones=zones,
        aliases={str(k): str(v) for k, v in (raw.get("aliases") or {}).items()},
        thresholds=thresholds,
        state_colors={str(k): str(v) for k, v in (raw.get("state_colors") or {}).items()},
        active_hours=active_hours,
        poll_open_meteo=int(poll.get("open_meteo", 600)),
        poll_met_warning=int(poll.get("met_warning", 300)),
        bbox={str(k): float(v) for k, v in (raw.get("klang_valley_bbox") or {}).items()},
        db_path=ROOT / os.getenv("DB_PATH", "data/rain.db"),
        health_port=int(os.getenv("HEALTH_PORT", "8080")),
    )
