"""Unit tests for lambdas/places/handler.py.

All boto3 'geo-places' client calls are mocked — no live AWS calls.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[1] / "lambdas" / "places" / "handler.py"
_spec = importlib.util.spec_from_file_location("places_handler", _HANDLER_PATH)
places_handler = importlib.util.module_from_spec(_spec)
sys.modules["places_handler"] = places_handler
_spec.loader.exec_module(places_handler)


class ExtractPlaceTests(unittest.TestCase):
    def test_extract_place_with_list_place_type(self):
        item = {
            "Title": "Fushimi Inari Shrine",
            "Address": {"Label": "68 Fukakusa Yabunouchicho, Kyoto, Japan"},
            "PlaceType": ["PointOfInterest"],
            "Position": [135.7727, 34.9671],
        }

        result = places_handler._extract_place(item)

        self.assertEqual(result["name"], "Fushimi Inari Shrine")
        self.assertEqual(result["category"], "PointOfInterest")
        self.assertEqual(result["latitude"], 34.9671)
        self.assertEqual(result["longitude"], 135.7727)

    def test_extract_place_with_missing_fields(self):
        item = {"Title": "Unknown Place"}

        result = places_handler._extract_place(item)

        self.assertEqual(result["name"], "Unknown Place")
        self.assertIsNone(result["address"])
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["longitude"])


class SearchPlacesTests(unittest.TestCase):
    @patch("places_handler._get_geo_places_client")
    def test_search_places_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search_text.return_value = {
            "ResultItems": [
                {
                    "Title": "Kinkaku-ji",
                    "Address": {"Label": "1 Kinkakujicho, Kyoto, Japan"},
                    "PlaceType": "PointOfInterest",
                    "Position": [135.7292, 35.0394],
                }
            ]
        }
        mock_get_client.return_value = mock_client

        results = places_handler.search_places("temples in Kyoto")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "Kinkaku-ji")
        mock_client.search_text.assert_called_once()
        call_kwargs = mock_client.search_text.call_args.kwargs
        self.assertEqual(call_kwargs["QueryText"], "temples in Kyoto")
        self.assertNotIn("BiasPosition", call_kwargs)

    @patch("places_handler._get_geo_places_client")
    def test_search_places_with_bias_position(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search_text.return_value = {
            "ResultItems": [
                {"Title": "Some Place", "Position": [135.7681, 35.0116]}
            ]
        }
        mock_get_client.return_value = mock_client

        places_handler.search_places(
            "coffee shops", bias_latitude=35.0116, bias_longitude=135.7681
        )

        call_kwargs = mock_client.search_text.call_args.kwargs
        self.assertEqual(call_kwargs["BiasPosition"], [135.7681, 35.0116])

    @patch("places_handler._get_geo_places_client")
    def test_search_places_no_results_raises(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search_text.return_value = {"ResultItems": []}
        mock_get_client.return_value = mock_client

        with self.assertRaises(places_handler.PlacesToolError):
            places_handler.search_places("nonexistent place xyz")

    @patch("places_handler._get_geo_places_client")
    def test_search_places_boto3_error_raises_tool_error(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search_text.side_effect = Exception("AWS is down")
        mock_get_client.return_value = mock_client

        with self.assertRaises(places_handler.PlacesToolError):
            places_handler.search_places("temples")


class HaversineKmTests(unittest.TestCase):
    def test_haversine_zero_distance(self):
        self.assertAlmostEqual(places_handler._haversine_km(35.0, 135.0, 35.0, 135.0), 0.0)

    def test_haversine_known_distance(self):
        # Kinkaku-ji to Fushimi Inari Shrine, Kyoto — roughly 8-9 km apart.
        d = places_handler._haversine_km(35.0394, 135.7292, 34.9671, 135.7727)
        self.assertGreater(d, 5)
        self.assertLess(d, 15)


class SequencePlacesTests(unittest.TestCase):
    def test_sequence_orders_by_nearest_neighbor(self):
        places = [
            {"name": "A", "latitude": 35.0, "longitude": 135.0},
            {"name": "C", "latitude": 35.5, "longitude": 135.5},
            {"name": "B", "latitude": 35.01, "longitude": 135.01},
        ]

        ordered = places_handler.sequence_places(places)

        # Starts at A (first in list); B is much closer to A than C, so B
        # should come next, then C last.
        self.assertEqual([p["name"] for p in ordered], ["A", "B", "C"])

    def test_sequence_single_place(self):
        places = [{"name": "Solo", "latitude": 35.0, "longitude": 135.0}]

        ordered = places_handler.sequence_places(places)

        self.assertEqual(ordered, places)

    def test_sequence_empty_list_raises(self):
        with self.assertRaises(places_handler.PlacesToolError):
            places_handler.sequence_places([])

    def test_sequence_missing_coordinates_raises(self):
        places = [
            {"name": "A", "latitude": 35.0, "longitude": 135.0},
            {"name": "B"},
        ]

        with self.assertRaises(places_handler.PlacesToolError):
            places_handler.sequence_places(places)


class HandlerEntrypointTests(unittest.TestCase):
    @patch("places_handler.search_places")
    def test_handler_search_action(self, mock_search):
        mock_search.return_value = [{"name": "Kinkaku-ji"}]

        result = places_handler.handler({"action": "search", "query": "temples in Kyoto"})

        self.assertEqual(result, {"results": [{"name": "Kinkaku-ji"}]})
        mock_search.assert_called_once_with(
            query="temples in Kyoto",
            bias_latitude=None,
            bias_longitude=None,
            max_results=5,
        )

    def test_handler_search_action_missing_query_returns_error(self):
        result = places_handler.handler({"action": "search"})

        self.assertIn("error", result)

    @patch("places_handler.sequence_places")
    def test_handler_sequence_action(self, mock_sequence):
        places = [{"name": "A", "latitude": 35.0, "longitude": 135.0}]
        mock_sequence.return_value = places

        result = places_handler.handler({"action": "sequence", "places": places})

        self.assertEqual(result, {"ordered_places": places})

    def test_handler_unknown_action_returns_error(self):
        result = places_handler.handler({"action": "fly"})

        self.assertIn("error", result)
        self.assertIn("Unknown action", result["error"])

    @patch("places_handler.search_places")
    def test_handler_tool_error_returns_error_dict(self, mock_search):
        mock_search.side_effect = places_handler.PlacesToolError("no results")

        result = places_handler.handler({"action": "search", "query": "nowhere"})

        self.assertEqual(result, {"error": "no results"})

    @patch("places_handler.search_places")
    def test_handler_unexpected_error_is_caught(self, mock_search):
        mock_search.side_effect = RuntimeError("kaboom")

        result = places_handler.handler({"action": "search", "query": "temples"})

        self.assertIn("error", result)
        self.assertIn("kaboom", result["error"])


if __name__ == "__main__":
    unittest.main()
