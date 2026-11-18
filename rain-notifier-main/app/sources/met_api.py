"""MET Malaysia official adapter (spec 4A): warnings (authoritative) + 7-day forecast.

Live response shapes were inspected during the build:
- GET /weather/warning returns a list of
  {warning_issue: {issued, title_bm, title_en}, valid_from, valid_to,
   heading_en, text_en, heading_bm, text_bm, ...}.
  Affected areas are NOT structured — they sit inside the free text, so a warning is
  matched to a zone by looking for the zone's `met_districts` names in the text
  (word-boundary, both languages).
- GET /weather/forecast returns
  {location: {location_id, location_name}, date, morning_forecast, ...,
   summary_forecast (Bahasa Melayu), summary_when, min_temp, max_temp}.
  Location ids are discovered by name at runtime (never hardcoded) and cached.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .base import Observation, SourceHealth

log = logging.getLogger("rain.met")

URL = "https://api.data.gov.my/weather"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
ROOT = Path(__file__).resolve().parents[2]
LOCATION_CACHE = ROOT / "data" / "met_locations.json"

# Titles/headings that are not land-rain warnings (marine, advisories).
_SKIP = ("tiada nasihat", "no advisory")
_MARINE = ("laut bergelora", "rough seas", "angin kencang dan laut", "strong winds and rough seas")

# Bahasa Melayu forecast values -> short English (docs' value table).
FORECAST_EN = {
    "tiada hujan": "No rain",
    "hujan": "Rain",
    "hujan di beberapa tempat": "Scattered rain",
    "hujan di satu dua tempat": "Isolated rain",
    "hujan di satu dua tempat di kawasan pantai": "Isolated rain (coast)",
    "hujan di satu dua tempat di kawasan pedalaman": "Isolated rain (inland)",
    "ribut petir": "Thunderstorm",
    "ribut petir di beberapa tempat": "Scattered thunderstorms",
    "ribut petir di beberapa tempat di kawasan pedalaman": "Scattered thunderstorms (inland)",
    "ribut petir di satu dua tempat": "Isolated thunderstorms",
    "ribut petir di satu dua tempat di kawasan pantai": "Isolated thunderstorms (coast)",
    "ribut petir di satu dua tempat di kawasan pedalaman": "Isolated thunderstorms (inland)",
    "berjerebu": "Hazy",
}
WHEN_EN = {
    "pagi": "morning",
    "petang": "afternoon",
    "malam": "night",
    "pagi dan petang": "morning & afternoon",
    "pagi dan malam": "morning & night",
    "petang dan malam": "afternoon & night",
    "sepanjang hari": "all day",
}


@dataclass
class MetWarning:
    wtype: str            # thunderstorm | rain | heavy_rain
    title_en: str
    text_en: str
    text_bm: str
    issued: str | None
    valid_from: str | None
    valid_to: str | None

    def blob(self) -> str:
        return " ".join(
            filter(None, [self.title_en, self.text_en, self.text_bm])
        )


def classify_warning(item: dict) -> str | None:
    """Classify a raw warning row; None = skip (advisory / marine / unrelated)."""
    wi = item.get("warning_issue") or {}
    title_en = str(wi.get("title_en") or "")
    title_bm = str(wi.get("title_bm") or "")
    heading_en = str(item.get("heading_en") or "")
    heading_bm = str(item.get("heading_bm") or "")
    blob_en = f"{title_en} {heading_en}".lower()
    blob_bm = f"{title_bm} {heading_bm}".lower()
    both = blob_en + " " + blob_bm
    if any(k in both for k in _SKIP):
        return None
    if any(k in both for k in _MARINE):
        return None  # warnings about Malaysian waters don't cover our zones
    if "sangat lebat" in blob_bm or "hujan lebat" in blob_bm or "heavy rain" in blob_en:
        return "heavy_rain"
    if "ribut petir" in blob_bm or "thunderstorm" in blob_en:
        return "thunderstorm"
    if "hujan" in blob_bm or "rain" in blob_en:
        return "rain"
    return None


def districts_in_text(district: str, text: str) -> bool:
    """Word-boundary match so 'Klang' does not match 'Klangg' but does match 'Pelabuhan Klang'."""
    return re.search(r"(?<![\w])" + re.escape(district) + r"(?![\w])", text, re.IGNORECASE) is not None


class MetSource:
    name = "met"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.health = SourceHealth(name=self.name)
        self.warnings: list[MetWarning] = []
        self._locations: dict[str, str] = {}
        if LOCATION_CACHE.exists():
            try:
                self._locations = json.loads(LOCATION_CACHE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._locations = {}

    # ---- warnings -------------------------------------------------------
    def fetch(self) -> list[Observation]:
        """Poll active warnings; parsed result kept for zone matching."""
        try:
            r = httpx.get(f"{URL}/warning", timeout=self.timeout, follow_redirects=True)
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:
            self.health.fail(f"{type(exc).__name__}: {exc}")
            raise

        now = datetime.now(timezone.utc)
        warnings: list[MetWarning] = []
        for item in rows or []:
            wtype = classify_warning(item)
            if not wtype:
                continue
            wi = item.get("warning_issue") or {}
            warnings.append(
                MetWarning(
                    wtype=wtype,
                    title_en=str(wi.get("title_en") or ""),
                    text_en=str(item.get("text_en") or ""),
                    text_bm=str(item.get("text_bm") or ""),
                    issued=wi.get("issued"),
                    valid_from=item.get("valid_from"),
                    valid_to=item.get("valid_to"),
                )
            )
        self.warnings = warnings
        self.health.touch()
        log.debug("active MET warnings: %d of %d rows", len(warnings), len(rows or []))
        return [
            Observation(
                zone="",
                point=w.title_en,
                source=self.name,
                ts=now,
                kind="warning",
                warning_type=w.wtype,
                text=w.text_en or w.text_bm,
            )
            for w in warnings
        ]

    def warning_types_for(self, districts: tuple[str, ...] | list[str]) -> set[str]:
        out: set[str] = set()
        for w in self.warnings:
            blob = w.blob()
            if any(districts_in_text(d, blob) for d in districts):
                out.add(w.wtype)
        return out

    def warning_labels_for(self, districts: tuple[str, ...] | list[str]) -> list[str]:
        """['thunderstorm, Petaling'] — used in the alert message (spec 6)."""
        labels: list[str] = []
        for w in self.warnings:
            blob = w.blob()
            hit = [d for d in districts if districts_in_text(d, blob)]
            if hit:
                labels.append(f"{w.wtype}, {', '.join(hit)}")
        return labels

    # ---- forecast -------------------------------------------------------
    def resolve_location(self, name: str) -> str | None:
        """Discover a location_id for a district/town name (cached; never hardcoded)."""
        if name in self._locations:
            return self._locations[name]
        try:
            r = httpx.get(
                f"{URL}/forecast",
                params={"contains": f"{name}@location__location_name", "limit": 50},
                timeout=self.timeout,
                follow_redirects=True,
            )
            r.raise_for_status()
            for row in r.json():
                loc = row.get("location") or {}
                if str(loc.get("location_name", "")).casefold() == name.casefold():
                    lid = str(loc["location_id"])
                    self._locations[name] = lid
                    try:
                        LOCATION_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        LOCATION_CACHE.write_text(
                            json.dumps(self._locations, indent=1, ensure_ascii=False), encoding="utf-8"
                        )
                    except OSError:
                        pass
                    return lid
        except Exception as exc:
            log.warning("location lookup failed for %r: %s", name, exc)
        return None

    def fetch_forecast(self, districts: list[str] | tuple[str, ...]) -> list[Observation]:
        """Today's general forecast per district (low-confidence daily hint, spec 4A)."""
        today = datetime.now(MYT).date().isoformat()
        out: list[Observation] = []
        now = datetime.now(timezone.utc)
        for district in dict.fromkeys(districts):
            lid = self.resolve_location(district)
            if not lid:
                continue
            try:
                r = httpx.get(
                    f"{URL}/forecast",
                    params={"contains": f"{lid}@location__location_id", "limit": 10},
                    timeout=self.timeout,
                    follow_redirects=True,
                )
                r.raise_for_status()
                rows = r.json()
            except Exception as exc:
                log.warning("forecast failed for %s: %s", district, exc)
                continue
            row = next((x for x in rows if x.get("date") == today), None)
            if not row:
                continue
            summary = str(row.get("summary_forecast") or "")
            when = WHEN_EN.get(str(row.get("summary_when") or "").lower(), "")
            text = FORECAST_EN.get(summary.lower(), summary)
            if when:
                text = f"{text} ({when})"
            out.append(
                Observation(
                    zone="",
                    point=district,
                    source=self.name,
                    ts=now,
                    kind="forecast",
                    text=text,
                )
            )
        return out
