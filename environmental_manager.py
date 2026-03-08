"""
weather_handler.py — Comprehensive Weather & Air Quality Manager for Mochi Table Assistant
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
APIs used:
  • Open-Meteo  /v1/forecast       — weather forecast (FREE, no key)
  • Open-Meteo  /v1/air-quality    — AQ forecast: PM2.5, PM10, AQI, ozone, etc. (FREE, no key)
  • Open-Meteo  /v1/geocoding      — city name → lat/lon (FREE, no key)
  • OpenAQ      /v3/locations      — nearest real sensor lookup (requires X-API-Key)
  • OpenAQ      /v3/locations/{id}/latest — real-time sensor readings

All public methods return TTS-ready plain-English strings.
CMD tag format:  [CMD:WEATHER:<ACTION>:<arg>]

Supported actions:
  CURRENT   <city>              — temperature, feels-like, humidity, wind, condition
  FORECAST  <city>              — 5-day daily forecast
  HOURLY    <city>              — next 12 hours
  AQI       <city>              — European + US AQI, dominant pollutant, health advice
  POLLUTION <city>              — full breakdown: PM2.5, PM10, NO2, O3, SO2, CO
  POLLEN    <city>              — grass, birch, alder, mugwort pollen levels
  UV        <city>              — UV index + advice
  WIND      <city>              — wind speed, direction, gusts
  HUMIDITY  <city>              — humidity, dewpoint, feels-like
  RAIN      <city>              — precipitation probability + amounts
  SUNRISE   <city>              — sunrise / sunset times
  SENSORS   <city>              — nearby OpenAQ sensor stations list
  REALTIME  <city>              — real-time OpenAQ sensor readings (latest)
"""

import json
import math
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

OPENMETEO_FORECAST_URL  = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_AQ_URL        = "https://air-quality-api.open-meteo.com/v1/air-quality"
OPENMETEO_GEO_URL       = "https://geocoding-api.open-meteo.com/v1/search"
OPENAQ_BASE             = "https://api.openaq.org/v3"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# TTL constants (seconds)
TTL_CURRENT     = 600       # 10 min  — current conditions
TTL_FORECAST    = 1800      # 30 min  — daily forecast
TTL_HOURLY      = 900       # 15 min  — hourly forecast
TTL_AQI         = 900       # 15 min  — air quality index
TTL_POLLEN      = 3600      # 1 hr    — pollen (slow-moving)
TTL_GEO         = 86400     # 24 hr   — geocode results
TTL_SENSORS     = 43200     # 12 hr   — sensor station list
TTL_REALTIME    = 300       # 5 min   — live sensor readings

# WMO weather code → human description
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

# European AQI thresholds & labels
EU_AQI_LABELS = [
    (20,  "Good"),
    (40,  "Fair"),
    (60,  "Moderate"),
    (80,  "Poor"),
    (100, "Very Poor"),
    (999, "Extremely Poor"),
]

# US AQI thresholds & labels
US_AQI_LABELS = [
    (50,  "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (999, "Hazardous"),
]

# Health advice indexed by EU AQI band (0–5)
EU_AQI_ADVICE = [
    "Air quality is great. No precautions needed.",
    "Air quality is acceptable. Unusually sensitive people should consider reducing prolonged outdoor exertion.",
    "Sensitive groups — children, elderly, people with respiratory conditions — should limit prolonged outdoor exertion.",
    "Everyone may experience health effects. Sensitive groups should avoid outdoor exertion.",
    "Health alert: everyone should avoid prolonged outdoor exertion.",
    "Health emergency: everyone should avoid all outdoor activity.",
]

# Wind direction degrees → compass label
def _degrees_to_compass(deg: float) -> str:
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    idx = round(deg / 22.5) % 16
    return dirs[idx]

# UV index → label
def _uv_label(uv: float) -> str:
    if uv < 3:   return "Low"
    if uv < 6:   return "Moderate"
    if uv < 8:   return "High"
    if uv < 11:  return "Very High"
    return "Extreme"

def _uv_advice(uv: float) -> str:
    if uv < 3:   return "No protection needed."
    if uv < 6:   return "Wear sunscreen and a hat if outside for long."
    if uv < 8:   return "Apply SPF 30+, seek shade during midday."
    if uv < 11:  return "SPF 50+ essential, stay in shade between 10am and 4pm."
    return "Avoid sun exposure. SPF 50+ and full coverage clothing required."

def _aqi_label(value: float, scale: list) -> str:
    for threshold, label in scale:
        if value <= threshold:
            return label
    return scale[-1][1]

def _aqi_band(value: float) -> int:
    """Returns 0–5 band index for EU AQI health advice."""
    for i, (threshold, _) in enumerate(EU_AQI_LABELS):
        if value <= threshold:
            return i
    return 5


# ─────────────────────────────────────────────────────────────────────────────
# TTLCache  (same pattern as anime_tools.py for consistency)
# ─────────────────────────────────────────────────────────────────────────────

class TTLCache:
    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int]) -> None:
        with self._lock:
            expires_at = (time.time() + ttl) if ttl is not None else None
            self._store[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# WeatherManager
# ─────────────────────────────────────────────────────────────────────────────

class WeatherManager:
    """
    Main weather + air quality manager.
    All public methods return TTS-ready strings and never raise.
    """

    def __init__(self):
        self._cache = TTLCache()
        self._config: dict = {}
        self._load_config()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r") as f:
                    self._config = json.load(f)
        except Exception as e:
            print(f"[weather_handler] config load error: {e}", flush=True)
            self._config = {}

    def _cfg(self, *keys, default=None):
        obj = self._config
        for k in keys:
            if not isinstance(obj, dict):
                return default
            obj = obj.get(k, default)
            if obj is None:
                return default
        return obj

    def _openaq_headers(self) -> dict:
        key = self._cfg("openaq", "api_key")
        h = {"Accept": "application/json"}
        if key:
            h["X-API-Key"] = key
        return h

    # ── Geocoding ─────────────────────────────────────────────────────────────

    def _geocode(self, city: str) -> Optional[dict]:
        """Returns {name, lat, lon, timezone} or None."""
        cache_key = f"geo:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            r = requests.get(OPENMETEO_GEO_URL, params={
                "name": city, "count": 1, "language": "en", "format": "json"
            }, timeout=8)
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                return None
            res = results[0]
            data = {
                "name":     res.get("name", city),
                "country":  res.get("country", ""),
                "lat":      res["latitude"],
                "lon":      res["longitude"],
                "timezone": res.get("timezone", "auto"),
            }
            self._cache.set(cache_key, data, TTL_GEO)
            return data
        except Exception as e:
            print(f"[weather_handler] geocode error for '{city}': {e}", flush=True)
            return None

    def _resolve(self, city: str) -> Optional[dict]:
        """Geocode with user-friendly failure message embedded."""
        loc = self._geocode(city)
        return loc  # callers check for None and return their own message

    # ── Open-Meteo helpers ────────────────────────────────────────────────────

    def _forecast_get(self, lat: float, lon: float, tz: str, params: dict) -> dict:
        base = {"latitude": lat, "longitude": lon, "timezone": tz, "wind_speed_unit": "kmh"}
        base.update(params)
        r = requests.get(OPENMETEO_FORECAST_URL, params=base, timeout=10)
        r.raise_for_status()
        return r.json()

    def _aq_get(self, lat: float, lon: float, tz: str, params: dict) -> dict:
        base = {"latitude": lat, "longitude": lon, "timezone": tz}
        base.update(params)
        r = requests.get(OPENMETEO_AQ_URL, params=base, timeout=10)
        r.raise_for_status()
        return r.json()

    def _current_index(self, times: list) -> int:
        """Find index in hourly array closest to current time."""
        now = datetime.now()
        best = 0
        best_diff = float("inf")
        for i, t in enumerate(times):
            try:
                dt = datetime.fromisoformat(t)
                diff = abs((dt - now).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best = i
            except Exception:
                pass
        return best

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC METHODS
    # ─────────────────────────────────────────────────────────────────────────

    # ── Current Weather ───────────────────────────────────────────────────────

    def current(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want the weather for."
        cache_key = f"current:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "current": ",".join([
                    "temperature_2m", "apparent_temperature", "relative_humidity_2m",
                    "weather_code", "wind_speed_10m", "wind_direction_10m",
                    "wind_gusts_10m", "cloud_cover", "precipitation",
                    "surface_pressure", "is_day"
                ])
            })
            c = data.get("current", {})
            temp      = c.get("temperature_2m")
            feels     = c.get("apparent_temperature")
            humidity  = c.get("relative_humidity_2m")
            wmo       = c.get("weather_code", 0)
            wind_spd  = c.get("wind_speed_10m")
            wind_dir  = c.get("wind_direction_10m")
            gusts     = c.get("wind_gusts_10m")
            precip    = c.get("precipitation", 0)
            condition = WMO_CODES.get(wmo, "unknown conditions")
            compass   = _degrees_to_compass(wind_dir) if wind_dir is not None else ""

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            parts = [
                f"Current weather in {name}",
                f"{condition}",
                f"{temp:.1f}°C, feels like {feels:.1f}°C",
                f"humidity {humidity}%",
                f"wind {wind_spd:.0f} km/h from the {compass}" +
                (f" with gusts up to {gusts:.0f} km/h" if gusts and gusts > wind_spd + 5 else ""),
            ]
            if precip and precip > 0:
                parts.append(f"{precip:.1f} mm of precipitation in the last hour")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_CURRENT)
            return result
        except Exception as e:
            print(f"[weather_handler] current error: {e}", flush=True)
            return "I couldn't get the current weather right now, try again in a moment."

    # ── 5-day Daily Forecast ──────────────────────────────────────────────────

    def forecast(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want a forecast for."
        cache_key = f"forecast:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "daily": ",".join([
                    "weather_code", "temperature_2m_max", "temperature_2m_min",
                    "precipitation_sum", "precipitation_probability_max",
                    "wind_speed_10m_max", "uv_index_max",
                ]),
                "forecast_days": 5,
            })
            daily    = data.get("daily", {})
            dates    = daily.get("time", [])
            codes    = daily.get("weather_code", [])
            highs    = daily.get("temperature_2m_max", [])
            lows     = daily.get("temperature_2m_min", [])
            precips  = daily.get("precipitation_sum", [])
            prec_pct = daily.get("precipitation_probability_max", [])
            winds    = daily.get("wind_speed_10m_max", [])
            uvs      = daily.get("uv_index_max", [])

            if not dates:
                return f"I couldn't get forecast data for {city}."

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            entries = []
            for i in range(min(5, len(dates))):
                day_name = datetime.fromisoformat(dates[i]).strftime("%A")
                cond     = WMO_CODES.get(codes[i] if i < len(codes) else 0, "")
                high     = f"{highs[i]:.0f}" if i < len(highs) and highs[i] is not None else "?"
                low      = f"{lows[i]:.0f}"  if i < len(lows)  and lows[i]  is not None else "?"
                pct      = f"{prec_pct[i]:.0f}% chance of rain" if i < len(prec_pct) and prec_pct[i] else ""
                uv       = f"UV {uvs[i]:.0f}" if i < len(uvs) and uvs[i] is not None else ""
                entry    = f"{day_name}: {cond}, high {high}°C low {low}°C"
                if pct:
                    entry += f", {pct}"
                if uv:
                    entry += f", {uv}"
                entries.append(entry)

            result = f"5-day forecast for {name}. " + ". ".join(entries) + "."
            self._cache.set(cache_key, result, TTL_FORECAST)
            return result
        except Exception as e:
            print(f"[weather_handler] forecast error: {e}", flush=True)
            return "I couldn't get the forecast right now, try again in a moment."

    # ── Hourly Forecast (next 12 h) ───────────────────────────────────────────

    def hourly(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want hourly weather for."
        cache_key = f"hourly:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "hourly": ",".join([
                    "temperature_2m", "weather_code",
                    "precipitation_probability", "wind_speed_10m",
                ]),
                "forecast_hours": 13,
            })
            hourly  = data.get("hourly", {})
            times   = hourly.get("time", [])
            temps   = hourly.get("temperature_2m", [])
            codes   = hourly.get("weather_code", [])
            prec    = hourly.get("precipitation_probability", [])
            winds   = hourly.get("wind_speed_10m", [])

            start = self._current_index(times)
            entries = []
            for i in range(start, min(start + 12, len(times))):
                hour_str = datetime.fromisoformat(times[i]).strftime("%I %p").lstrip("0")
                temp     = f"{temps[i]:.0f}°C" if i < len(temps) and temps[i] is not None else ""
                cond     = WMO_CODES.get(codes[i] if i < len(codes) else 0, "")
                rain_pct = f"{prec[i]:.0f}% rain" if i < len(prec) and prec[i] else ""
                entry    = f"{hour_str}: {temp} {cond}"
                if rain_pct:
                    entry += f" ({rain_pct})"
                entries.append(entry)

            name   = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            result = f"Next 12 hours in {name}. " + ". ".join(entries) + "."
            self._cache.set(cache_key, result, TTL_HOURLY)
            return result
        except Exception as e:
            print(f"[weather_handler] hourly error: {e}", flush=True)
            return "I couldn't get hourly weather right now, try again in a moment."

    # ── AQI Summary ───────────────────────────────────────────────────────────

    def aqi(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want the air quality for."
        cache_key = f"aqi:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._aq_get(loc["lat"], loc["lon"], loc["timezone"], {
                "hourly": ",".join([
                    "european_aqi", "us_aqi",
                    "pm2_5", "pm10",
                    "nitrogen_dioxide", "ozone",
                ]),
                "forecast_days": 1,
            })
            h     = data.get("hourly", {})
            times = h.get("time", [])
            idx   = self._current_index(times)

            eu_aqi  = h.get("european_aqi",    [None])[idx] if idx < len(h.get("european_aqi", [])) else None
            us_aqi  = h.get("us_aqi",          [None])[idx] if idx < len(h.get("us_aqi", []))       else None
            pm25    = h.get("pm2_5",            [None])[idx] if idx < len(h.get("pm2_5", []))        else None
            pm10    = h.get("pm10",             [None])[idx] if idx < len(h.get("pm10", []))         else None
            no2     = h.get("nitrogen_dioxide", [None])[idx] if idx < len(h.get("nitrogen_dioxide", [])) else None
            o3      = h.get("ozone",            [None])[idx] if idx < len(h.get("ozone", []))        else None

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]

            parts = [f"Air quality in {name}"]
            if eu_aqi is not None:
                label  = _aqi_label(eu_aqi, EU_AQI_LABELS)
                advice = EU_AQI_ADVICE[_aqi_band(eu_aqi)]
                parts.append(f"European AQI is {eu_aqi:.0f}, which is {label}")
                parts.append(advice)
            if us_aqi is not None:
                label = _aqi_label(us_aqi, US_AQI_LABELS)
                parts.append(f"US AQI is {us_aqi:.0f}, rated {label}")

            # Dominant pollutant
            pollutants = {}
            if pm25  is not None: pollutants["PM2.5"]           = pm25
            if pm10  is not None: pollutants["PM10"]            = pm10
            if no2   is not None: pollutants["nitrogen dioxide"] = no2
            if o3    is not None: pollutants["ozone"]           = o3
            if pollutants:
                dominant = max(pollutants, key=pollutants.get)
                parts.append(f"The dominant pollutant is {dominant}")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_AQI)
            return result
        except Exception as e:
            print(f"[weather_handler] aqi error: {e}", flush=True)
            return "I couldn't get air quality data right now, try again in a moment."

    # ── Full Pollution Breakdown ───────────────────────────────────────────────

    def pollution(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want pollution data for."
        cache_key = f"pollution:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._aq_get(loc["lat"], loc["lon"], loc["timezone"], {
                "hourly": ",".join([
                    "pm2_5", "pm10", "nitrogen_dioxide",
                    "ozone", "sulphur_dioxide", "carbon_monoxide",
                    "nitrogen_monoxide", "dust", "ammonia",
                ]),
                "forecast_days": 1,
            })
            h     = data.get("hourly", {})
            times = h.get("time", [])
            idx   = self._current_index(times)

            def _v(key):
                arr = h.get(key, [])
                return arr[idx] if idx < len(arr) and arr[idx] is not None else None

            pm25 = _v("pm2_5");     pm10 = _v("pm10")
            no2  = _v("nitrogen_dioxide"); o3 = _v("ozone")
            so2  = _v("sulphur_dioxide");  co = _v("carbon_monoxide")
            no   = _v("nitrogen_monoxide"); dust = _v("dust")
            nh3  = _v("ammonia")

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            lines = [f"Pollution breakdown for {name}"]

            if pm25  is not None: lines.append(f"PM2.5 is {pm25:.1f} µg/m³")
            if pm10  is not None: lines.append(f"PM10 is {pm10:.1f} µg/m³")
            if no2   is not None: lines.append(f"Nitrogen dioxide is {no2:.1f} µg/m³")
            if o3    is not None: lines.append(f"Ozone is {o3:.1f} µg/m³")
            if so2   is not None: lines.append(f"Sulphur dioxide is {so2:.1f} µg/m³")
            if co    is not None: lines.append(f"Carbon monoxide is {co:.0f} µg/m³")
            if nh3   is not None: lines.append(f"Ammonia is {nh3:.1f} µg/m³")
            if dust  is not None: lines.append(f"Dust is {dust:.1f} µg/m³")

            if len(lines) == 1:
                return f"I couldn't get pollution data for {city} right now."

            result = ". ".join(lines) + "."
            self._cache.set(cache_key, result, TTL_AQI)
            return result
        except Exception as e:
            print(f"[weather_handler] pollution error: {e}", flush=True)
            return "I couldn't get pollution data right now, try again in a moment."

    # ── Pollen ────────────────────────────────────────────────────────────────

    def pollen(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want pollen info for."
        cache_key = f"pollen:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._aq_get(loc["lat"], loc["lon"], loc["timezone"], {
                "hourly": ",".join([
                    "alder_pollen", "birch_pollen", "grass_pollen",
                    "mugwort_pollen", "olive_pollen", "ragweed_pollen",
                ]),
                "forecast_days": 1,
            })
            h     = data.get("hourly", {})
            times = h.get("time", [])
            idx   = self._current_index(times)

            def _v(key):
                arr = h.get(key, [])
                return arr[idx] if idx < len(arr) and arr[idx] is not None else None

            def _pollen_level(val):
                if val is None: return None
                if val < 10:    return "low"
                if val < 50:    return "moderate"
                if val < 200:   return "high"
                return "very high"

            name   = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            pollen_data = {
                "grass":   _v("grass_pollen"),
                "birch":   _v("birch_pollen"),
                "alder":   _v("alder_pollen"),
                "mugwort": _v("mugwort_pollen"),
                "olive":   _v("olive_pollen"),
                "ragweed": _v("ragweed_pollen"),
            }

            parts = [f"Pollen forecast for {name}"]
            any_found = False
            for ptype, val in pollen_data.items():
                if val is not None:
                    level = _pollen_level(val)
                    parts.append(f"{ptype} pollen is {level} at {val:.0f} grains/m³")
                    any_found = True

            if not any_found:
                return (f"Pollen data is not available for {name}. "
                        "Open-Meteo pollen forecasts cover Europe only.")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_POLLEN)
            return result
        except Exception as e:
            print(f"[weather_handler] pollen error: {e}", flush=True)
            return "I couldn't get pollen data right now. Note: pollen forecasts are only available for Europe."

    # ── UV Index ──────────────────────────────────────────────────────────────

    def uv(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want UV info for."
        cache_key = f"uv:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "hourly": "uv_index,uv_index_clear_sky",
                "daily":  "uv_index_max",
                "current": "uv_index",
                "forecast_days": 1,
            })
            current_uv = (data.get("current") or {}).get("uv_index")
            daily_max  = ((data.get("daily") or {}).get("uv_index_max") or [None])[0]

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]

            if current_uv is None and daily_max is None:
                return f"I couldn't get UV data for {name} right now."

            parts = [f"UV index for {name}"]
            if current_uv is not None:
                label  = _uv_label(current_uv)
                advice = _uv_advice(current_uv)
                parts.append(f"currently {current_uv:.1f}, rated {label}")
                parts.append(advice)
            if daily_max is not None:
                parts.append(f"today's maximum UV index will be {daily_max:.1f}")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_CURRENT)
            return result
        except Exception as e:
            print(f"[weather_handler] uv error: {e}", flush=True)
            return "I couldn't get UV data right now, try again in a moment."

    # ── Wind ──────────────────────────────────────────────────────────────────

    def wind(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want wind info for."
        cache_key = f"wind:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "daily":   "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant",
                "forecast_days": 1,
            })
            c     = data.get("current", {})
            spd   = c.get("wind_speed_10m")
            dirn  = c.get("wind_direction_10m")
            gusts = c.get("wind_gusts_10m")
            d_max_spd  = ((data.get("daily") or {}).get("wind_speed_10m_max") or [None])[0]
            d_max_gust = ((data.get("daily") or {}).get("wind_gusts_10m_max") or [None])[0]
            compass    = _degrees_to_compass(dirn) if dirn is not None else "unknown direction"

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            parts = [f"Wind conditions in {name}"]
            if spd is not None:
                parts.append(f"currently blowing {spd:.0f} km/h from the {compass}")
            if gusts is not None and spd is not None and gusts > spd + 5:
                parts.append(f"with gusts up to {gusts:.0f} km/h")
            if d_max_spd is not None:
                parts.append(f"today's maximum wind speed will be {d_max_spd:.0f} km/h")
            if d_max_gust is not None:
                parts.append(f"with gusts reaching {d_max_gust:.0f} km/h")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_CURRENT)
            return result
        except Exception as e:
            print(f"[weather_handler] wind error: {e}", flush=True)
            return "I couldn't get wind data right now, try again in a moment."

    # ── Humidity ──────────────────────────────────────────────────────────────

    def humidity(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want humidity info for."
        cache_key = f"humidity:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "current": ",".join([
                    "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
                    "temperature_2m",
                ]),
            })
            c        = data.get("current", {})
            humidity = c.get("relative_humidity_2m")
            dewpoint = c.get("dew_point_2m")
            feels    = c.get("apparent_temperature")
            temp     = c.get("temperature_2m")

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            parts = [f"Humidity in {name}"]
            if humidity is not None:
                comfort = ("comfortable" if 30 <= humidity <= 60
                           else "dry" if humidity < 30
                           else "humid")
                parts.append(f"relative humidity is {humidity}%, which feels {comfort}")
            if dewpoint is not None:
                parts.append(f"the dew point is {dewpoint:.1f}°C")
            if feels is not None and temp is not None:
                diff = feels - temp
                if abs(diff) > 1:
                    direction = "warmer" if diff > 0 else "cooler"
                    parts.append(f"it feels {abs(diff):.0f}°C {direction} than the actual {temp:.0f}°C due to humidity and wind")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_CURRENT)
            return result
        except Exception as e:
            print(f"[weather_handler] humidity error: {e}", flush=True)
            return "I couldn't get humidity data right now, try again in a moment."

    # ── Rain / Precipitation ──────────────────────────────────────────────────

    def rain(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want rain info for."
        cache_key = f"rain:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "current": "precipitation,rain,snowfall,weather_code",
                "daily":   ",".join([
                    "precipitation_sum", "precipitation_probability_max",
                    "rain_sum", "snowfall_sum",
                ]),
                "forecast_days": 3,
            })
            c        = data.get("current", {})
            curr_p   = c.get("precipitation", 0)
            curr_wmo = c.get("weather_code", 0)

            daily    = data.get("daily", {})
            dates    = daily.get("time", [])
            p_sums   = daily.get("precipitation_sum", [])
            p_probs  = daily.get("precipitation_probability_max", [])
            r_sums   = daily.get("rain_sum", [])
            s_sums   = daily.get("snowfall_sum", [])

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            parts = [f"Rain and precipitation for {name}"]

            if curr_p and curr_p > 0:
                cond = WMO_CODES.get(curr_wmo, "precipitation")
                parts.append(f"currently {cond} with {curr_p:.1f} mm this hour")
            else:
                parts.append("no precipitation currently")

            for i in range(min(3, len(dates))):
                day_name = datetime.fromisoformat(dates[i]).strftime("%A")
                prob     = p_probs[i] if i < len(p_probs) and p_probs[i] is not None else 0
                total    = p_sums[i]  if i < len(p_sums)  and p_sums[i]  is not None else 0
                snow     = s_sums[i]  if i < len(s_sums)  and s_sums[i]  is not None else 0
                entry    = f"{day_name}: {prob:.0f}% chance of rain"
                if total > 0:
                    entry += f", {total:.1f} mm expected"
                if snow > 0:
                    entry += f", {snow:.1f} cm of snow"
                parts.append(entry)

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_HOURLY)
            return result
        except Exception as e:
            print(f"[weather_handler] rain error: {e}", flush=True)
            return "I couldn't get precipitation data right now, try again in a moment."

    # ── Sunrise / Sunset ──────────────────────────────────────────────────────

    def sunrise(self, city: str) -> str:
        if not city:
            return "Please tell me which city you want sunrise info for."
        cache_key = f"sunrise:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        try:
            data = self._forecast_get(loc["lat"], loc["lon"], loc["timezone"], {
                "daily": "sunrise,sunset,daylight_duration,sunshine_duration",
                "forecast_days": 2,
            })
            daily    = data.get("daily", {})
            dates    = daily.get("time", [])
            sunrises = daily.get("sunrise", [])
            sunsets  = daily.get("sunset", [])
            daylight = daily.get("daylight_duration", [])
            sunshine = daily.get("sunshine_duration", [])

            if not sunrises:
                return f"I couldn't get sunrise data for {city}."

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            parts = [f"Sun times for {name}"]

            for i in range(min(2, len(dates))):
                day_name = "Today" if i == 0 else "Tomorrow"
                sr = datetime.fromisoformat(sunrises[i]).strftime("%I:%M %p").lstrip("0") if i < len(sunrises) else "unknown"
                ss = datetime.fromisoformat(sunsets[i]).strftime("%I:%M %p").lstrip("0")  if i < len(sunsets) else "unknown"
                entry = f"{day_name}: sunrise at {sr}, sunset at {ss}"
                if i < len(daylight) and daylight[i] is not None:
                    hours = daylight[i] / 3600
                    entry += f", {hours:.1f} hours of daylight"
                parts.append(entry)

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_FORECAST)
            return result
        except Exception as e:
            print(f"[weather_handler] sunrise error: {e}", flush=True)
            return "I couldn't get sunrise data right now, try again in a moment."

    # ─────────────────────────────────────────────────────────────────────────
    # OpenAQ — Real Sensor Data
    # ─────────────────────────────────────────────────────────────────────────

    def _openaq_check_key(self) -> bool:
        key = self._cfg("openaq", "api_key")
        if not key:
            return False
        return True

    def _openaq_nearest_location_id(self, lat: float, lon: float) -> Optional[int]:
        """Find nearest OpenAQ v3 location ID within 25km."""
        cache_key = f"openaq_loc:{lat:.3f},{lon:.3f}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            r = requests.get(
                f"{OPENAQ_BASE}/locations",
                headers=self._openaq_headers(),
                params={
                    "coordinates": f"{lat},{lon}",
                    "radius":      25000,
                    "limit":       1,
                },
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                return None
            loc_id = results[0].get("id")
            self._cache.set(cache_key, loc_id, TTL_SENSORS)
            return loc_id
        except Exception as e:
            print(f"[weather_handler] openaq location lookup error: {e}", flush=True)
            return None

    def sensors(self, city: str) -> str:
        """List nearest OpenAQ sensor stations."""
        if not city:
            return "Please tell me which city you want sensor info for."
        if not self._openaq_check_key():
            return "OpenAQ real-time sensor data requires an API key. Please add your OpenAQ key to config.json under openaq.api_key."

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        cache_key = f"sensors:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        try:
            r = requests.get(
                f"{OPENAQ_BASE}/locations",
                headers=self._openaq_headers(),
                params={
                    "coordinates": f"{loc['lat']},{loc['lon']}",
                    "radius":      25000,
                    "limit":       5,
                },
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])

            if not results:
                name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
                return f"No OpenAQ sensor stations found within 25 km of {name}."

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            entries = []
            for station in results[:5]:
                s_name   = station.get("name") or f"Station {station.get('id')}"
                sensors  = station.get("sensors", [])
                params   = list({s.get("parameter", {}).get("name", "") for s in sensors if s.get("parameter")})
                params   = [p for p in params if p][:4]
                param_str = ", ".join(params) if params else "unknown parameters"
                entries.append(f"{s_name} measuring {param_str}")

            result = f"OpenAQ stations near {name}: " + ". ".join(entries) + "."
            self._cache.set(cache_key, result, TTL_SENSORS)
            return result
        except Exception as e:
            print(f"[weather_handler] sensors error: {e}", flush=True)
            return "I couldn't get sensor station data right now, try again in a moment."

    def realtime(self, city: str) -> str:
        """Latest real-time readings from nearest OpenAQ sensor station."""
        if not city:
            return "Please tell me which city you want real-time air data for."
        if not self._openaq_check_key():
            return "OpenAQ real-time sensor data requires an API key. Please add your OpenAQ key to config.json under openaq.api_key."

        loc = self._resolve(city)
        if not loc:
            return f"I couldn't find {city}. Please check the city name."

        cache_key = f"realtime:{city.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        loc_id = self._openaq_nearest_location_id(loc["lat"], loc["lon"])
        if not loc_id:
            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            return f"No OpenAQ sensor station found within 25 km of {name}."

        try:
            r = requests.get(
                f"{OPENAQ_BASE}/locations/{loc_id}/latest",
                headers=self._openaq_headers(),
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])

            if not results:
                return "The nearest sensor station has no recent readings right now."

            # Also fetch the station name
            station_name = f"Station {loc_id}"
            try:
                sr = requests.get(
                    f"{OPENAQ_BASE}/locations/{loc_id}",
                    headers=self._openaq_headers(),
                    timeout=8,
                )
                if sr.status_code == 200:
                    station_data = sr.json().get("results", [{}])[0]
                    station_name = station_data.get("name") or station_name
            except Exception:
                pass

            name = f"{loc['name']}, {loc['country']}" if loc["country"] else loc["name"]
            readings = []
            for item in results[:8]:
                value   = item.get("value")
                sensors_id = item.get("sensorsId")
                # We need to look up what this sensor measures
                # The latest endpoint doesn't return parameter name directly,
                # so we report value + sensor ID
                if value is not None:
                    readings.append(f"sensor {sensors_id}: {value:.2f}")

            if not readings:
                return f"No valid readings from the nearest station near {name}."

            result = (f"Latest real-time readings from {station_name} near {name}: "
                      + ", ".join(readings) + ". "
                      "For parameter names, use the SENSORS command to identify which sensor measures what.")
            self._cache.set(cache_key, result, TTL_REALTIME)
            return result
        except Exception as e:
            print(f"[weather_handler] realtime error: {e}", flush=True)
            return "I couldn't get real-time sensor data right now, try again in a moment."

    # ─────────────────────────────────────────────────────────────────────────
    # Dispatcher
    # ─────────────────────────────────────────────────────────────────────────

    def dispatch(self, tag: str) -> str:
        """
        Parse a [CMD:WEATHER:<ACTION>:<city>] tag and call the right method.
        Returns a TTS-ready string. Never raises.
        """
        try:
            inner = tag.strip()
            if inner.startswith("[") and inner.endswith("]"):
                inner = inner[1:-1]
            parts = inner.split(":")

            if len(parts) < 3 or parts[0] != "CMD" or parts[1] != "WEATHER":
                return "I didn't understand that weather command."

            action = parts[2].upper()
            city   = ":".join(parts[3:]).strip() if len(parts) > 3 else ""

            dispatch_map = {
                "CURRENT":   self.current,
                "FORECAST":  self.forecast,
                "HOURLY":    self.hourly,
                "AQI":       self.aqi,
                "POLLUTION": self.pollution,
                "POLLEN":    self.pollen,
                "UV":        self.uv,
                "WIND":      self.wind,
                "HUMIDITY":  self.humidity,
                "RAIN":      self.rain,
                "SUNRISE":   self.sunrise,
                "SENSORS":   self.sensors,
                "REALTIME":  self.realtime,
            }

            handler = dispatch_map.get(action)
            if not handler:
                return f"I don't know how to handle the weather command {action}."

            return handler(city)

        except Exception as e:
            print(f"[weather_handler] dispatch error: {e}", flush=True)
            return "I ran into a problem with that weather request."


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    manager = WeatherManager()

    tests = [
        ("current Delhi",                  lambda: manager.current("Delhi")),
        ("forecast London",                lambda: manager.forecast("London")),
        ("hourly Tokyo",                   lambda: manager.hourly("Tokyo")),
        ("aqi Beijing",                    lambda: manager.aqi("Beijing")),
        ("pollution Mumbai",               lambda: manager.pollution("Mumbai")),
        ("uv Sydney",                      lambda: manager.uv("Sydney")),
        ("wind Chicago",                   lambda: manager.wind("Chicago")),
        ("humidity Singapore",             lambda: manager.humidity("Singapore")),
        ("rain New York",                  lambda: manager.rain("New York")),
        ("sunrise Paris",                  lambda: manager.sunrise("Paris")),
        ("pollen Berlin",                  lambda: manager.pollen("Berlin")),
        ("dispatch CURRENT Dubai",         lambda: manager.dispatch("[CMD:WEATHER:CURRENT:Dubai]")),
        ("dispatch FORECAST Cairo",        lambda: manager.dispatch("[CMD:WEATHER:FORECAST:Cairo]")),
        ("dispatch AQI Seoul",             lambda: manager.dispatch("[CMD:WEATHER:AQI:Seoul]")),
        ("dispatch UV Los Angeles",        lambda: manager.dispatch("[CMD:WEATHER:UV:Los Angeles]")),
        ("dispatch RAIN Bangkok",          lambda: manager.dispatch("[CMD:WEATHER:RAIN:Bangkok]")),
        ("dispatch SUNRISE Istanbul",      lambda: manager.dispatch("[CMD:WEATHER:SUNRISE:Istanbul]")),
        ("sensors Delhi (needs key)",      lambda: manager.sensors("Delhi")),
        ("realtime Delhi (needs key)",     lambda: manager.realtime("Delhi")),
    ]

    for label, test in tests:
        print(f"\n--- {label} ---")
        try:
            result = test()
            print(result)
        except Exception as e:
            print(f"FAILED: {e}")
        time.sleep(0.6)