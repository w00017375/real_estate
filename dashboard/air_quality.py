from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://api.openweathermap.org/data/2.5/air_pollution"
DOCUMENTATION_URL = "https://openweathermap.org/api/air-pollution"
CACHE_SECONDS = 60 * 60
# A conservative local ceiling also keeps 12 hourly district queries below
# 9,000 calls in a 31-day month. It does not represent the account-wide quota.
MONTHLY_CALL_SAFETY_LIMIT = 9_000


class AirQualityError(RuntimeError):
    """Base error returned by the server-side air-quality integration."""


class AirQualityConfigurationError(AirQualityError):
    """Raised when the server-side API key is missing."""


class AirQualityUpstreamError(AirQualityError):
    """Raised when OpenWeather cannot return a usable reading."""


def load_openweather_api_key(project_root: Path) -> str | None:
    """Read the secret from the process environment or a local ignored .env."""
    value = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if value:
        return value

    env_file = project_root / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "OPENWEATHER_API_KEY":
            return value.strip().strip('"').strip("'") or None
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_fresh(timestamp: Any, now: datetime) -> bool:
    parsed = _parse_timestamp(timestamp)
    return parsed is not None and 0 <= (now - parsed).total_seconds() < CACHE_SECONDS


def _coordinate_pairs(value: Any):
    """Yield lon/lat pairs from Polygon or MultiPolygon coordinate arrays."""
    if isinstance(value, list):
        if len(value) >= 2 and all(isinstance(part, (int, float)) for part in value[:2]):
            longitude, latitude = float(value[0]), float(value[1])
            if math.isfinite(longitude) and math.isfinite(latitude):
                yield longitude, latitude
        else:
            for part in value:
                yield from _coordinate_pairs(part)


def _geometry_pairs(geometry: Any):
    if not isinstance(geometry, dict):
        return
    yield from _coordinate_pairs(geometry.get("coordinates"))
    for child in geometry.get("geometries") or []:
        yield from _geometry_pairs(child)


def _district_sample_points(districts_path: Path) -> list[tuple[str, float, float]]:
    """Use the same district geometry that the map renders for query locations."""
    try:
        geojson = json.loads(districts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AirQualityConfigurationError("Не удалось прочитать границы районов Ташкента.") from error

    points: list[tuple[str, float, float]] = []
    for feature in geojson.get("features") or []:
        name = str((feature.get("properties") or {}).get("ADM2_RU") or "").strip()
        coordinates = list(_geometry_pairs(feature.get("geometry")))
        if not name or not coordinates:
            continue
        longitudes, latitudes = zip(*coordinates)
        points.append((name, (min(latitudes) + max(latitudes)) / 2, (min(longitudes) + max(longitudes)) / 2))
    if not points:
        raise AirQualityConfigurationError("В файле карты нет точек районов Ташкента.")
    return points


class OpenWeatherAirQualityService:
    """Share hourly OpenWeather readings sampled at Tashkent district centers."""

    def __init__(
        self,
        api_key: str | None,
        cache_path: Path,
        *,
        districts_path: Path | None = None,
        sample_points: Sequence[tuple[str, float, float]] | None = None,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.cache_path = cache_path
        self.districts_path = districts_path
        self._sample_points = list(sample_points) if sample_points is not None else None
        self._opener = opener
        self._now = now
        self._lock = threading.Lock()
        self._document: dict[str, Any] | None = None

    def get_tashkent(self) -> dict[str, Any]:
        if not self.api_key:
            raise AirQualityConfigurationError(
                "На сервере не задан OPENWEATHER_API_KEY. Добавьте его в .env и перезапустите карту."
            )
        with self._lock:
            now = self._now().astimezone(timezone.utc)
            document = self._load_document()
            cached_payload = document.get("payload")
            if isinstance(cached_payload, dict) and _is_fresh(document.get("payload_fetched_at"), now):
                return self._response(cached_payload, document, cached=True, stale=False)

            try:
                payload = self._refresh(document, now)
            except AirQualityError as error:
                if isinstance(cached_payload, dict):
                    stale_payload = {**cached_payload, "warning": str(error)}
                    return self._response(stale_payload, document, cached=True, stale=True)
                raise

            document["payload"] = payload
            document["payload_fetched_at"] = _iso_timestamp(now)
            self._save_document(document)
            return self._response(payload, document, cached=False, stale=False)

    def _refresh(self, document: dict[str, Any], now: datetime) -> dict[str, Any]:
        points = self._sample_points
        if points is None:
            if self.districts_path is None:
                raise AirQualityConfigurationError("Не указан файл границ районов Ташкента.")
            points = _district_sample_points(self.districts_path)
            self._sample_points = points

        self._record_calls(document, len(points), now)
        samples: list[dict[str, Any]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(6, len(points))) as executor:
            futures = {executor.submit(self._request_point, point): point for point in points}
            for future in as_completed(futures):
                try:
                    samples.append(future.result())
                except AirQualityUpstreamError as error:
                    errors.append(str(error))

        if not samples:
            raise AirQualityUpstreamError(errors[0] if errors else "OpenWeather не вернул данные по Ташкенту.")
        order = {name: index for index, (name, _, _) in enumerate(points)}
        samples.sort(key=lambda item: order[item["name"]])
        note = (
            "OpenWeather оценивает загрязнение по координатам районов; эти точки не являются "
            "физическими станциями мониторинга. Индекс OpenWeather имеет шкалу 1–5."
        )
        if errors:
            note += f" Доступно {len(samples)} из {len(points)} точек."
        return {
            "source": "OpenWeather Air Pollution API",
            "documentation_url": DOCUMENTATION_URL,
            "coverage": "modeled_points",
            "coverage_note": note,
            "refresh_after_seconds": CACHE_SECONDS,
            "fetched_at": _iso_timestamp(now),
            "samples": samples,
        }

    def _request_point(self, point: tuple[str, float, float]) -> dict[str, Any]:
        name, latitude, longitude = point
        query = urlencode({"lat": latitude, "lon": longitude, "appid": self.api_key})
        request = Request(
            f"{API_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": "EstateParserDashboard/1.0"},
        )
        try:
            with self._opener(request, timeout=25) as response:
                raw = response.read()
        except HTTPError as error:
            if error.code == 401:
                raise AirQualityUpstreamError("OpenWeather не принял API-ключ (HTTP 401).") from None
            if error.code == 429:
                raise AirQualityUpstreamError("OpenWeather ограничил частоту запросов (HTTP 429).") from None
            raise AirQualityUpstreamError(f"OpenWeather отклонил запрос (HTTP {error.code}).") from None
        except (TimeoutError, URLError, OSError) as error:
            raise AirQualityUpstreamError(f"OpenWeather временно недоступен: {type(error).__name__}.") from None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AirQualityUpstreamError("OpenWeather вернул невалидный JSON.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list) or not payload["list"]:
            raise AirQualityUpstreamError("OpenWeather вернул неожиданный формат ответа.")
        reading = payload["list"][0]
        main = reading.get("main") if isinstance(reading, dict) else None
        aqi = main.get("aqi") if isinstance(main, dict) else None
        if isinstance(aqi, bool) or not isinstance(aqi, (int, float)) or aqi not in (1, 2, 3, 4, 5):
            raise AirQualityUpstreamError("В ответе OpenWeather нет индекса AQI 1–5.")
        observed_at = None
        if isinstance(reading.get("dt"), (int, float)):
            try:
                observed_at = _iso_timestamp(datetime.fromtimestamp(reading["dt"], timezone.utc))
            except (OverflowError, ValueError):
                pass
        components = reading.get("components") if isinstance(reading.get("components"), dict) else {}
        return {
            "id": hashlib.sha1(name.encode("utf-8")).hexdigest()[:12],
            "name": name,
            "kind": "modeled_point",
            "latitude": latitude,
            "longitude": longitude,
            "aqi": int(aqi),
            "observed_at": observed_at,
            "components_ug_m3": {
                key: value for key, value in components.items()
                if key in {"co", "no", "no2", "o3", "so2", "pm2_5", "pm10", "nh3"}
                and isinstance(value, (int, float)) and math.isfinite(value)
            },
        }

    def _record_calls(self, document: dict[str, Any], count: int, now: datetime) -> None:
        month = now.strftime("%Y-%m")
        quota = document.get("quota") if isinstance(document.get("quota"), dict) else {}
        if quota.get("month") != month:
            quota = {"month": month, "requests": 0}
        requests = int(quota.get("requests") or 0)
        if requests + count > MONTHLY_CALL_SAFETY_LIMIT:
            raise AirQualityUpstreamError("Защитный месячный лимит запросов качества воздуха исчерпан.")
        quota["requests"] = requests + count
        document["quota"] = quota
        # Reserve before sending requests: failures cannot overshoot the safety ceiling.
        self._save_document(document)

    def _response(self, payload: dict[str, Any], document: dict[str, Any], *, cached: bool, stale: bool) -> dict[str, Any]:
        quota = document.get("quota") if isinstance(document.get("quota"), dict) else {}
        return {
            **payload,
            "cached": cached,
            "stale": stale,
            "quota": {
                "month": quota.get("month"),
                "requests_by_dashboard": int(quota.get("requests") or 0),
                "safety_limit": MONTHLY_CALL_SAFETY_LIMIT,
            },
        }

    def _load_document(self) -> dict[str, Any]:
        if self._document is not None:
            return self._document
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._document = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._document = {}
        return self._document

    def _save_document(self, document: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.cache_path)
        self._document = document
