"""
Weather via Open-Meteo — chosen deliberately over OpenWeatherMap (used by Prodigy-AI and
J.A.R.V.I.S) because it requires NO API key at all, matching VERONICA's "no paid/required
key" default. If the user has an OPENWEATHER_API_KEY configured, this tool could be
extended to prefer it; left as a documented extension point rather than built in, to keep
the default path key-free.
"""
from __future__ import annotations

import re
from typing import Any

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.weather")

_WEATHER_PATTERN = re.compile(r"\bweather\s+(?:in|at|for)\s+([a-zA-Z\s]+)", re.I)
# Time words people add after the city ("weather in Mumbai today") are not part of it.
_TRAILING_TIME_WORDS = re.compile(
    r"(?:\s+(?:today|tonight|now|right now|currently|tomorrow|this (?:morning|afternoon|evening|week)"
    r"|please|like))+\s*$", re.I)

# Whole short questions only, so "the temperature of the sun" stays with the LLM.
_HOME_WEATHER = re.compile(
    r"^\s*(?:(?:what|how)(?:'s|\s+is)\s+|tell\s+me\s+|check\s+)?(?:the\s+)?(?:weather|temperature|forecast)"
    r"(?:\s+like)?(?:\s+(?:today|now|right\s+now|outside|tonight|tomorrow))?\s*[?.!]?\s*$|"
    r"^\s*(?:is|will)\s+it\s+(?:going\s+to\s+)?(?:rain|be\s+(?:hot|cold|sunny))(?:\s+(?:today|tonight|tomorrow))?\s*[?.!]?\s*$",
    re.I)

# The complete WMO weather-code table Open-Meteo uses (missing codes made the weather
# report say "unknown conditions" during moderate drizzle and heavy showers).
_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


class WeatherTool(Tool):
    name = "get_weather"
    description = ("Get current weather for a city, using a free keyless weather API. "
                   "Omit `city` to use the user's home city.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name; omit for the user's home city."}},
        "required": [],
    }

    def __init__(self, config=None, profile=None) -> None:
        from veronica.tools.productivity_tools import Profile
        self.config = config
        self.profile = profile or Profile(config)

    def matches(self, text: str) -> bool:
        if _WEATHER_PATTERN.search(text):
            return True
        # "how's the weather (today)?" — only fast-path when a home city is known.
        return bool(_HOME_WEATHER.search(text)) and bool(self.profile.home_city())

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _WEATHER_PATTERN.search(text)
        if not m:
            return {}
        return {"city": _TRAILING_TIME_WORDS.sub("", m.group(1).strip().rstrip("?")).strip()}

    def run(self, city: str = "", **kwargs: Any) -> ToolResult:
        named = (city or "").strip()
        city = named or (self.profile.home_city() or "")
        if not city:
            return ToolResult.fail("Which city? You can say 'my city is Mumbai' and I'll remember it.")
        notice = "" if named else self.profile.detection_notice()
        try:
            import requests
        except ImportError:
            return ToolResult.fail("requests not installed; run `pip install requests`.")

        try:
            coords = None if named else self.profile.detected_coords()
            if coords:  # Windows location: weather for exactly where the user is
                (lat, lon), resolved_name = coords, city
            else:
                geo = requests.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city.split(",")[0], "count": 1},
                    timeout=8,
                ).json()
                results = geo.get("results")
                if not results:
                    return ToolResult.fail(f"Couldn't find a location called '{city}'.")
                lat, lon = results[0]["latitude"], results[0]["longitude"]
                resolved_name = results[0].get("name", city)

            wx = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon, "current_weather": True, "timezone": "auto",
                        "forecast_days": 1,
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"},
                timeout=8,
            ).json()
            current = wx.get("current_weather")
            if not current:
                return ToolResult.fail(f"Weather data unavailable for {resolved_name}.")

            temp = current["temperature"]
            wind = current["windspeed"]
            code = current.get("weathercode", -1)
            desc = _WEATHER_CODES.get(code, "unknown conditions")
            today = ""
            daily = wx.get("daily") or {}
            try:
                hi, lo = daily["temperature_2m_max"][0], daily["temperature_2m_min"][0]
                rain = (daily.get("precipitation_probability_max") or [None])[0]
                today = f" Today: high {round(hi)}°, low {round(lo)}°" + \
                    (f", {rain}% chance of rain." if rain is not None else ".")
            except (KeyError, IndexError, TypeError):
                pass  # forecast is a bonus; current conditions are the answer
            return ToolResult.ok(
                f"{resolved_name}: {temp}°C, {desc}, wind {wind} km/h.{today}{notice}",
                city=resolved_name, temperature_c=temp, description=desc, wind_kmh=wind,
            )
        except requests.RequestException as e:
            logger.warning("Weather request failed: %s", e)
            return ToolResult.fail(f"Couldn't reach the weather service: {e}")
