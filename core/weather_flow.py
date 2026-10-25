"""Stage 31-P4 (2026-10-22, user-picked): weather via a free endpoint.

USER DECISION 2026-10-22: free open-data endpoint (Open-Meteo, no
account), stdlib urllib only. Honesty rules:
- every answer carries the SOURCE and a TIME label ("as of HH:MM");
- offline/failed fetches are reported as failures with the reason,
  never as fabricated weather;
- location: 'in <city>' geocodes via Open-Meteo's free geocoder and
  NAMES the chosen match (top result, bounded); without a city it
  uses ZARA_WEATHER_LAT/LON; neither → an honest ask;
- rate courtesy: one bounded fetch per turn, 4 s timeout.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import time
from datetime import datetime
from threading import Lock

_INVITE_RE = re.compile(
    r"^\s*(?:what(?:'?s|\s+is)|how(?:'?s|\s+is))\s+the weather\b|"
    r"^\s*weather (?:now|today|tomorrow|forecast|in)\b|"
    r"^\s*forecast in\b|^\s*(?:will it "
    r"(?:rain|snow)|is it (?:raining|sunny|cold|hot))\b|"
    r"^\s*(?:weather|forecast)\s*$", re.I)
_CITY_RE = re.compile(r"\bin\s+([a-zA-Z][a-zA-Z .\-]{1,60})")
# P5 feature-hunt ③ (2026-10-23): the SKY/RAIN verdict must match the
# ASKED DAY — a "tomorrow" invite never answers with today's sky.
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
_RAIN_ASK_RE = re.compile(
    r"\b(?:will it (?:rain|snow)|rain tomorrow|snow tomorrow)\b", re.I)
_TODAY_RE = re.compile(r"\btoday\b|\bnow\b", re.I)
_MAX_CITY = 60
_TIMEOUT_S = 4.0


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "zara-assistant/1 (personal; "
                                    "local operator)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        data = r.read(200_000)
    return json.loads(data.decode("utf-8", errors="replace"))


def geocode(city: str) -> dict:
    city_q = city.strip()[:_MAX_CITY]
    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + urllib.parse.quote(city_q) + "&count=3&language=en"
           + "&format=json")
    out = _fetch_json(url)
    results = out.get("results") or []
    if not results:
        return {"found": False, "city": city_q}
    top = results[0]
    return {"found": True,
            "lat": float(top.get("latitude", 0)),
            "lon": float(top.get("longitude", 0)),
            "name": str(top.get("name", city_q))[:80],
            "country": str(top.get("country", ""))[:80],
            "admin": str(top.get("admin1", ""))[:80]}


def forecast(lat: float, lon: float) -> dict:
    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat:.4f}&longitude={lon:.4f}"
           "&current=temperature_2m,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability"
           # P5 feature-hunt ③: daily maxima power the tomorrow answers
           # (code, min/max temp, precipitation-probability max) —
           # same free endpoint, no new deps, still 2 forecast days.
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max"
           "&forecast_days=2&timezone=auto&wind_speed_unit=kmh"
           "&temperature_unit=celsius")
    return _fetch_json(url)


_CODES = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy",
          3: "overcast", 45: "fog", 48: "fog (frost)", 51: "light drizzle",
          53: "drizzle", 55: "heavy drizzle", 61: "light rain",
          63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
          75: "heavy snow", 80: "rain showers", 81: "rain showers",
          82: "violent showers", 95: "thunderstorm", 96: "thunderstorm"
          " with hail", 99: "severe thunderstorm"}


def describe(out: dict) -> str:
    cur = out.get("current") or {}
    temp = cur.get("temperature_2m")
    code = int(cur.get("weather_code", -1) or -1)
    cond = _CODES.get(code, "unknown sky")
    wind = cur.get("wind_speed_10m")
    hourly = (out.get("hourly") or {})
    times = hourly.get("time") or []
    rain = hourly.get("precipitation_probability") or []
    rain_txt = ""
    try:
        now_h = datetime.now().strftime("%Y-%m-%dT%H:00")
        i = times.index(now_h)
        nxt = rain[i:i + 6]
        peak = max(nxt) if nxt else None
        if peak is not None:
            rain_txt = (f", rain chance up to {peak}% in the next 6 h"
                        if peak else
                        ", no rain expected in the next 6 h")
    except Exception:
        rain_txt = ""
    tz = out.get("timezone", "")
    stamp = str(cur.get("time", ""))[:16]
    src = (" — source: Open-Meteo, no account, fetched "
           + (f"{stamp}" if stamp else "just now")
           + (f" ({tz})" if tz else ""))
    wind_txt = f", wind {wind} km/h" if wind is not None else ""
    return (f"{temp}{chr(176)}C, {cond}{wind_txt}" + rain_txt + src)


def _daily_ok(out: dict) -> bool:
    """A tomorrow answer needs the daily block with both days."""
    daily = out.get("daily") or {}
    dates = daily.get("time") or []
    return len(dates) >= 2


def _rain_verdict(p: int, asked_snow: bool) -> str:
    """Honest likelihood wording from the daily precipitation max — the
    verdict leads, the numbers carry the evidence."""
    if p is None:
        return ("I couldn't read tomorrow's precipitation chance — "
                "no honest verdict.")
    if p >= 60:
        lead = "Yes"
    elif p >= 40:
        lead = "Very likely"
    elif p >= 20:
        lead = "Maybe"
    else:
        lead = "Probably not"
    num = ("precipitation chance up to" if asked_snow else "rain chance "
           "up to") + f" {p}%"
    return f"{lead} — {num}"


def describe_tomorrow(out: dict, asked_rain: bool = False,
                      asked_snow: bool = False) -> str:
    """P5 feature-hunt ③ (2026-10-23): tomorrow's answer from the daily
    block (index 1 of a 2-day fetch). Verdict-first when the user asked
    about rain/snow; always source+time labeled. Missing daily data is
    an honest string, never a fabricated forecast."""
    daily = out.get("daily") or {}
    dates = daily.get("time") or []
    if len(dates) < 2:
        return ("I couldn't read tomorrow's forecast block — I'd "
                "rather say that than invent one.")
    tday = str(dates[1])[:10]
    tmax = (daily.get("temperature_2m_max") or [None, None])[1]
    tmin = (daily.get("temperature_2m_min") or [None, None])[1]
    code = int((daily.get("weather_code") or [-1, -1])[1] or -1)
    cond = _CODES.get(code, "unknown sky")
    p = (daily.get("precipitation_probability_max") or [None, None])[1]
    src = (" — source: Open-Meteo, no account, daily block fetched "
           f"{tday}"
           + (f" ({out.get('timezone', '')})" if out.get("timezone")
              else ""))
    if asked_rain or asked_snow:
        temps = ""
        if tmin is not None and tmax is not None:
            temps = f" ({tmin}\u2013{tmax}{chr(176)}C, {cond})"
        return (f"Tomorrow ({tday}): {_rain_verdict(p, asked_snow)}"
                f"{temps}{src}")
    rain_txt = ""
    if p is not None:
        rain_txt = (f" (precip chance up to {p}%)" if p else
                    " (no precipitation expected)")
    temps = ""
    if tmin is not None and tmax is not None:
        temps = f", {tmin}\u2013{tmax}{chr(176)}C"
    return f"Tomorrow ({tday}): {cond}{temps}{rain_txt}{src}"


class WeatherFlow:
    """P4 laptop-side flow: weather reads, one bounded fetch per turn."""

    def __init__(self, audit) -> None:
        self.audit = audit
        self._lock = Lock()
        self._last = 0.0

    def wants_turn(self, clean: str) -> bool:
        s = (clean or "").strip()
        return _INVITE_RE.match(s) is not None

    def _audit(self, action: str, who: str, detail: str = "") -> None:
        try:
            self.audit.record("weather_flow", action, who, detail[:300])
        except Exception:  # noqa: BLE001
            pass

    def turn(self, clean: str, who: str = "user",
             requester_device: str = "") -> dict | None:
        s = (clean or "").strip()
        m = _INVITE_RE.match(s)
        if m is None:
            return None
        # P5 feature-hunt ③: the answered day must match the asked day.
        asks_tomorrow = _TOMORROW_RE.search(s) is not None
        asks_rain = _RAIN_ASK_RE.search(s) is not None
        asks_snow = re.search(r"\bsnow\b", s, re.I) is not None
        city = ""
        cm = _CITY_RE.search(s)
        if cm:
            # Trailing day words are invite grammar, never part of the
            # city ("weather in Lisbon tomorrow" → city "Lisbon").
            city = re.sub(
                r"\s*\b(today|tomorrow|day after tomorrow)\b\s*[?.!]*$",
                "", cm.group(1).strip(' .?'), flags=re.I).strip(' .?')
        lat = os.environ.get("ZARA_WEATHER_LAT")
        lon = os.environ.get("ZARA_WEATHER_LON")
        try:
            now = time.time()
            if now - self._last < 2.0 and self._last:
                return {"message": "One weather call at a time — ask "
                                   "again in a moment.",
                        "proposal": None, "resolution": None,
                        "executed": False}
            # P5 fix (2026-10-23): the courtesy gate ticks on ACTUAL
            # fetches only — a no-location honest ask must not consume
            # the turn's rate window.
            if city:
                g = geocode(city)
                if not g["found"]:
                    self._audit("weather.geo_miss", who, city[:40])
                    return {"message":
                            f"I couldn't find a weather station for "
                            f"'{city[:60]}' — give me another name "
                            "or set ZARA_WEATHER_LAT/LON.",
                            "proposal": None, "resolution": None,
                            "executed": False,
                            "weather_flow": "geo_miss"}
                self._last = now
                where = g["name"] + (f", {g['country']}" if g["country"]
                                     else "")
                out = forecast(g["lat"], g["lon"])
                self._audit("weather.reported", who,
                            f"geocoded={where[:40]}")
                body = (describe_tomorrow(
                            out, asked_rain=asks_rain,
                            asked_snow=asks_snow)
                        if asks_tomorrow else describe(out))
                reported = asks_tomorrow and not _daily_ok(out)
                if reported:
                    self._audit("weather.forecast_missing", who, city[:40])
                return {"message":
                        f"{where}: " + body,
                        "proposal": None, "resolution": None,
                        "executed": not reported, "verified": not reported,
                        "weather_flow": ("forecast_missing" if reported
                                         else "reported")}
            if lat and lon:
                self._last = now
                out = forecast(float(lat), float(lon))
                self._audit("weather.reported", who, "home_coords")
                body = (describe_tomorrow(
                            out, asked_rain=asks_rain,
                            asked_snow=asks_snow)
                        if asks_tomorrow else describe(out))
                reported = asks_tomorrow and not _daily_ok(out)
                if reported:
                    self._audit("weather.forecast_missing", who, "")
                return {"message":
                        body,
                        "proposal": None, "resolution": None,
                        "executed": not reported, "verified": not reported,
                        "weather_flow": ("forecast_missing" if reported
                                         else "reported")}
            self._audit("weather.no_location", who, "")
            return {"message":
                    "Where? Name a city ('weather in Lisbon') or set "
                    "ZARA_WEATHER_LAT/LON once and I'll remember to "
                    "use it for unstated asks.",
                    "proposal": None, "resolution": None,
                    "executed": False, "weather_flow": "need_location"}
        except Exception as e:  # noqa: BLE001 — network failures are data
            self._audit("weather.failed", who, str(e)[:80])
            return {"message":
                    "I couldn't reach the weather service just now "
                    "(offline, degraded, or the endpoint hiccuped) — "
                    "I'd rather say that than invent a forecast. Try "
                    "again in a moment.",
                    "proposal": None, "resolution": None,
                    "executed": False, "weather_flow": "failed"}
