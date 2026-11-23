"""Live weather via Open-Meteo (free, no API key).

City name -> geocoding API -> latitude/longitude -> forecast API -> short structured summary.

Safety and resilience:
- The city name is normalized and checked against a strict character allow-list.
- 8 s timeout per request, one retry with backoff on HTTP 429 / 5xx.
- 10-minute in-memory cache so repeated questions don't hit the rate limit.
- Text coming back from the API (place names) is cleaned and length-capped before it
  reaches the LLM, so a poisoned API response can't smuggle in long instructions.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from typing import Optional

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(8.0)
CACHE_TTL_SECONDS = 600
CITY_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ .,'\-]{2,80}$")

# WMO weather interpretation codes used by Open-Meteo
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "depositing rime fog", 51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle", 61: "slight rain",
    63: "moderate rain", 65: "heavy rain", 66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}
SEVERE_CODES = {65, 67, 75, 82, 86, 95, 96, 99}

_cache: dict[str, tuple[float, dict]] = {}


class WeatherError(Exception):
    """A user-facing weather problem (bad city, not found, rate limited, API down)."""


def normalize_city(city: str) -> str:
    if not isinstance(city, str):
        raise WeatherError("City must be text.")
    name = unicodedata.normalize("NFC", " ".join(city.split()))
    name = name.split(",")[0].strip()  # "Seattle, WA" -> "Seattle"
    if not CITY_RE.match(name):
        raise WeatherError("City name has invalid characters or length.")
    return name


def _clean(text: object, limit: int = 60) -> str:
    s = "".join(ch for ch in str(text or "") if ch.isprintable())
    return re.sub(r"[<>{}\[\]`]", "", s)[:limit]


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    for attempt in range(2):
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException:
            raise WeatherError("The weather service timed out.") from None
        except httpx.HTTPError:
            raise WeatherError("Could not reach the weather service.") from None
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise WeatherError("The weather service is rate-limited or unavailable. Try again shortly.")
        if resp.status_code != 200:
            raise WeatherError(f"The weather service returned HTTP {resp.status_code}.")
        return resp.json()
    raise WeatherError("The weather service is unavailable.")


def summarize(place: dict, forecast: dict) -> dict:
    cur = forecast.get("current", {})
    daily = forecast.get("daily", {})
    code = int(cur.get("weather_code", -1))
    precip_now = float(cur.get("precipitation", 0) or 0)
    rain_today = float((daily.get("precipitation_sum") or [0])[0] or 0)
    chance = (daily.get("precipitation_probability_max") or [None])[0]
    wind = float(cur.get("wind_speed_10m", 0) or 0)
    if code in SEVERE_CODES or rain_today >= 10 or wind >= 50:
        risk = "HIGH"
    elif rain_today >= 2 or (chance or 0) >= 60 or precip_now > 0:
        risk = "MODERATE"
    else:
        risk = "LOW"
    return {
        "location": ", ".join(p for p in (_clean(place.get("name")), _clean(place.get("admin1")),
                                          _clean(place.get("country"))) if p),
        "latitude": round(float(place["latitude"]), 3),
        "longitude": round(float(place["longitude"]), 3),
        "temperature_c": cur.get("temperature_2m"),
        "conditions": WMO_CODES.get(code, "unknown"),
        "precipitation_now_mm": precip_now,
        "precipitation_today_mm": rain_today,
        "precipitation_chance_today_pct": chance,
        "wind_speed_kmh": wind,
        "dispatch_weather_risk": risk,
        "observed_at": _clean(cur.get("time"), 20),
        "source": "open-meteo.com",
    }


async def get_weather(city: str, client: Optional[httpx.AsyncClient] = None) -> dict:
    """Look up live conditions for a city name."""
    name = normalize_city(city)
    key = name.lower()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return {**hit[1], "cached": True}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        geo = await _get_json(client, GEOCODE_URL, {"name": name, "count": 1, "language": "en", "format": "json"})
        results = geo.get("results") or []
        if not results:
            raise WeatherError(f"No location found for '{name}'.")
        place = results[0]
        forecast = await _get_json(client, FORECAST_URL, {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "precipitation_sum,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 1,
        })
    finally:
        if owns_client:
            await client.aclose()
    summary = summarize(place, forecast)
    _cache[key] = (time.monotonic(), summary)
    return summary
