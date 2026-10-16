"""Fast-path weather city extraction ("weather in Mumbai today" -> "Mumbai")."""
import pytest

from veronica.tools.weather_tools import WeatherTool


@pytest.mark.parametrize("text,city", [
    ("how is the weather in Mumbai today", "Mumbai"),
    ("weather in New York right now", "New York"),
    ("what's the weather in Pune tomorrow please", "Pune"),
    ("weather in Delhi", "Delhi"),
    ("weather in Delhi?", "Delhi"),
    ("weather for Nowshera", "Nowshera"),  # a city that merely starts with "Now"
])
def test_city_ignores_trailing_time_words(text, city):
    assert WeatherTool().extract_arguments(text) == {"city": city}
