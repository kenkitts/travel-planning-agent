"""Places tool Lambda handler.

Wraps Amazon Location Service's Places API (the `geo-places` boto3 client) to
provide place search and simple geographic day-sequencing for the travel
planning agent. Used as an AgentCore Gateway Lambda target.

Uses `geo-places` (SearchText) rather than the legacy `location` client's
SearchPlaceIndexForText, so no Place Index resource needs to be provisioned —
auth is handled entirely via IAM.

Logging: structured logger.info()/logger.error() calls at the request,
upstream-boto3-call, and response boundaries, matching agent.py's style —
added as part of the observability pass (see DESIGN.md). Prior to this, the
handler emitted nothing beyond Lambda's own automatic invocation/error/
duration log lines, making a bad geo-places response (empty results, a
boto3/IAM error) indistinguishable from any other failure without
re-deriving it from the Gateway's own opaque error message.
"""
import logging
import math
import os
import time
from typing import Any, Optional

import boto3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


class PlacesToolError(Exception):
    """Raised for any handled failure while fulfilling a places request."""


def _get_geo_places_client():
    return boto3.client("geo-places", region_name=AWS_REGION)


def _extract_place(item: dict) -> dict:
    position = item.get("Position") or [None, None]
    longitude, latitude = position[0], position[1]
    place_type = item.get("PlaceType")
    if isinstance(place_type, list):
        category = place_type[0] if place_type else None
    else:
        category = place_type

    return {
        "name": item.get("Title"),
        "address": (item.get("Address") or {}).get("Label"),
        "category": category,
        "latitude": latitude,
        "longitude": longitude,
    }


def search_places(
    query: str,
    bias_latitude: Optional[float] = None,
    bias_longitude: Optional[float] = None,
    max_results: int = 5,
) -> list[dict]:
    """Free-text search for places, optionally biased toward a coordinate."""
    client = _get_geo_places_client()
    kwargs: dict[str, Any] = {"QueryText": query, "MaxResults": max_results}
    if bias_latitude is not None and bias_longitude is not None:
        kwargs["BiasPosition"] = [bias_longitude, bias_latitude]

    started = time.monotonic()
    try:
        response = client.search_text(**kwargs)
    except Exception as e:  # noqa: BLE001 - surface any boto3/AWS error as a tool error
        logger.error(
            "geo-places SearchText failed: query=%r (%.0fms elapsed): %s",
            query, (time.monotonic() - started) * 1000, e,
        )
        raise PlacesToolError(f"Places search failed: {e}") from e

    elapsed_ms = (time.monotonic() - started) * 1000
    items = response.get("ResultItems", [])
    if not items:
        logger.warning("geo-places SearchText returned no results: query=%r", query)
        raise PlacesToolError(f"No places found for query '{query}'")

    logger.info(
        "geo-places SearchText succeeded: query=%r result_count=%d (%.0fms elapsed)",
        query, len(items), elapsed_ms,
    )
    return [_extract_place(item) for item in items]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometers."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def sequence_places(places: list[dict]) -> list[dict]:
    """Greedy nearest-neighbor ordering of places, starting from the first.

    This is a lightweight heuristic (not a full TSP solver) intended to avoid
    obviously illogical backtracking across a handful of daily stops.
    """
    if not places:
        raise PlacesToolError("'places' must be a non-empty list")

    for p in places:
        if p.get("latitude") is None or p.get("longitude") is None:
            raise PlacesToolError(
                f"Place '{p.get('name', '<unnamed>')}' is missing latitude/longitude"
            )

    remaining = list(places[1:])
    ordered = [places[0]]
    current = places[0]

    while remaining:
        nearest_idx = min(
            range(len(remaining)),
            key=lambda i: _haversine_km(
                current["latitude"],
                current["longitude"],
                remaining[i]["latitude"],
                remaining[i]["longitude"],
            ),
        )
        current = remaining.pop(nearest_idx)
        ordered.append(current)

    return ordered


def handler(event: dict, context: Any = None) -> dict:
    """Lambda entrypoint for the AgentCore Gateway Lambda target.

    Expected event shape (per tool_schema.json), dispatched on 'action':
      search:   {"action": "search", "query": "...", "bias_latitude": ...,
                 "bias_longitude": ..., "max_results": 5}
      sequence: {"action": "sequence", "places": [{"name", "latitude", "longitude"}, ...]}
    """
    try:
        action = event.get("action")
        logger.info("Places tool request: action=%r event=%r", action, event)

        if action == "search":
            query = event.get("query")
            if not query:
                raise PlacesToolError("'query' is required for action 'search'")
            results = search_places(
                query=query,
                bias_latitude=event.get("bias_latitude"),
                bias_longitude=event.get("bias_longitude"),
                max_results=event.get("max_results", 5),
            )
            logger.info("Places tool response: action=search result_count=%d", len(results))
            return {"results": results}

        if action == "sequence":
            places = event.get("places")
            ordered = sequence_places(places or [])
            logger.info("Places tool response: action=sequence place_count=%d", len(ordered))
            return {"ordered_places": ordered}

        raise PlacesToolError(
            f"Unknown action '{action}'; expected 'search' or 'sequence'"
        )
    except PlacesToolError as e:
        logger.warning("Places tool request failed: %s", e)
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - convert any unexpected error to a tool-safe response
        logger.exception("Places tool request failed with an unexpected error")
        return {"error": f"Unexpected error: {e}"}
