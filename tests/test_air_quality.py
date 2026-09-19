from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from dashboard.air_quality import (
    AirQualityConfigurationError,
    AirQualityUpstreamError,
    OpenWeatherAirQualityService,
    _district_sample_points,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _reading(aqi: int, pm25: float = 18.2) -> dict:
    return {
        "coord": [69.27, 41.31],
        "list": [{
            "dt": 1789441200,
            "main": {"aqi": aqi},
            "components": {"pm2_5": pm25, "pm10": 27.0},
        }],
    }


class AirQualityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary.name) / "openweather.json"
        self.now = lambda: datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
        self.points = [("Северный район", 41.36, 69.30), ("Южный район", 41.25, 69.24)]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_requires_server_side_key(self) -> None:
        service = OpenWeatherAirQualityService(None, self.cache_path, sample_points=self.points, now=self.now)
        with self.assertRaises(AirQualityConfigurationError):
            service.get_tashkent()

    def test_queries_coordinates_and_uses_hourly_cache(self) -> None:
        calls: list[tuple[float, float]] = []

        def opener(request, timeout):
            del timeout
            query = parse_qs(urlparse(request.full_url).query)
            point = float(query["lat"][0]), float(query["lon"][0])
            calls.append(point)
            return _Response(_reading(2 if point[0] > 41.3 else 4))

        service = OpenWeatherAirQualityService(
            "test-secret", self.cache_path, sample_points=self.points, opener=opener, now=self.now
        )
        first = service.get_tashkent()
        second = service.get_tashkent()

        self.assertEqual(first["source"], "OpenWeather Air Pollution API")
        self.assertEqual(first["coverage"], "modeled_points")
        self.assertEqual([(item["name"], item["aqi"]) for item in first["samples"]], [
            ("Северный район", 2), ("Южный район", 4),
        ])
        self.assertEqual(first["samples"][0]["components_ug_m3"]["pm2_5"], 18.2)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(first["quota"]["requests_by_dashboard"], 2)
        self.assertNotIn("test-secret", self.cache_path.read_text(encoding="utf-8"))

    def test_respects_local_monthly_safety_ceiling(self) -> None:
        self.cache_path.write_text(json.dumps({
            "quota": {"month": "2026-09", "requests": 8_999},
        }), encoding="utf-8")
        service = OpenWeatherAirQualityService(
            "test-secret", self.cache_path, sample_points=self.points, now=self.now,
            opener=lambda *_args, **_kwargs: self.fail("External request must not be made"),
        )
        with self.assertRaises(AirQualityUpstreamError):
            service.get_tashkent()

    def test_http_error_does_not_expose_key(self) -> None:
        def opener(request, timeout):
            del timeout
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        service = OpenWeatherAirQualityService(
            "test-secret", self.cache_path, sample_points=self.points[:1], opener=opener, now=self.now
        )
        with self.assertRaises(AirQualityUpstreamError) as context:
            service.get_tashkent()
        self.assertIn("401", str(context.exception))
        self.assertNotIn("test-secret", str(context.exception))

    def test_rejects_other_aqi_scales(self) -> None:
        service = OpenWeatherAirQualityService(
            "test-secret", self.cache_path, sample_points=self.points[:1],
            opener=lambda *_args, **_kwargs: _Response(_reading(87)), now=self.now,
        )
        with self.assertRaises(AirQualityUpstreamError):
            service.get_tashkent()

    def test_geojson_geometry_collection_provides_district_center(self) -> None:
        path = Path(self.temporary.name) / "districts.geojson"
        path.write_text(json.dumps({
            "features": [{
                "properties": {"ADM2_RU": "Тестовый район"},
                "geometry": {"type": "GeometryCollection", "geometries": [{
                    "type": "Polygon", "coordinates": [[[69.2, 41.2], [69.4, 41.2], [69.4, 41.4]]],
                }]},
            }],
        }), encoding="utf-8")
        points = _district_sample_points(path)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][0], "Тестовый район")
        self.assertAlmostEqual(points[0][1], 41.3)
        self.assertAlmostEqual(points[0][2], 69.3)


if __name__ == "__main__":
    unittest.main()
