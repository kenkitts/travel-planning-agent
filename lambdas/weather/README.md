# Weather tool Lambda

Wraps the [Open-Meteo](https://open-meteo.com/) API (free, no API key required)
to provide weather forecasts for the travel planning agent.

## Behavior

Given a place name (or explicit latitude/longitude) and a date range, the
handler:

1. Geocodes the place name via Open-Meteo's Geocoding API
   (`GET https://geocoding-api.open-meteo.com/v1/search`) to get
   latitude/longitude — skipped if lat/lon are provided directly.
2. Fetches a daily forecast via the Forecast API
   (`GET https://api.open-meteo.com/v1/forecast`) for that location and date
   range, requesting: `temperature_2m_max`, `temperature_2m_min`,
   `precipitation_probability_max`, `precipitation_sum`, `weather_code`.
3. Returns a normalized JSON payload: per-day max/min temp (°C), precipitation
   probability (%), precipitation sum (mm), and a human-readable condition
   derived from the WMO weather code.

## Input

```json
{
  "location": "Kyoto, Japan",
  "start_date": "2026-10-03",
  "end_date": "2026-10-10"
}
```

or with explicit coordinates (skips geocoding):

```json
{
  "latitude": 35.0116,
  "longitude": 135.7681,
  "start_date": "2026-10-03",
  "end_date": "2026-10-10"
}
```

## Output

```json
{
  "location": "Kyoto, Japan",
  "latitude": 35.0116,
  "longitude": 135.7681,
  "timezone": "Asia/Tokyo",
  "daily": [
    {
      "date": "2026-10-03",
      "temperature_max_c": 24.1,
      "temperature_min_c": 16.8,
      "precipitation_probability_max_pct": 20,
      "precipitation_sum_mm": 0.0,
      "condition": "Partly cloudy"
    }
  ]
}
```

On error (place not found, API failure, invalid input), returns:

```json
{ "error": "<description>" }
```

## Notes on Open-Meteo's forecast range

Open-Meteo's `/v1/forecast` endpoint supports up to 16 days out. Itinerary
requests further in the future than that are not covered by this tool — the
agent should fall back to general/seasonal knowledge in that case rather than
treating a resulting error as fatal.
