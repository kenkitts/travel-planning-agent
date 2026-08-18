# Places tool Lambda

Wraps [Amazon Location Service's Places (geo-places) API](https://docs.aws.amazon.com/location/latest/APIReference/)
to provide place search, geocoding, and simple geographic sequencing for the
travel planning agent.

Uses the `geo-places` boto3 client (`search_text`, `geocode`) rather than the
legacy `location` client, so no Place Index resource needs to be provisioned
via CDK — auth is handled entirely through IAM.

## Behavior

Supports two actions, selected via the `action` field in the input:

- **`search`** — free-text search for places (e.g. "temples in Kyoto",
  "coffee shops near Shibuya"), optionally biased toward a coordinate.
  Returns candidate places with name, address, category, and coordinates.
- **`sequence`** — given a list of places (each with coordinates), returns
  them reordered by a simple nearest-neighbor greedy route starting from the
  first place in the list, to produce a more geographically sensible
  day plan. This is a lightweight heuristic, not a true routing/TSP solver —
  sufficient for sequencing a handful of stops in a day.

## Input

Search:
```json
{
  "action": "search",
  "query": "temples in Kyoto",
  "bias_latitude": 35.0116,
  "bias_longitude": 135.7681,
  "max_results": 5
}
```

Sequence:
```json
{
  "action": "sequence",
  "places": [
    {"name": "Fushimi Inari Shrine", "latitude": 34.9671, "longitude": 135.7727},
    {"name": "Kiyomizu-dera", "latitude": 34.9948, "longitude": 135.7847},
    {"name": "Kinkaku-ji", "latitude": 35.0394, "longitude": 135.7292}
  ]
}
```

## Output

Search:
```json
{
  "results": [
    {
      "name": "Fushimi Inari Shrine",
      "address": "68 Fukakusa Yabunouchicho, Fushimi Ward, Kyoto, Japan",
      "category": "Place of Worship",
      "latitude": 34.9671,
      "longitude": 135.7727
    }
  ]
}
```

Sequence:
```json
{
  "ordered_places": [
    {"name": "Fushimi Inari Shrine", "latitude": 34.9671, "longitude": 135.7727},
    {"name": "Kiyomizu-dera", "latitude": 34.9948, "longitude": 135.7847},
    {"name": "Kinkaku-ji", "latitude": 35.0394, "longitude": 135.7292}
  ]
}
```

On error (unknown action, no results, AWS API failure), returns:

```json
{ "error": "<description>" }
```
