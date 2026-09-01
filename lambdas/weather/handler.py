"""Weather tool Lambda handler.

Wraps the Open-Meteo API (https://open-meteo.com/) to provide daily weather
forecasts for a named location or explicit coordinates, over a date range.
Used as an AgentCore Gateway Lambda target so the travel planning agent can
ground itinerary suggestions in real forecast data.

No API key is required for Open-Meteo's free tier.

Logging: structured logger.info()/logger.error() calls at the request,
upstream-HTTP-call, and response boundaries, matching agent.py's style —
added as part of the observability pass (see DESIGN.md). Prior to this, the
handler emitted nothing beyond Lambda's own automatic invocation/error/
duration log lines, making a bad Open-Meteo response or geocoding miss
indistinguishable from any other failure without re-deriving it from the
Gateway's own opaque error message.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DAILY_VARIABLES = ",".join(
    [
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_probability_max",
        "precipitation_sum",
        "weather_code",
    ]
)

REQUEST_TIMEOUT_SECONDS = 10

# WMO weather interpretation codes -> human-readable condition.
# https://open-meteo.com/en/docs (Weather variable documentation)
WMO_CODE_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherToolError(Exception):
    """Raised for any handled failure while fulfilling a weather request."""


def _http_get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(full_url, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        logger.error(
            "Open-Meteo call failed: url=%s HTTP %s (%.0fms elapsed)",
            url, e.code, (time.monotonic() - started) * 1000,
        )
        raise WeatherToolError(f"Weather API request failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        logger.error(
            "Open-Meteo call failed: url=%s reason=%s (%.0fms elapsed)",
            url, e.reason, (time.monotonic() - started) * 1000,
        )
        raise WeatherToolError(f"Weather API request failed: {e.reason}") from e

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info("Open-Meteo call succeeded: url=%s (%.0fms elapsed)", url, elapsed_ms)

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        logger.error("Open-Meteo returned invalid JSON: url=%s", url)
        raise WeatherToolError("Weather API returned invalid JSON") from e


def geocode_location(location: str) -> dict:
    """Resolve a place name to coordinates using Open-Meteo's Geocoding API.

    Returns a dict with latitude, longitude, and a resolved display name.
    Raises WeatherToolError if no match is found.
    """
    data = _http_get_json(GEOCODING_URL, {"name": location, "count": 1})
    results = data.get("results") or []
    if not results:
        logger.warning("Geocoding miss for location=%r", location)
        raise WeatherToolError(f"Could not find a location matching '{location}'")

    top = results[0]
    name_parts = [top.get("name")]
    if top.get("admin1"):
        name_parts.append(top["admin1"])
    if top.get("country"):
        name_parts.append(top["country"])

    return {
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "resolved_name": ", ".join(p for p in name_parts if p),
        "timezone": top.get("timezone"),
    }


def fetch_daily_forecast(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
) -> dict:
    """Fetch the daily forecast for a coordinate and date range."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": DAILY_VARIABLES,
        "timezone": "auto",
        "start_date": start_date,
        "end_date": end_date,
    }
    data = _http_get_json(FORECAST_URL, params)

    if "error" in data and data["error"]:
        reason = data.get("reason", "unknown error")
        logger.error("Open-Meteo forecast error: reason=%s", reason)
        raise WeatherToolError(f"Weather API error: {reason}")

    daily = data.get("daily")
    if not daily:
        logger.error("Open-Meteo forecast response missing 'daily' key")
        raise WeatherToolError("Weather API response missing daily forecast data")

    return data


def _normalize_daily(daily: dict) -> list[dict]:
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    precip_prob = daily.get("precipitation_probability_max", [])
    precip_sum = daily.get("precipitation_sum", [])
    codes = daily.get("weather_code", [])

    days = []
    for i, date in enumerate(dates):
        code = codes[i] if i < len(codes) else None
        days.append(
            {
                "date": date,
                "temperature_max_c": tmax[i] if i < len(tmax) else None,
                "temperature_min_c": tmin[i] if i < len(tmin) else None,
                "precipitation_probability_max_pct": (
                    precip_prob[i] if i < len(precip_prob) else None
                ),
                "precipitation_sum_mm": precip_sum[i] if i < len(precip_sum) else None,
                "condition": WMO_CODE_DESCRIPTIONS.get(code, "Unknown"),
            }
        )
    return days


def get_weather_forecast(
    start_date: str,
    end_date: str,
    location: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> dict:
    """Core tool logic: resolve location (if needed) and fetch the forecast.

    Either `location` or both `latitude` and `longitude` must be provided.
    """
    if latitude is not None and longitude is not None:
        resolved_name = location or f"{latitude},{longitude}"
        timezone = None
    elif location:
        geo = geocode_location(location)
        latitude = geo["latitude"]
        longitude = geo["longitude"]
        resolved_name = geo["resolved_name"]
        timezone = geo.get("timezone")
    else:
        raise WeatherToolError(
            "Either 'location' or both 'latitude' and 'longitude' must be provided"
        )

    forecast = fetch_daily_forecast(latitude, longitude, start_date, end_date)

    return {
        "location": resolved_name,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": forecast.get("timezone", timezone),
        "daily": _normalize_daily(forecast["daily"]),
    }


def handler(event: dict, context: Any = None) -> dict:
    """Lambda entrypoint for the AgentCore Gateway Lambda target.

    Expected event shape (per tool_schema.json):
        {
          "location": "Kyoto, Japan",       # optional if lat/lon given
          "latitude": 35.0116,              # optional
          "longitude": 135.7681,            # optional
          "start_date": "2026-10-03",
          "end_date": "2026-10-10"
        }
    """
    try:
        location = event.get("location")
        latitude = event.get("latitude")
        longitude = event.get("longitude")
        start_date = event.get("start_date")
        end_date = event.get("end_date")

        logger.info(
            "Weather tool request: location=%r latitude=%r longitude=%r "
            "start_date=%r end_date=%r",
            location, latitude, longitude, start_date, end_date,
        )

        if not start_date or not end_date:
            raise WeatherToolError("Both 'start_date' and 'end_date' are required")

        result = get_weather_forecast(
            start_date=start_date,
            end_date=end_date,
            location=location,
            latitude=latitude,
            longitude=longitude,
        )
        logger.info(
            "Weather tool response: resolved_location=%r day_count=%d",
            result.get("location"), len(result.get("daily", [])),
        )
        return result
    except WeatherToolError as e:
        logger.warning("Weather tool request failed: %s", e)
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - convert any unexpected error to a tool-safe response
        logger.exception("Weather tool request failed with an unexpected error")
        return {"error": f"Unexpected error: {e}"}
