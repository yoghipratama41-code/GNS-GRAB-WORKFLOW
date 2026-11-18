"""Source adapter contract (spec 4): every source exposes fetch() -> list[Observation]."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Observation:
    zone: str                     # "" for source-level rows (e.g. MET warnings)
    point: str                    # sample point name, district name, or "warning"
    source: str                   # open_meteo | met | simulated
    ts: datetime                  # always UTC
    precip_mm_h: float | None = None
    probability_pct: float | None = None
    kind: str = "nowcast"         # nowcast | warning | forecast
    warning_type: str | None = None   # thunderstorm | rain | heavy_rain
    text: str = ""


@dataclass
class SourceHealth:
    name: str
    ok: bool = False
    last_ok: datetime | None = None
    last_error: str | None = None
    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.ok = True
        self.last_error = None
        self.last_ok = datetime.now(timezone.utc)
        self.updated = self.last_ok

    def fail(self, msg: str) -> None:
        self.ok = False
        self.last_error = msg[:500]
        self.updated = datetime.now(timezone.utc)
