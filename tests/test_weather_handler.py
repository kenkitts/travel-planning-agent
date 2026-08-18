"""Unit tests for lambdas/weather/handler.py.

All Open-Meteo HTTP calls are mocked — no live network access.
"""
import importlib.util
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[1] / "lambdas" / "weather" / "handler.py"
_spec = importlib.util.spec_from_file_location("weather_handler", _HANDLER_PATH)
weather_handler = importlib.util.module_from_spec(_spec)
sys.modules["weather_handler"] = weather_handler
_spec.loader.exec_module(weather_handler)


def _mock_response(payload: dict):
    """Build a mock object usable as a `with urllib.request.urlopen(...) as resp` context manager."""
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return mock_cm


class GeocodeLocationTests(unittest.TestCase):
    @patch("weather_handler.urllib.request.urlopen")
    def test_geocode_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {
                "results": [
                    {
                        "latitude": 35.0116,
                        "longitude": 135.7681,
                        "name": "Kyoto",
                        "admin1": "Kyoto Prefecture",
                        "country": "Japan",
                        "timezone": "Asia/Tokyo",
                    }
                ]
            }
        )

        result = weather_handler.geocode_location("Kyoto, Japan")

        self.assertEqual(result["latitude"], 35.0116)
        self.assertEqual(result["longitude"], 135.7681)
        self.assertEqual(result["resolved_name"], "Kyoto, Kyoto Prefecture, Japan")
        self.assertEqual(result["timezone"], "Asia/Tokyo")

    @patch("weather_handler.urllib.request.urlopen")
    def test_geocode_no_results_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"results": []})

        with self.assertRaises(weather_handler.WeatherToolError):
            weather_handler.geocode_location("Nowhereville")

    @patch("weather_handler.urllib.request.urlopen")
    def test_geocode_http_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="x", code=500, msg="Internal Server Error", hdrs=None, fp=None
        )

        with self.assertRaises(weather_handler.WeatherToolError):
            weather_handler.geocode_location("Kyoto")


class FetchDailyForecastTests(unittest.TestCase):
    @patch("weather_handler.urllib.request.urlopen")
    def test_fetch_forecast_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {
                "timezone": "Asia/Tokyo",
                "daily": {
                    "time": ["2026-10-03"],
                    "temperature_2m_max": [24.1],
                    "temperature_2m_min": [16.8],
                    "precipitation_probability_max": [20],
                    "precipitation_sum": [0.0],
                    "weather_code": [2],
                },
            }
        )

        result = weather_handler.fetch_daily_forecast(35.0116, 135.7681, "2026-10-03", "2026-10-03")

        self.assertEqual(result["timezone"], "Asia/Tokyo")
        self.assertIn("daily", result)

    @patch("weather_handler.urllib.request.urlopen")
    def test_fetch_forecast_api_error_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {"error": True, "reason": "Invalid date range"}
        )

        with self.assertRaises(weather_handler.WeatherToolError):
            weather_handler.fetch_daily_forecast(35.0116, 135.7681, "bad", "bad")

    @patch("weather_handler.urllib.request.urlopen")
    def test_fetch_forecast_missing_daily_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"timezone": "Asia/Tokyo"})

        with self.assertRaises(weather_handler.WeatherToolError):
            weather_handler.fetch_daily_forecast(35.0116, 135.7681, "2026-10-03", "2026-10-03")


class NormalizeDailyTests(unittest.TestCase):
    def test_normalize_daily_maps_wmo_code_to_condition(self):
        daily = {
            "time": ["2026-10-03", "2026-10-04"],
            "temperature_2m_max": [24.1, 20.0],
            "temperature_2m_min": [16.8, 15.0],
            "precipitation_probability_max": [20, 80],
            "precipitation_sum": [0.0, 5.5],
            "weather_code": [0, 61],
        }

        result = weather_handler._normalize_daily(daily)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["condition"], "Clear sky")
        self.assertEqual(result[1]["condition"], "Slight rain")
        self.assertEqual(result[1]["precipitation_sum_mm"], 5.5)

    def test_normalize_daily_unknown_code(self):
        daily = {
            "time": ["2026-10-03"],
            "temperature_2m_max": [24.1],
            "temperature_2m_min": [16.8],
            "precipitation_probability_max": [20],
            "precipitation_sum": [0.0],
            "weather_code": [999],
        }

        result = weather_handler._normalize_daily(daily)

        self.assertEqual(result[0]["condition"], "Unknown")


class GetWeatherForecastTests(unittest.TestCase):
    @patch("weather_handler.fetch_daily_forecast")
    @patch("weather_handler.geocode_location")
    def test_get_forecast_with_location_name(self, mock_geocode, mock_fetch):
        mock_geocode.return_value = {
            "latitude": 35.0116,
            "longitude": 135.7681,
            "resolved_name": "Kyoto, Japan",
            "timezone": "Asia/Tokyo",
        }
        mock_fetch.return_value = {
            "timezone": "Asia/Tokyo",
            "daily": {
                "time": ["2026-10-03"],
                "temperature_2m_max": [24.1],
                "temperature_2m_min": [16.8],
                "precipitation_probability_max": [20],
                "precipitation_sum": [0.0],
                "weather_code": [0],
            },
        }

        result = weather_handler.get_weather_forecast(
            start_date="2026-10-03", end_date="2026-10-03", location="Kyoto, Japan"
        )

        mock_geocode.assert_called_once_with("Kyoto, Japan")
        self.assertEqual(result["location"], "Kyoto, Japan")
        self.assertEqual(len(result["daily"]), 1)

    @patch("weather_handler.fetch_daily_forecast")
    @patch("weather_handler.geocode_location")
    def test_get_forecast_with_explicit_coordinates_skips_geocoding(
        self, mock_geocode, mock_fetch
    ):
        mock_fetch.return_value = {
            "timezone": "Asia/Tokyo",
            "daily": {
                "time": ["2026-10-03"],
                "temperature_2m_max": [24.1],
                "temperature_2m_min": [16.8],
                "precipitation_probability_max": [20],
                "precipitation_sum": [0.0],
                "weather_code": [0],
            },
        }

        result = weather_handler.get_weather_forecast(
            start_date="2026-10-03",
            end_date="2026-10-03",
            latitude=35.0116,
            longitude=135.7681,
        )

        mock_geocode.assert_not_called()
        self.assertEqual(result["latitude"], 35.0116)

    def test_get_forecast_missing_location_and_coordinates_raises(self):
        with self.assertRaises(weather_handler.WeatherToolError):
            weather_handler.get_weather_forecast(start_date="2026-10-03", end_date="2026-10-03")


class HandlerEntrypointTests(unittest.TestCase):
    @patch("weather_handler.get_weather_forecast")
    def test_handler_success(self, mock_get_forecast):
        mock_get_forecast.return_value = {"location": "Kyoto, Japan", "daily": []}

        event = {
            "location": "Kyoto, Japan",
            "start_date": "2026-10-03",
            "end_date": "2026-10-10",
        }
        result = weather_handler.handler(event)

        self.assertEqual(result["location"], "Kyoto, Japan")

    def test_handler_missing_dates_returns_error(self):
        result = weather_handler.handler({"location": "Kyoto, Japan"})

        self.assertIn("error", result)

    @patch("weather_handler.get_weather_forecast")
    def test_handler_tool_error_returns_error_dict(self, mock_get_forecast):
        mock_get_forecast.side_effect = weather_handler.WeatherToolError("boom")

        result = weather_handler.handler(
            {"location": "Nowhere", "start_date": "2026-10-03", "end_date": "2026-10-10"}
        )

        self.assertEqual(result, {"error": "boom"})

    @patch("weather_handler.get_weather_forecast")
    def test_handler_unexpected_error_is_caught(self, mock_get_forecast):
        mock_get_forecast.side_effect = RuntimeError("kaboom")

        result = weather_handler.handler(
            {"location": "Kyoto", "start_date": "2026-10-03", "end_date": "2026-10-10"}
        )

        self.assertIn("error", result)
        self.assertIn("kaboom", result["error"])


if __name__ == "__main__":
    unittest.main()
