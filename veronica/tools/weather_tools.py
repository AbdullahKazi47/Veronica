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

_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 61: "light rain", 63: "moderate rain", 65: "heavy rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow",
    80: "rain showers", 95: "thunderstorm",
}


class WeatherTool(Tool):
    name = "get_weather"
    description = "Get current weather for a city, using a free keyless weather API."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name."}},
        "required": ["city"],
    }

    def __init__(self, config=None) -> None:
        self.config = config

    def matches(self, text: str) -> bool:
        return bool(_WEATHER_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _WEATHER_PATTERN.search(text)
        return {"city": m.group(1).strip().rstrip("?")} if m else {}

    def run(self, city: str = "", **kwargs: Any) -> ToolResult:
        if not city:
            return ToolResult.fail("No city given.")
        try:
            import requests
        except ImportError:
            return ToolResult.fail("requests not installed; run `pip install requests`.")

        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1},
                timeout=8,
            ).json()
            results = geo.get("results")
            if not results:
                return ToolResult.fail(f"Couldn't find a location called '{city}'.")
            lat, lon = results[0]["latitude"], results[0]["longitude"]
            resolved_name = results[0].get("name", city)

            wx = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon, "current_weather": True},
                timeout=8,
            ).json()
            current = wx.get("current_weather")
            if not current:
                return ToolResult.fail(f"Weather data unavailable for {resolved_name}.")

            temp = current["temperature"]
            wind = current["windspeed"]
            code = current.get("weathercode", -1)
            desc = _WEATHER_CODES.get(code, "unknown conditions")
            return ToolResult.ok(
                f"{resolved_name}: {temp}°C, {desc}, wind {wind} km/h.",
                city=resolved_name, temperature_c=temp, description=desc, wind_kmh=wind,
            )
        except requests.RequestException as e:
            logger.warning("Weather request failed: %s", e)
            return ToolResult.fail(f"Couldn't reach the weather service: {e}")
