const state = {
  data: null,
  scope: "city",
  selectedDistrict: null,
  listVisible: true,
  boundaryLayer: null,
  labelsLayer: null,
  landmarksLayer: null,
  majorRoadsLayer: null,
  majorRoadBoundary: null,
  propertyMarkersLayer: null,
  airQualityEnabled: false,
  airQualityLayer: null,
  airQualityBoundaryGeometry: null,
  airQualityRefreshTimer: null,
  airQualityRequest: null,
  filters: {
    minPrice: "",
    maxPrice: "",
    areaMin: "",
    areaMax: "",
    floorMin: "",
    floorMax: "",
    currency: "",
    rooms: "",
    buildingType: "",
    transactionType: "",
    newBuilding: "",
    furnished: "",
    source: "",
    duplicateMode: "",
    activeOnly: true,
    sort: "newest",
    amenities: [],
    nearbyPlaces: [],
    sellerGroup: "",
  },
};

const elements = {
  list: document.querySelector("#listing-list"),
  template: document.querySelector("#listing-card-template"),
  resultCount: document.querySelector("#result-count"),
  resultWord: document.querySelector("#result-word"),
  title: document.querySelector("#selection-title"),
  copy: document.querySelector("#selection-copy"),
  eyebrow: document.querySelector("#selection-eyebrow"),
  clearDistrict: document.querySelector("#clear-district"),
  filters: document.querySelector("#filters"),
  filterToggle: document.querySelector("#filter-toggle"),
  listToggle: document.querySelector("#list-toggle"),
  workspace: document.querySelector(".workspace"),
  resultsPanel: document.querySelector(".results-panel"),
  activeFilterCount: document.querySelector("#active-filter-count"),
  mapFilterDock: document.querySelector("#map-filter-dock"),
  mapFilterMasterToggle: document.querySelector("#map-filter-master-toggle"),
  sortMenu: document.querySelector("#sort-menu"),
  sortCurrent: document.querySelector("#sort-current"),
  sourceStrip: document.querySelector("#source-strip"),
  listingDialog: document.querySelector("#listing-dialog"),
  listingDialogContent: document.querySelector("#listing-dialog-content"),
  listingDialogClose: document.querySelector("#listing-dialog-close"),
  airQualityDock: document.querySelector("#air-quality-dock"),
  airQualityToggle: document.querySelector("#air-quality-toggle"),
  airQualityStatus: document.querySelector("#air-quality-status"),
  airQualityLegend: document.querySelector("#air-quality-legend"),
  airQualityReading: document.querySelector("#air-quality-reading"),
  airQualityRangeMin: document.querySelector("#air-quality-range-min"),
  airQualityRangeMax: document.querySelector("#air-quality-range-max"),
};

const map = L.map("map", {
  zoomControl: false,
  attributionControl: false,
  preferCanvas: false,
  zoomAnimation: true,
  fadeAnimation: true,
  markerZoomAnimation: true,
  scrollWheelZoom: false,
  touchZoom: true,
  bounceAtZoomLimits: false,
  minZoom: 4,
  maxZoom: 14,
  zoomSnap: 0,
  zoomDelta: 1,
}).setView([41.31, 69.27], 11);

map.createPane("districtLabels");
map.getPane("districtLabels").style.zIndex = "450";
map.getPane("districtLabels").style.pointerEvents = "none";
map.createPane("majorRoads");
map.getPane("majorRoads").style.zIndex = "425";
map.getPane("majorRoads").style.pointerEvents = "none";
map.createPane("districtTooltips");
map.getPane("districtTooltips").style.zIndex = "700";
map.createPane("airHeat");
map.getPane("airHeat").style.zIndex = "395";
map.getPane("airHeat").style.pointerEvents = "none";
map.getPane("airHeat").classList.add("leaflet-air-heat-pane");
map.createPane("airSamples");
map.getPane("airSamples").style.zIndex = "465";
map.getPane("airSamples").style.pointerEvents = "none";
map.getPane("airSamples").classList.add("leaflet-air-samples-pane");
map.createPane("propertyMarkers");
map.getPane("propertyMarkers").style.zIndex = "640";

const PM25_COLOR_STOPS = [
  [0, [85, 189, 157]],
  [10, [215, 206, 105]],
  [20, [239, 153, 84]],
  [35, [218, 94, 83]],
  [75, [141, 81, 145]],
];
const PM25_COLOR_CACHE = new Map();

function pm25Rgb(value) {
  const key = Math.round(Math.max(0, Math.min(75, Number(value))) * 100);
  const cached = PM25_COLOR_CACHE.get(key);
  if (cached) return cached;
  const pm25 = key / 100;
  for (let index = 1; index < PM25_COLOR_STOPS.length; index += 1) {
    const [end, endRgb] = PM25_COLOR_STOPS[index];
    if (pm25 > end) continue;
    const [start, startRgb] = PM25_COLOR_STOPS[index - 1];
    const fraction = (pm25 - start) / (end - start);
    const rgb = startRgb.map((channel, component) => Math.round(channel + (endRgb[component] - channel) * fraction));
    PM25_COLOR_CACHE.set(key, rgb);
    return rgb;
  }
  return PM25_COLOR_STOPS.at(-1)[1];
}

function pm25HeatRgb(value, minimum, maximum) {
  const rgb = pm25Rgb(value);
  const spread = maximum - minimum;
  if (spread < 0.05) return rgb;
  const relative = Math.max(0, Math.min(1, (value - minimum) / spread));
  const lighten = (1 - relative) * 0.38;
  return rgb.map((channel) => Math.round(channel + (255 - channel) * lighten));
}

function rgbCss(rgb) {
  return `rgb(${rgb.join(",")})`;
}

const AirQualityHeatLayer = L.Layer.extend({
  options: { pane: "airHeat", gridSize: 4 },

  initialize(samples, options = {}) {
    L.setOptions(this, options);
    this._samples = samples || [];
  },

  onAdd(leafletMap) {
    this._map = leafletMap;
    this._canvas = L.DomUtil.create("canvas", "air-quality-heat-canvas");
    leafletMap.getPane(this.options.pane).appendChild(this._canvas);
    leafletMap.on("moveend zoomend resize", this._reset, this);
    this._reset();
  },

  onRemove(leafletMap) {
    leafletMap.off("moveend zoomend resize", this._reset, this);
    this._canvas?.remove();
    this._canvas = null;
    this._map = null;
  },

  _reset() {
    if (!this._map || !this._canvas) return;
    const size = this._map.getSize();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const topLeft = this._map.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(this._canvas, topLeft);
    this._canvas.width = Math.max(1, Math.round(size.x * ratio));
    this._canvas.height = Math.max(1, Math.round(size.y * ratio));
    this._canvas.style.width = `${size.x}px`;
    this._canvas.style.height = `${size.y}px`;

    const context = this._canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size.x, size.y);
    context.globalCompositeOperation = "source-over";

    const boundary = this.options.boundaryGeometry;
    if (boundary?.coordinates?.length) {
      context.save();
      context.beginPath();
      for (const polygon of boundary.coordinates) {
        for (const ring of polygon) {
          ring.forEach(([longitude, latitude], index) => {
            const point = this._map.latLngToContainerPoint([latitude, longitude]);
            if (index === 0) context.moveTo(point.x, point.y);
            else context.lineTo(point.x, point.y);
          });
          context.closePath();
        }
      }
      context.clip("evenodd");
    }

    const projectedSamples = [];
    for (const sample of this._samples) {
      const latitude = Number(sample.latitude);
      const longitude = Number(sample.longitude);
      const pm25 = Number(sample.components_ug_m3?.pm2_5);
      if (![latitude, longitude, pm25].every(Number.isFinite)) continue;
      const point = this._map.latLngToContainerPoint([latitude, longitude]);
      projectedSamples.push({ x: point.x, y: point.y, pm25 });
    }

    if (projectedSamples.length) {
      const cell = this.options.gridSize;
      const gridWidth = Math.max(1, Math.ceil(size.x / cell));
      const gridHeight = Math.max(1, Math.ceil(size.y / cell));
      const grid = document.createElement("canvas");
      grid.width = gridWidth;
      grid.height = gridHeight;
      const gridContext = grid.getContext("2d");
      const image = gridContext.createImageData(gridWidth, gridHeight);
      for (let row = 0; row < gridHeight; row += 1) {
        const y = (row + 0.5) * cell;
        for (let column = 0; column < gridWidth; column += 1) {
          const x = (column + 0.5) * cell;
          let weightedValue = 0;
          let totalWeight = 0;
          for (const sample of projectedSamples) {
            const distanceSquared = (x - sample.x) ** 2 + (y - sample.y) ** 2;
            const weight = 1 / (distanceSquared + 8 ** 2);
            weightedValue += sample.pm25 * weight;
            totalWeight += weight;
          }
          const rgb = pm25HeatRgb(
            weightedValue / totalWeight,
            this.options.minimumPm25,
            this.options.maximumPm25,
          );
          const offset = (row * gridWidth + column) * 4;
          image.data[offset] = rgb[0];
          image.data[offset + 1] = rgb[1];
          image.data[offset + 2] = rgb[2];
          image.data[offset + 3] = 255;
        }
      }
      gridContext.putImageData(image, 0, 0);
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.globalAlpha = 0.78;
      context.drawImage(grid, 0, 0, size.x, size.y);
    }
    if (boundary?.coordinates?.length) context.restore();
  },
});

const SCHEMATIC_MAJOR_ROAD_LAYERS = new Set([
  "highway_major_subtle",
  "highway_motorway_subtle",
]);

function schematicRoadPaint() {
  return {
    "line-color": "#62706a",
    "line-opacity": 0.12,
    "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.2, 12.5, 3.4],
    "line-blur": 0,
    "line-gap-width": 0,
    "line-offset": 0,
  };
}

function prepareSchematicBaseStyle(style) {
  return {
    ...style,
    layers: (style.layers || [])
      .filter((layer) => layer.type !== "symbol")
      .filter((layer) => {
        const isTransportLine = layer.type === "line" && layer["source-layer"] === "transportation";
        return !isTransportLine;
      }),
  };
}

function cityBoundaryGeometry(geojson) {
  const coordinates = [];
  const appendGeometry = (geometry) => {
    if (!geometry) return;
    if (geometry.type === "Polygon") coordinates.push(geometry.coordinates);
    if (geometry.type === "MultiPolygon") coordinates.push(...geometry.coordinates);
    if (geometry.type === "GeometryCollection") {
      geometry.geometries?.forEach(appendGeometry);
    }
  };
  for (const feature of geojson.features || []) {
    appendGeometry(feature.geometry);
  }
  if (!coordinates.length) throw new Error("Геометрия границы Ташкента пуста");
  return { type: "MultiPolygon", coordinates };
}

function prepareSchematicRoadStyle(style) {
  return {
    ...style,
    layers: (style.layers || [])
      .filter((layer) => SCHEMATIC_MAJOR_ROAD_LAYERS.has(layer.id))
      .map((layer) => {
        const paint = schematicRoadPaint();
        const prepared = {
          ...layer,
          layout: {
            ...(layer.layout || {}),
            visibility: "visible",
            "line-cap": "butt",
            "line-join": "round",
          },
          paint,
        };
        delete prepared.minzoom;
        delete prepared.maxzoom;
        const roadClassFilter = layer.id.startsWith("highway_major_")
          ? ["match", ["get", "class"], ["primary", "trunk"], true, false]
          : ["==", ["get", "class"], "motorway"];
        prepared.filter = [
          "all",
          ["==", ["geometry-type"], "LineString"],
          roadClassFilter,
        ];
        return prepared;
      }),
  };
}

let majorRoadMaskFrame = null;

function applyMajorRoadMask() {
  majorRoadMaskFrame = null;
  if (!state.majorRoadsLayer || !state.majorRoadBoundary || !map.hasLayer(state.majorRoadsLayer)) return;
  const container = state.majorRoadsLayer.getContainer();
  if (!container) return;

  const mapRect = map.getContainer().getBoundingClientRect();
  const roadRect = container.getBoundingClientRect();
  if (!roadRect.width || !roadRect.height) return;
  const offsetX = mapRect.left - roadRect.left;
  const offsetY = mapRect.top - roadRect.top;
  const paths = [];

  for (const polygon of state.majorRoadBoundary.coordinates) {
    for (const ring of polygon) {
      const points = ring.map(([lng, lat]) => {
        const point = map.latLngToContainerPoint([lat, lng]);
        return `${(point.x + offsetX).toFixed(1)} ${(point.y + offsetY).toFixed(1)}`;
      });
      if (points.length) paths.push(`M ${points.join(" L ")} Z`);
    }
  }

  const width = Math.ceil(roadRect.width);
  const height = Math.ceil(roadRect.height);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><path d="${paths.join(" ")}" fill="white" fill-rule="evenodd"/></svg>`;
  const mask = `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
  container.style.maskImage = mask;
  container.style.webkitMaskImage = mask;
  container.style.maskPosition = "0 0";
  container.style.webkitMaskPosition = "0 0";
  container.style.maskRepeat = "no-repeat";
  container.style.webkitMaskRepeat = "no-repeat";
  container.style.maskSize = "100% 100%";
  container.style.webkitMaskSize = "100% 100%";
}

function scheduleMajorRoadMask() {
  if (majorRoadMaskFrame !== null) window.cancelAnimationFrame(majorRoadMaskFrame);
  majorRoadMaskFrame = window.requestAnimationFrame(applyMajorRoadMask);
}

function syncMajorRoadLayerVisibility() {
  if (!state.majorRoadsLayer) return;
  if (state.scope === "city") {
    if (!map.hasLayer(state.majorRoadsLayer)) state.majorRoadsLayer.addTo(map);
    scheduleMajorRoadMask();
  } else if (map.hasLayer(state.majorRoadsLayer)) {
    state.majorRoadsLayer.remove();
  }
}

function airQualityTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
    timeZone: "Asia/Tashkent",
  }).format(date);
}

function clearAirQualityTimer() {
  if (state.airQualityRefreshTimer !== null) {
    window.clearTimeout(state.airQualityRefreshTimer);
    state.airQualityRefreshTimer = null;
  }
}

function clearAirQualityLayer() {
  clearAirQualityTimer();
  if (state.airQualityLayer) {
    state.airQualityLayer.remove();
    state.airQualityLayer = null;
  }
  elements.airQualityLegend.hidden = true;
}

function setAirQualityStatus(text, loading = false) {
  elements.airQualityStatus.textContent = text;
  elements.airQualityDock.classList.toggle("is-loading", loading);
}

function renderAirQuality(payload) {
  clearAirQualityLayer();
  const samples = (payload.samples || []).filter((sample) => (
    Number.isFinite(Number(sample.latitude))
    && Number.isFinite(Number(sample.longitude))
    && sample.components_ug_m3?.pm2_5 != null
    && Number.isFinite(Number(sample.components_ug_m3.pm2_5))
    && Number(sample.components_ug_m3.pm2_5) >= 0
  ));
  if (!samples.length) throw new Error("OpenWeather не вернул актуальные точки для Ташкента.");
  const values = samples.map((sample) => Number(sample.components_ug_m3.pm2_5)).sort((first, second) => first - second);
  const minimum = values[0];
  const maximum = values.at(-1);

  const heat = new AirQualityHeatLayer(samples, {
    boundaryGeometry: state.airQualityBoundaryGeometry,
    minimumPm25: minimum,
    maximumPm25: maximum,
  });
  const markers = L.layerGroup();
  for (const sample of samples) {
    const pm25 = Number(sample.components_ug_m3.pm2_5);
    const value = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(pm25);
    const marker = L.marker([sample.latitude, sample.longitude], {
      pane: "airSamples",
      interactive: false,
      keyboard: false,
      icon: L.divIcon({
        className: "air-quality-marker",
        html: `<span class="air-quality-marker__value" aria-label="PM₂.₅: ${value} микрограмм на кубический метр">${value}</span>`,
        iconSize: [46, 20],
        iconAnchor: [23, -12],
      }),
    });
    marker.addTo(markers);
  }

  state.airQualityLayer = L.layerGroup([heat, markers]).addTo(map);
  updateBoundaryStyles();
  elements.airQualityLegend.hidden = false;
  const midpoint = Math.floor(values.length / 2);
  const median = values.length % 2 ? values[midpoint] : (values[midpoint - 1] + values[midpoint]) / 2;
  const displayValue = (value) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value);
  const medianText = displayValue(median);
  elements.airQualityReading.innerHTML = `PM₂.₅ ${medianText} <small>мкг/м³</small>`;
  elements.airQualityRangeMin.textContent = displayValue(minimum);
  elements.airQualityRangeMax.textContent = displayValue(maximum);
  elements.airQualityLegend.style.setProperty("--heat-low", rgbCss(pm25HeatRgb(minimum, minimum, maximum)));
  elements.airQualityLegend.style.setProperty("--heat-high", rgbCss(pm25HeatRgb(maximum, minimum, maximum)));
  elements.airQualityLegend.setAttribute(
    "aria-label",
    `PM₂.₅: медиана ${medianText} мкг/м³; плавный градиент по текущим точкам от ${displayValue(minimum)} до ${displayValue(maximum)} мкг/м³`,
  );
  elements.airQualityDock.title = payload.coverage_note || "Оценка OpenWeather по координатам районов";
  const latestObservation = samples
    .map((sample) => sample.observed_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  const time = airQualityTime(latestObservation || payload.fetched_at);
  const coverage = `PM₂.₅ · ${samples.length} ${samples.length === 1 ? "точка" : "точек"}`;
  setAirQualityStatus(`${coverage}${time ? ` · ${time}` : ""}${payload.stale ? " · кэш" : ""}`);

  const refreshMilliseconds = Math.max(60_000, Number(payload.refresh_after_seconds || 3600) * 1000);
  state.airQualityRefreshTimer = window.setTimeout(() => {
    if (!state.airQualityEnabled || state.scope !== "city") return;
    if (document.visibilityState === "hidden") {
      state.airQualityRefreshTimer = window.setTimeout(() => loadAirQuality(), 60_000);
      return;
    }
    loadAirQuality();
  }, refreshMilliseconds);
}

async function loadAirQuality() {
  if (!state.airQualityEnabled || state.scope !== "city") return;
  if (state.airQualityRequest) return state.airQualityRequest;
  setAirQualityStatus("Загрузка…", true);
  state.airQualityRequest = fetch("/api/air-quality", { cache: "no-store" })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
      if (state.airQualityEnabled && state.scope === "city") renderAirQuality(payload);
      return payload;
    })
    .catch((error) => {
      console.error("Качество воздуха не загружено", error);
      if (state.airQualityEnabled) setAirQualityStatus("Данные недоступны");
      elements.airQualityDock.title = error.message || "Не удалось загрузить OpenWeather";
    })
    .finally(() => {
      state.airQualityRequest = null;
      elements.airQualityDock.classList.remove("is-loading");
    });
  return state.airQualityRequest;
}

function setAirQualityEnabled(enabled) {
  state.airQualityEnabled = Boolean(enabled) && state.scope === "city";
  elements.airQualityToggle.checked = state.airQualityEnabled;
  if (!state.airQualityEnabled) {
    clearAirQualityLayer();
    updateBoundaryStyles();
    setAirQualityStatus(state.scope === "city" ? "Выключено" : "Только для Ташкента");
    return;
  }
  loadAirQuality();
}

function syncAirQualityAvailability() {
  const available = AIR_QUALITY_UI_ENABLED && state.scope === "city";
  elements.airQualityDock.hidden = !available;
  elements.airQualityToggle.disabled = !available;
  if (!available) setAirQualityEnabled(false);
  else if (!state.airQualityEnabled) setAirQualityStatus("Выключено");
}

async function loadSchematicBasemap() {
  if (typeof L.maplibreGL !== "function") {
    console.warn("Схематичная подложка недоступна: MapLibre не загрузился");
    return;
  }
  try {
    const [styleResponse, boundaryResponse] = await Promise.all([
      fetch("https://tiles.openfreemap.org/styles/positron"),
      fetch("/data/tashkent_city_districts.geojson"),
    ]);
    if (!styleResponse.ok) throw new Error(`HTTP ${styleResponse.status}`);
    if (!boundaryResponse.ok) throw new Error(`Границы Ташкента: HTTP ${boundaryResponse.status}`);
    const [sourceStyle, cityGeojson] = await Promise.all([
      styleResponse.json(),
      boundaryResponse.json(),
    ]);
    const boundary = cityBoundaryGeometry(cityGeojson);
    state.majorRoadBoundary = boundary;
    const basemap = L.maplibreGL({
      style: prepareSchematicBaseStyle(sourceStyle),
      interactive: false,
      attributionControl: false,
    }).addTo(map);
    basemap.getContainer().classList.add("schematic-basemap");
    state.majorRoadsLayer = L.maplibreGL({
      style: prepareSchematicRoadStyle(sourceStyle),
      interactive: false,
      attributionControl: false,
      pane: "majorRoads",
      padding: 0.3,
    });
    syncMajorRoadLayerVisibility();
    state.majorRoadsLayer.getContainer()?.classList.add("schematic-major-roads");
    state.majorRoadsLayer.getMaplibreMap()?.on("idle", scheduleMajorRoadMask);
    scheduleMajorRoadMask();
  } catch (error) {
    console.warn("Схематичная подложка не загрузилась", error);
  }
}

const AIR_QUALITY_UI_ENABLED = false;
const CITY_MIN_ZOOM = 10;
const CITY_MAX_ZOOM = 14;
const CITY_CENTER = [41.3111, 69.2797];
const CITY_OVERVIEW_ZOOM = 12;
const DISTRICT_LABEL_MIN_ZOOM = 11.15;
const LANDMARK_LABEL_MIN_ZOOM = 12.15;
const COUNTRY_MIN_ZOOM = 3;
const COUNTRY_MAX_ZOOM = 12;
const REGION_LABEL_MIN_ZOOM = 7.15;

L.control.zoom({ position: "bottomleft" }).addTo(map);
map.on("zoom zoomend", updateDistrictLabelVisibility);
map.on("move zoom moveend zoomend resize", scheduleMajorRoadMask);

const anchoredZoom = {
  frame: null,
  targetZoom: null,
  anchorPoint: null,
  anchorLatLng: null,
  lastFrameAt: 0,
  lastInputAt: 0,
  moving: false,
};

function clampMapZoom(zoom) {
  return Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), zoom));
}

function centerForAnchoredZoom(anchorLatLng, anchorPoint, zoom) {
  const viewportCenter = map.getSize().divideBy(2);
  const projectedAnchor = map.project(anchorLatLng, zoom);
  const anchorOffset = anchorPoint.subtract(viewportCenter);
  return map.unproject(projectedAnchor.subtract(anchorOffset), zoom);
}

function cancelAnchoredZoom() {
  if (anchoredZoom.frame !== null) {
    window.cancelAnimationFrame(anchoredZoom.frame);
    anchoredZoom.frame = null;
  }
  if (anchoredZoom.moving) {
    map._moveEnd(true);
    anchoredZoom.moving = false;
  }
  anchoredZoom.targetZoom = null;
}

function renderAnchoredZoomFrame(timestamp) {
  const elapsed = Math.min(34, Math.max(1, timestamp - anchoredZoom.lastFrameAt));
  anchoredZoom.lastFrameAt = timestamp;

  const currentZoom = map.getZoom();
  const remaining = anchoredZoom.targetZoom - currentZoom;
  const easeOut = 1 - Math.exp(-elapsed / 78);
  const nextZoom = Math.abs(remaining) < 0.001
    ? anchoredZoom.targetZoom
    : currentZoom + remaining * easeOut;
  const nextCenter = centerForAnchoredZoom(
    anchoredZoom.anchorLatLng,
    anchoredZoom.anchorPoint,
    nextZoom,
  );

  map._move(nextCenter, nextZoom, { pinch: true, round: false });

  const settled = Math.abs(anchoredZoom.targetZoom - nextZoom) < 0.001
    && timestamp - anchoredZoom.lastInputAt > 48;
  if (settled) {
    map._moveEnd(true);
    anchoredZoom.frame = null;
    anchoredZoom.targetZoom = null;
    anchoredZoom.moving = false;
    return;
  }

  anchoredZoom.frame = window.requestAnimationFrame(renderAnchoredZoomFrame);
}

function enableAnchoredExponentialZoom() {
  const container = map.getContainer();
  container.addEventListener("wheel", (event) => {
    event.preventDefault();

    const anchorPoint = map.mouseEventToContainerPoint(event);
    const anchorLatLng = map.containerPointToLatLng(anchorPoint);
    const modeMultiplier = event.deltaMode === 1
      ? 16
      : event.deltaMode === 2 ? container.clientHeight : 1;
    const limitedDelta = Math.max(-140, Math.min(140, event.deltaY * modeMultiplier));
    const sensitivity = event.ctrlKey ? 0.011 : 0.0026;
    const zoomDelta = -limitedDelta * sensitivity;
    const exponentialScale = 2 ** zoomDelta;
    const baseZoom = anchoredZoom.targetZoom ?? map.getZoom();
    const targetZoom = clampMapZoom(baseZoom + Math.log2(exponentialScale));

    if (anchoredZoom.frame === null && Math.abs(targetZoom - map.getZoom()) < 0.0001) return;

    anchoredZoom.targetZoom = targetZoom;
    anchoredZoom.anchorPoint = anchorPoint;
    anchoredZoom.anchorLatLng = anchorLatLng;
    anchoredZoom.lastInputAt = performance.now();

    if (anchoredZoom.frame === null) {
      map._stop();
      map._moveStart(true, false);
      anchoredZoom.moving = true;
      anchoredZoom.lastFrameAt = anchoredZoom.lastInputAt;
      anchoredZoom.frame = window.requestAnimationFrame(renderAnchoredZoomFrame);
    }
  }, { passive: false });
}

enableAnchoredExponentialZoom();

function countWord(value) {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "объявлений";
  if (mod10 === 1) return "объявление";
  if (mod10 >= 2 && mod10 <= 4) return "объявления";
  return "объявлений";
}

function formatCount(value) {
  const count = Number(value) || 0;
  return `${count} ${countWord(count)}`;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value));
}

function formatDate(value) {
  if (!value) return "Дата не указана";
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" }).format(date);
}

function formatDateTime(value) {
  if (!value) return "";
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function dateValue(item) {
  const value = item.published_at || item.first_seen_at;
  if (!value) return 0;
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function offersFor(item) {
  return Array.isArray(item.offers) && item.offers.length ? item.offers : [item];
}

function currentOfferPrice(offer) {
  return offer.current_price ?? offer.price ?? null;
}

function preferredOffer(item) {
  const min = Number(state.filters.minPrice) || 0;
  const max = Number(state.filters.maxPrice) || Infinity;
  return offersFor(item).find((offer) => {
    if (state.filters.source && offer.source !== state.filters.source) return false;
    if (state.filters.sellerGroup && sellerGroupFor(offer) !== state.filters.sellerGroup) return false;
    if (state.filters.activeOnly && offer.status !== "active") return false;
    if (state.filters.currency && offer.currency !== state.filters.currency) return false;
    const price = currentOfferPrice(offer);
    return price === null ? min === 0 && max === Infinity : Number(price) >= min && Number(price) <= max;
  }) || offersFor(item)[0];
}

function sortablePrice(item) {
  const values = offersFor(item)
    .filter((offer) => !state.filters.currency || offer.currency === state.filters.currency)
    .map(currentOfferPrice)
    .filter((value) => value !== null && Number.isFinite(Number(value)))
    .map(Number);
  return values.length ? Math.min(...values) : null;
}

function districtCounts() {
  const counts = new Map();
  filteredItems(false).forEach((item) => {
    if (item.district) counts.set(item.district, (counts.get(item.district) || 0) + 1);
  });
  return counts;
}

function validListingCoordinates(item) {
  const latitude = Number(item.latitude);
  const longitude = Number(item.longitude);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
  if (latitude === 0 && longitude === 0) return null;
  return [latitude, longitude];
}

function hasStreetAndHouse(item) {
  const address = String(item.street || "").trim().replace(/\s+/g, " ");
  if (!address) return false;
  return /(?:,|(?:^|\s)(?:дом|д\.)\s*)\s*\d+[A-Za-zА-Яа-яЁё0-9/-]*\s*$/iu.test(address)
    || /(?:^|\s)(?:улица|ул\.|проспект|пр-т|переулок|проезд|шоссе)\s+.+\s\d+[A-Za-zА-Яа-яЁё0-9/-]*\s*$/iu.test(address);
}

function listingLocationPrecision(item) {
  if (item.location_precision === "coordinates" || validListingCoordinates(item)) return "coordinates";
  if (
    item.location_precision === "address"
    || hasStreetAndHouse(item)
    || String(item.street || "").trim()
    || String(item.residential_complex_name || "").trim()
  ) return "address";
  return "district";
}

function hashText(value) {
  let hash = 2166136261;
  for (const character of String(value || "")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function seededRandom(seed) {
  let value = seed >>> 0;
  return () => {
    value += 0x6D2B79F5;
    let result = value;
    result = Math.imul(result ^ (result >>> 15), result | 1);
    result ^= result + Math.imul(result ^ (result >>> 7), result | 61);
    return ((result ^ (result >>> 14)) >>> 0) / 4294967296;
  };
}

function pointInRing(longitude, latitude, ring) {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index, index += 1) {
    const [currentLongitude, currentLatitude] = ring[index];
    const [previousLongitude, previousLatitude] = ring[previous];
    const intersects = (currentLatitude > latitude) !== (previousLatitude > latitude)
      && longitude < ((previousLongitude - currentLongitude) * (latitude - currentLatitude))
        / (previousLatitude - currentLatitude) + currentLongitude;
    if (intersects) inside = !inside;
  }
  return inside;
}

function pointInPolygon(longitude, latitude, rings) {
  if (!rings?.length || !pointInRing(longitude, latitude, rings[0])) return false;
  return !rings.slice(1).some((hole) => pointInRing(longitude, latitude, hole));
}

function pointInGeometry(longitude, latitude, geometry) {
  if (!geometry) return false;
  if (geometry.type === "Polygon") return pointInPolygon(longitude, latitude, geometry.coordinates);
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.some((polygon) => pointInPolygon(longitude, latitude, polygon));
  }
  if (geometry.type === "GeometryCollection") {
    return geometry.geometries.some((child) => pointInGeometry(longitude, latitude, child));
  }
  return false;
}

function districtBoundaryLayers(name) {
  const layers = [];
  state.boundaryLayer?.eachLayer((layer) => {
    if (boundaryName(layer) === name && layer.getBounds) layers.push(layer);
  });
  return layers;
}

function approximateAddressCoordinates(item) {
  const layers = districtBoundaryLayers(item.district);
  if (!layers.length) return null;
  const seed = hashText(`${item.property_group_id || item.listing_pk}|${item.street}`);
  const layer = layers[seed % layers.length];
  const bounds = layer.getBounds();
  const random = seededRandom(seed);
  const south = bounds.getSouth();
  const north = bounds.getNorth();
  const west = bounds.getWest();
  const east = bounds.getEast();
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const latitude = south + (north - south) * (0.06 + random() * 0.88);
    const longitude = west + (east - west) * (0.06 + random() * 0.88);
    if (pointInGeometry(longitude, latitude, layer.feature?.geometry)) return [latitude, longitude];
  }
  const center = bounds.getCenter();
  return [center.lat, center.lng];
}

function propertyMarkerIcon() {
  return L.divIcon({
    className: "property-marker",
    html: `<span class="property-marker__pin" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 21s6-6.5 6-12a6 6 0 1 0-12 0c0 5.5 6 12 6 12Z"/><circle cx="12" cy="9" r="2"/></svg></span>`,
    iconSize: [30, 36],
    iconAnchor: [15, 33],
    tooltipAnchor: [0, -29],
  });
}

function propertyMarkerTooltip(item) {
  const offer = preferredOffer(item);
  const price = currentOfferPrice(offer);
  const priceText = price === null
    ? "Цена не указана"
    : `${formatNumber(price)} ${offer.currency || ""}`.trim();
  const address = [item.street, item.district].filter(Boolean).join(", ");
  return `<div class="property-marker-preview">
    <strong>${escapeHtml(item.title || "Объявление")}</strong>
    <span>${escapeHtml(priceText)}</span>
    ${address ? `<small>${escapeHtml(address)}</small>` : ""}
  </div>`;
}

function renderPropertyMarkers(items) {
  if (state.propertyMarkersLayer) state.propertyMarkersLayer.remove();
  state.propertyMarkersLayer = null;
  if (!state.selectedDistrict) return;

  const markers = L.layerGroup();
  items.forEach((item) => {
    const precision = listingLocationPrecision(item);
    if (precision === "district") return;
    const position = precision === "coordinates"
      ? validListingCoordinates(item)
      : approximateAddressCoordinates(item);
    if (!position) return;
    const marker = L.marker(position, {
      pane: "propertyMarkers",
      icon: propertyMarkerIcon(),
      keyboard: true,
      riseOnHover: true,
      title: item.title || "Открыть объявление",
    });
    marker.bindTooltip(propertyMarkerTooltip(item), {
      direction: "top",
      className: "property-marker-tooltip",
      opacity: 1,
    });
    marker.on("click", (event) => {
      if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
      openListingDialog(item);
    });
    marker.addTo(markers);
  });
  state.propertyMarkersLayer = markers.addTo(map);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// UI estimate for a comparable summary; source prices remain unchanged.
const UZS_PER_CURRENCY = {
  UZS: 1,
  USD: 12_500,
  EUR: 13_600,
  RUB: 140,
};

function priceInUzs(item) {
  const rawPrice = item.price ?? item.monthly_rent;
  const value = Number(rawPrice);
  if (!Number.isFinite(value) || value <= 0) return null;
  const currency = String(item.currency || item.rent_currency || "UZS").toUpperCase();
  const rate = UZS_PER_CURRENCY[currency];
  return rate ? value * rate : null;
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function formatMillionsUzs(value) {
  if (value === null) return "Цена не указана";
  const millions = value / 1_000_000;
  return `≈ ${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(millions)} млн сум`;
}

const DISTRICT_LANDMARKS = {
  "Алмазарский район": "/assets/districts/almazar.png",
  "Бектемирский район": "/assets/districts/bektemir.png",
  "Мирабадский район": "/assets/districts/mirabad.png",
  "Мирзо-Улугбекский район": "/assets/districts/mirzo-ulugbek.png",
  "Сергелийский район": "/assets/districts/sergeli.png",
  "Учтепинский район": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Street_in_Uchtepa_district%2C_Tashkent.jpg/960px-Street_in_Uchtepa_district%2C_Tashkent.jpg",
  "Чиланзарский район": "/assets/districts/chilanzar.png",
  "Шайхантахурский район": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/Tashkent_City_Park_at_night_2.jpg/960px-Tashkent_City_Park_at_night_2.jpg",
  "Яккасарайский район": "/assets/districts/yakkasaray.png",
  "Янгихаётский район": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bc/Yangihayot_station_of_Tashkent_Metro_-_hall_and_information_signs.jpg/960px-Yangihayot_station_of_Tashkent_Metro_-_hall_and_information_signs.jpg",
  "Яшнабадский район": "/assets/districts/yashnabad.png",
  "Юнусабадский район": "/assets/districts/yunusabad.png",
};

const DISTRICT_DISPLAY_NAMES = {
  "Ташкентский район": "Келес / Ташкентский район",
};

const DISTRICT_SUMMARIES = {
  "Алмазарский район": "Северо-западная часть Ташкента с историческими махаллями и важными городскими магистралями. Здесь расположены комплекс Хазрати Имам и крупные образовательные учреждения; застройка сочетает частные дома, советские кварталы и новые жилые комплексы.",
  "Бектемирский район": "Юго-восточная окраина города у реки Чирчик, где городская среда постепенно переходит в более открытую пригородную территорию. Район отличается промышленными зонами, спокойными махаллями и преимущественно малоэтажной застройкой с отдельными новыми домами.",
  "Мирабадский район": "Центральный район вокруг Северного вокзала и улиц Шахрисабз, Нукусской и Тараса Шевченко. Деловая и культурная среда соседствует с зелёными улицами; в застройке представлены дореволюционные дома, советские кварталы и современные высотные комплексы.",
  "Мирзо-Улугбекский район": "Восточная часть Ташкента, вытянутая от центральных кварталов к массивам Карасу и городской периферии. Район известен университетами, парками и научными учреждениями; здесь соседствуют частный сектор, типовые многоэтажки и новые жилые комплексы.",
  "Сергелийский район": "Южная часть столицы с прямым выходом к крупным транспортным коридорам и линии метро. В районе много новых жилых массивов и современных многоэтажных домов, а ближе к окраинам сохраняются промышленные территории и кварталы частной застройки.",
  "Учтепинский район": "Западный жилой район с плотной сетью махаллей, рынков и повседневной городской инфраструктуры. Основу среды составляют кварталы советских многоэтажек и частные дома, которые дополняются точечной современной застройкой.",
  "Чиланзарский район": "Крупный жилой район к юго-западу от центра, сформированный вдоль проспекта Бунёдкор и линии метро. Известен Magic City и зелёными бульварами; преобладают советские микрорайоны, дополненные современными жилыми и коммерческими комплексами.",
  "Шайхантахурский район": "Историческая западная часть центра с рынком Чорсу, медресе Кукельдаш и современным Tashkent City. Плотная городская ткань объединяет старые махалли, административные здания, типовые дома и новую высотную застройку.",
  "Яккасарайский район": "Компактная территория к югу от центра вдоль улицы Шота Руставели и Малой кольцевой дороги. Район ценят за близость к деловой части города; здесь много тихих частных улиц, кирпичных домов среднего этажа и современных комплексов.",
  "Янгихаётский район": "Самый южный район Ташкента с обширными территориями, где городская застройка переходит в пригородную. Здесь развиваются новые жилые массивы и транспортная инфраструктура, при этом значительную часть среды составляют махалли и частные дома.",
  "Яшнабадский район": "Восточная и юго-восточная часть города рядом с аэропортом, парком Ашхабад и массивом Тузель. Район сочетает широкие магистрали, зелёные кварталы, промышленные зоны, советские многоэтажки и активно строящиеся жилые комплексы.",
  "Юнусабадский район": "Северная часть столицы с телебашней, Японским садом, мечетью Минор и крупными зелёными зонами. Рельеф местами более выраженный, чем в центре; застройка представлена просторными советскими массивами, частными кварталами и новыми высотными домами.",
  "Бекабадский район": "Юго-восток Ташкентской области у Сырдарьи и государственной границы. Территория связана с промышленным Бекабадом и сельскими поселениями; жильё преимущественно малоэтажное и частное, с отдельными городскими кварталами.",
  "Бостанлыкский район": "Горный северо-восток области с Чарвакским водохранилищем, Чимганом и курортными посёлками. Рельеф определяет рассредоточенную малоэтажную застройку: частные дома, дачи, коттеджи и гостинично-рекреационные объекты.",
  "Букинский район": "Равнинная сельскохозяйственная территория на юге области с городом Бука и сетью небольших поселений. В жилой среде преобладают частные дома, большие участки и компактные малоэтажные кварталы.",
  "Чиназский район": "Западная часть области на важном автомобильном направлении из Ташкента к другим регионам страны. Район объединяет город Чиназ и сельские поселения; характерны частные дома, малоэтажные кварталы и придорожная коммерческая застройка.",
  "Кибрайский район": "Ближайший северо-восточный пригород столицы с зелёными посёлками и удобными выездами в Ташкент и Чирчик. Здесь распространены частные дома и коттеджные массивы, а рядом с городом появляется всё больше новых жилых проектов.",
  "Ахангаранский район": "Восточная часть области в долине реки Ахангаран, связанная с промышленными центрами Алмалык, Ангрен и Ахангаран. Городские кварталы чередуются с посёлками и открытыми предгорными территориями; застройка смешанная, от квартир до частных домов.",
  "Аккурганский район": "Равнинный аграрный район в южной части области с небольшими городскими и сельскими поселениями. Жилая среда спокойная и преимущественно малоэтажная, основу составляют частные дома с приусадебными участками.",
  "Паркентский район": "Предгорная территория к востоку от Ташкента, известная виноградниками, садами и направлениями Сукок и Кумушкан. Поселения располагаются среди холмистого ландшафта; преобладают частные дома, дачи и коттеджная застройка.",
  "Пскентский район": "Юго-восточная равнинно-предгорная часть области с небольшими населёнными пунктами и сельскохозяйственными землями. Основной тип жилья — частные малоэтажные дома, в районном центре встречаются компактные квартирные кварталы.",
  "Куйичирчикский район": "Равнинная территория вдоль нижнего течения Чирчика, занятая преимущественно сельскохозяйственными землями и посёлками. Застройка невысокая и разреженная, с доминированием частных домов и дворов.",
  "Уртачирчикский район": "Центральная часть области вокруг Нурафшана, связанная с Ташкентом сетью пригородных дорог. Здесь сельские поселения соседствуют с быстро развивающейся административной застройкой, новыми домами и частными кварталами.",
  "Янгиюльский район": "Западный пригородный пояс столицы вокруг Янгиюля и крупных транспортных маршрутов. Жилая среда сочетает городские многоэтажные кварталы, промышленную застройку, махалли и обширный частный сектор.",
  "Юкоричирчикский район": "Восточная часть столичной агломерации в долине Чирчика с садами, посёлками и близостью к предгорьям. В застройке преобладают частные дома и малоэтажные кварталы, новые проекты концентрируются вдоль основных дорог.",
  "Зангиатинский район": "Плотно развивающийся западный и юго-западный пригород Ташкента, связанный со столицей ежедневными транспортными потоками. Старые махалли и частные дома здесь соседствуют с логистическими объектами и новыми жилыми комплексами.",
  "Ташкентский район": "Северный пригород столицы вокруг Келеса, непосредственно примыкающий к городской границе. Район плотно заселён и хорошо связан с Ташкентом; основу застройки составляют частные кварталы, к которым добавляются новые мало- и среднеэтажные дома.",
};

function districtDisplayName(name) {
  return DISTRICT_DISPLAY_NAMES[name] || name;
}

function landmarkForDistrict(name) {
  return DISTRICT_LANDMARKS[name] || "";
}

function summaryForDistrict(name) {
  return DISTRICT_SUMMARIES[name]
    || `${districtDisplayName(name)} — территория Ташкентской области с преимущественно малоэтажной жилой средой. Характер застройки меняется от городских кварталов у районного центра до частных домов и сельских поселений.`;
}

function districtPreviewHtml(name) {
  const isRegionDistrict = state.scope !== "city";
  const districtScope = isRegionDistrict ? "region" : "city";
  const districtItems = (state.data?.items || []).filter(
    (item) => item.scope === districtScope && item.district === name,
  );
  const medianPrice = median(
    districtItems
      .map(priceInUzs)
      .filter((value) => value !== null),
  );
  const districtPublicationCount = districtItems.length;
  const districtPhoto = isRegionDistrict ? "" : landmarkForDistrict(name);
  return `<div class="district-preview${districtPhoto ? " district-preview--with-photo" : ""}">
    <div class="district-preview__copy">
      <div class="district-preview__header">
        <span class="district-preview__title">${escapeHtml(districtDisplayName(name))}</span>
      </div>
      <p class="district-preview__summary">${escapeHtml(summaryForDistrict(name))}</p>
      <div class="district-preview__metrics">
        ${isRegionDistrict ? "" : `<span><small>Медианная цена</small><strong>${formatMillionsUzs(medianPrice)}</strong></span>`}
        <span><small>Объявлений</small><strong>${districtPublicationCount}</strong></span>
      </div>
    </div>
    ${districtPhoto ? `<div class="district-preview__photo" style="--landmark-url:url('${escapeHtml(districtPhoto)}')" role="img" aria-label="Вид района"></div>` : ""}
  </div>`;
}

const CITY_LANDMARKS = [
  { name: "Парк Алишера Навои", type: "park", lat: 41.3135, lng: 69.2430 },
  { name: "Площадь Амира Темура", type: "square", lat: 41.3111, lng: 69.2797 },
  { name: "Ташкент City Park", type: "park", lat: 41.3190, lng: 69.2350 },
  { name: "Японский сад", type: "garden", lat: 41.3385, lng: 69.2873 },
  { name: "Парк Magic City", type: "attraction", lat: 41.2990, lng: 69.2375 },
  { name: "Площадь Независимости", type: "square", lat: 41.3116, lng: 69.2695 },
];

const LANDMARK_ICONS = {
  park: '<svg viewBox="0 0 24 24"><path d="M12 3 6.5 10h3L5 16h5.2v4h3.6v-4H19l-4.5-6h3L12 3Z" /></svg>',
  garden: '<svg viewBox="0 0 24 24"><path d="M19.5 4.5c-6.4.2-10.7 2.8-11.7 7.2-.6 2.7.8 5 3.3 5.4 4.5.7 7.7-4.3 8.4-12.6Z" /><path d="M5 20c2.4-5.2 6.1-8.7 11.2-10.7" /></svg>',
  square: '<svg viewBox="0 0 24 24"><path d="M5 20h14M7 17h10M8 17V9m4 8V9m4 8V9M6 9h12l-1-3H7L6 9Z" /></svg>',
  attraction: '<svg viewBox="0 0 24 24"><path d="m12 3 2.3 5.2 5.7.6-4.3 3.9 1.2 5.6-4.9-2.9-4.9 2.9 1.2-5.6L4 8.8l5.7-.6L12 3Z" /></svg>',
};

function landmarkIcon(type) {
  return LANDMARK_ICONS[type] || LANDMARK_ICONS.square;
}

function renderCityLandmarks() {
  if (state.landmarksLayer) state.landmarksLayer.remove();
  if (state.scope !== "city") {
    state.landmarksLayer = null;
    return;
  }
  state.landmarksLayer = L.layerGroup().addTo(map);
  CITY_LANDMARKS.forEach((landmark) => {
    const marker = L.marker([landmark.lat, landmark.lng], {
      interactive: false,
      keyboard: false,
      icon: L.divIcon({
        className: "city-landmark-marker",
        html: `<span class="city-landmark-glyph" aria-hidden="true">${landmarkIcon(landmark.type)}</span>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      }),
    }).addTo(state.landmarksLayer);
    marker.bindTooltip(escapeHtml(landmark.name), {
      permanent: true,
      direction: "bottom",
      className: "city-landmark-label",
      offset: [0, 7],
    });
  });
}

const districtPalette = [
  "#83c5ab",
  "#e6b184",
  "#9dbce0",
  "#d8c56e",
  "#9fce8e",
  "#d795ae",
  "#7fb9bd",
  "#dda96f",
  "#aca0d6",
  "#8ec2a0",
  "#d8957f",
  "#7dbda4",
];

function colorForDistrict(name) {
  const scopeKey = state.scope === "city" ? "city" : "region";
  const entries = state.data?.districts?.[scopeKey] || [];
  const entry = entries.find((candidate) => candidate.name === name);
  const index = entry ? entries.indexOf(entry) : 0;
  return districtPalette[index % districtPalette.length];
}

function darkenColor(hexColor, amount = 0.18) {
  const value = Number.parseInt(hexColor.replace("#", ""), 16);
  const factor = 1 - amount;
  const red = Math.round(((value >> 16) & 255) * factor);
  const green = Math.round(((value >> 8) & 255) * factor);
  const blue = Math.round((value & 255) * factor);
  return `#${[red, green, blue].map((part) => part.toString(16).padStart(2, "0")).join("")}`;
}

function featureStyle(name, count = 0, mergedRegionCity = false) {
  const selected = name === state.selectedDistrict;
  if (state.scope === "city" && state.airQualityLayer) {
    return {
      color: selected ? "#344f46" : "#65766f",
      weight: 1.5,
      fillColor: "#fff",
      fillOpacity: 0,
      lineCap: "round",
      lineJoin: "round",
    };
  }
  const fillColor = count === 0 || (state.selectedDistrict && !selected) ? "#d7dbd9" : colorForDistrict(name);
  const fixedCityWeight = 1.5;
  if (mergedRegionCity) {
    return {
      color: fillColor,
      weight: 5,
      fillColor,
      fillOpacity: selected ? 0.94 : 0.82,
      lineCap: "round",
      lineJoin: "round",
    };
  }
  return {
    color: darkenColor(fillColor, selected ? 0.3 : 0.18),
    weight: state.scope === "city" ? fixedCityWeight : selected ? 3 : 1.25,
    fillColor,
    fillOpacity: selected ? 0.94 : 0.82,
    lineCap: "round",
    lineJoin: "round",
  };
}

const BOUNDARY_NAME_ALIASES = {
  "Сергилийский район": "Сергелийский район",
};

const REGION_CITY_PARENT_DISTRICTS = {
  "город Алмалык": "Ахангаранский район",
  "город Ангрен": "Ахангаранский район",
  "город Бекабад": "Бекабадский район",
  "город Ахангаран": "Ахангаранский район",
  "город Нурафшан": "Уртачирчикский район",
  "город Чирчик": "Кибрайский район",
  "город Янгиюль": "Янгиюльский район",
};

const REGION_CITY_NAMES = new Set(Object.keys(REGION_CITY_PARENT_DISTRICTS));

function canonicalBoundaryName(name) {
  return BOUNDARY_NAME_ALIASES[name] || REGION_CITY_PARENT_DISTRICTS[name] || name;
}

function isMergedRegionCity(feature) {
  return state.scope !== "city" && REGION_CITY_NAMES.has(feature?.properties?.ADM2_RU);
}

function boundaryName(layer) {
  return canonicalBoundaryName(layer.feature?.properties?.ADM2_RU);
}

function districtCountCircleSize(count, maxCount) {
  if (count <= 0) return 0;
  const ratio = Math.sqrt(count / Math.max(1, maxCount));
  return Math.round(19 + ratio * 10);
}

function cityLabelIcon(name, count, maxCount = count) {
  const shortName = name === "Ташкентский район"
    ? "Келес"
    : name.replace(" район", "").replace(/^город /, "").replace("ский", "").replace("ская", "");
  const circleSize = districtCountCircleSize(count, maxCount);
  const countCircle = count > 0
    ? `<span class="district-label-count" style="--count-circle-size:${circleSize}px">${count}</span>`
    : "";
  return L.divIcon({
    className: `district-label${state.selectedDistrict && name !== state.selectedDistrict ? " is-muted" : ""}`,
    html: `<span class="district-label-name">${shortName}</span>${countCircle}`,
    iconSize: [180, 58],
    iconAnchor: [90, 29],
  });
}

function updateDistrictLabelVisibility() {
  const districtLabelsVisible = map.getZoom() >= (state.scope === "city" ? DISTRICT_LABEL_MIN_ZOOM : REGION_LABEL_MIN_ZOOM);
  const landmarkLabelsVisible = state.scope === "city" && map.getZoom() >= LANDMARK_LABEL_MIN_ZOOM;
  if (state.labelsLayer) {
    state.labelsLayer.eachLayer((label) => {
      const element = label.getElement();
      if (element) element.classList.toggle("is-zoom-hidden", !districtLabelsVisible);
    });
  }
  if (state.landmarksLayer) {
    state.landmarksLayer.eachLayer((landmark) => {
      const markerElement = landmark.getElement?.();
      const tooltip = landmark.getTooltip?.();
      const element = tooltip?.getElement();
      if (markerElement) markerElement.classList.toggle("is-zoom-hidden", !landmarkLabelsVisible);
      if (element) element.classList.toggle("is-zoom-hidden", !landmarkLabelsVisible);
    });
  }
}

function updateBoundaryStyles() {
  if (!state.boundaryLayer) return;
  const counts = districtCounts();
  const maxCount = Math.max(1, ...counts.values());
  state.boundaryLayer.eachLayer((layer) => {
    const name = boundaryName(layer);
    const count = counts.get(name) || 0;
    layer.setStyle(featureStyle(name, count, Boolean(layer.isMergedRegionCity)));
    if (layer.setTooltipContent) {
      const previewHtml = districtPreviewHtml(name);
      if (previewHtml !== layer.districtPreviewHtml) {
        layer.setTooltipContent(previewHtml);
        layer.districtPreviewHtml = previewHtml;
      }
    }
  });
  if (state.labelsLayer) {
    state.labelsLayer.eachLayer((label) => {
      const count = counts.get(label.featureName) || 0;
      label.setIcon(cityLabelIcon(label.featureName, count, maxCount));
    });
  }
  updateDistrictLabelVisibility();
}

async function renderBoundaries() {
  cancelAnchoredZoom();
  syncMajorRoadLayerVisibility();
  const file = state.scope === "city"
    ? "/data/tashkent_city_districts.geojson"
    : "/data/tashkent_region_districts.geojson";
  const response = await fetch(file);
  if (!response.ok) throw new Error(`Границы карты не загружены: ${response.status}`);
  const geojson = await response.json();
  state.airQualityBoundaryGeometry = state.scope === "city" ? cityBoundaryGeometry(geojson) : null;
  const counts = districtCounts();
  const maxCount = Math.max(1, ...counts.values());

  map.setMinZoom(state.scope === "city" ? CITY_MIN_ZOOM : COUNTRY_MIN_ZOOM);
  map.setMaxZoom(state.scope === "city" ? CITY_MAX_ZOOM : COUNTRY_MAX_ZOOM);

  if (state.boundaryLayer) state.boundaryLayer.remove();
  if (state.labelsLayer) state.labelsLayer.remove();
  renderCityLandmarks();
  state.labelsLayer = L.layerGroup().addTo(map);

  state.boundaryLayer = L.geoJSON(geojson, {
    style: (feature) => {
      const name = canonicalBoundaryName(feature.properties.ADM2_RU);
      return featureStyle(name, counts.get(name) || 0, isMergedRegionCity(feature));
    },
    onEachFeature: (feature, layer) => {
      const name = canonicalBoundaryName(feature.properties.ADM2_RU);
      const mergedRegionCity = isMergedRegionCity(feature);
      layer.isMergedRegionCity = mergedRegionCity;
      const count = counts.get(name) || 0;
      const previewHtml = districtPreviewHtml(name);
      layer.districtPreviewHtml = previewHtml;
      layer.bindTooltip(previewHtml, {
        direction: "bottom",
        opacity: 1,
        interactive: true,
        sticky: true,
        pane: "districtTooltips",
        className: "district-preview-tooltip",
        offset: [0, 12],
      });
      layer.on({
        mouseover: () => {
          if (name !== state.selectedDistrict) {
            const hoverStyle = { fillOpacity: state.airQualityLayer && state.scope === "city" ? 0 : 0.9 };
            if (state.scope !== "city" && !mergedRegionCity) hoverStyle.weight = 2.2;
            layer.setStyle(hoverStyle);
          }
        },
        mouseout: () => updateBoundaryStyles(),
        click: () => selectDistrict(name, layer),
      });

      if (!mergedRegionCity) {
        const center = layer.getBounds().getCenter();
        const label = L.marker(center, {
          interactive: false,
          keyboard: false,
          pane: "districtLabels",
          icon: cityLabelIcon(name, count, maxCount),
        });
        label.featureName = name;
        label.addTo(state.labelsLayer);
      }
    },
  }).addTo(map);

  const padding = state.scope === "city" ? [28, 28] : [18, 18];
  map.fitBounds(state.boundaryLayer.getBounds(), { padding, animate: true });
  updateDistrictLabelVisibility();
}

function selectDistrict(name, layer) {
  if (state.selectedDistrict === name) {
    resetDistrictSelection();
    return;
  }
  state.selectedDistrict = name;
  if (!state.listVisible) setListVisibility(true);
  updateBoundaryStyles();
  layer.bringToFront();
  updateView();
  if (window.innerWidth <= 980) {
    document.querySelector(".results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function resetDistrictSelection() {
  state.selectedDistrict = null;
  updateBoundaryStyles();
  updateView();
}

function selectScope(scope) {
  if (state.scope === scope) return;
  state.scope = scope;
  state.selectedDistrict = null;
  syncAirQualityAvailability();
  document.querySelectorAll(".scope-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.scope === scope);
  });
  renderBoundaries().catch(showError);
  updateView();
}

function filteredItems(includeDistrict = true) {
  if (!state.data) return [];
  const listingScope = state.scope === "country" ? "region" : state.scope;
  const min = Number(state.filters.minPrice) || 0;
  const max = Number(state.filters.maxPrice) || Infinity;
  const result = state.data.items.filter((item) => {
    if (item.scope !== listingScope) return false;
    if (includeDistrict && state.selectedDistrict && item.district !== state.selectedDistrict) return false;
    const offers = offersFor(item);
    if (state.filters.rooms && Number(item.rooms) !== Number(state.filters.rooms)) return false;
    if (state.filters.buildingType && item.building_type !== state.filters.buildingType) return false;
    if (state.filters.transactionType && item.transaction_type !== state.filters.transactionType) return false;
    if (state.filters.newBuilding && String(item.is_new_building ?? "") !== state.filters.newBuilding) return false;
    if (state.filters.furnished && String(item.furnished ?? "") !== state.filters.furnished) return false;
    const hasDuplicates = Number(item.publication_count || offers.length) > 1;
    if (state.filters.duplicateMode === "unique" && hasDuplicates) return false;
    if (state.filters.duplicateMode === "duplicate" && !hasDuplicates) return false;
    if (state.filters.amenities.length && !state.filters.amenities.every((name) => item.amenities?.includes(name))) return false;
    if (state.filters.nearbyPlaces.length && !state.filters.nearbyPlaces.every((name) => {
      if (name === "__metro__") {
        return item.nearby_places?.some((place) => place.toLocaleLowerCase("ru-RU").includes("метро"));
      }
      return item.nearby_places?.includes(name);
    })) return false;
    const matchingPriceOffer = offers.some((offer) => {
      if (state.filters.source && offer.source !== state.filters.source) return false;
      if (state.filters.sellerGroup && sellerGroupFor(offer) !== state.filters.sellerGroup) return false;
      if (state.filters.activeOnly && offer.status !== "active") return false;
      if (state.filters.currency && offer.currency !== state.filters.currency) return false;
      const price = currentOfferPrice(offer);
      if (price === null) return min === 0 && max === Infinity && !state.filters.currency;
      return Number(price) >= min && Number(price) <= max;
    });
    if (!matchingPriceOffer) return false;
    const areaMin = Number(state.filters.areaMin) || 0;
    const areaMax = Number(state.filters.areaMax) || Infinity;
    if (item.area_m2 !== null && (Number(item.area_m2) < areaMin || Number(item.area_m2) > areaMax)) return false;
    if (item.area_m2 === null && (areaMin > 0 || areaMax < Infinity)) return false;
    const floorMin = Number(state.filters.floorMin) || 0;
    const floorMax = Number(state.filters.floorMax) || Infinity;
    if (item.floor !== null && (Number(item.floor) < floorMin || Number(item.floor) > floorMax)) return false;
    if (item.floor === null && (floorMin > 0 || floorMax < Infinity)) return false;
    return true;
  });

  const sorters = {
    newest: (a, b) => dateValue(b) - dateValue(a),
    "price-asc": (a, b) => (sortablePrice(a) ?? Infinity) - (sortablePrice(b) ?? Infinity),
    "price-desc": (a, b) => (sortablePrice(b) ?? -Infinity) - (sortablePrice(a) ?? -Infinity),
    "area-desc": (a, b) => (b.area_m2 ?? -Infinity) - (a.area_m2 ?? -Infinity),
  };
  return result.sort(sorters[state.filters.sort]);
}

const OWNER_SELLER_TYPES = new Set([
  "private", "owner", "likely_owner", "probable_owner", "probably_owner",
  "частник", "собственник", "скорее_всего_собственник", "вероятный_собственник",
]);
const PROFESSIONAL_SELLER_TYPES = new Set([
  "agent", "agency", "realtor", "likely_realtor", "probable_realtor", "probably_realtor", "official",
  "риэлтор", "риелтор", "агентство", "агент", "скорее_всего_риэлтор", "скорее_всего_риелтор",
  "вероятный_риэлтор", "вероятный_риелтор",
]);

function sellerGroupFor(item) {
  if (item.seller_group === "owner" || item.seller_group === "professional") {
    return item.seller_group;
  }
  const normalizeType = (value) => String(value || "")
    .trim()
    .toLocaleLowerCase("ru-RU")
    .replace(/[\s-]+/g, "_");
  const modelType = normalizeType(item.seller_status);
  if (modelType === "likely_owner") return "owner";
  if (modelType === "likely_realtor") return "professional";
  if (item.is_official_seller) return "professional";
  const types = [modelType];
  if (types.some((type) => OWNER_SELLER_TYPES.has(type))) return "owner";
  if (types.some((type) => PROFESSIONAL_SELLER_TYPES.has(type))) return "professional";
  return "unknown";
}

function fact(label) {
  const span = document.createElement("span");
  span.textContent = label;
  return span;
}

function readableValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "Не указано";
  return `${value}${suffix}`;
}

function booleanValue(value, positive, negative) {
  if (value === null || value === undefined || value === "") return "Не указано";
  return Number(value) === 1 || value === true ? positive : negative;
}

function publicationWord(value) {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "публикаций";
  if (mod10 === 1) return "публикация";
  if (mod10 >= 2 && mod10 <= 4) return "публикации";
  return "публикаций";
}

function duplicateSummary(item) {
  const offers = offersFor(item);
  if (offers.length <= 1) return "Уникальная публикация";
  const sources = [...new Set(offers.map((offer) => offer.source).filter(Boolean))];
  const location = sources.length > 1
    ? `на ${sources.length} площадках`
    : `на ${sourceDisplayName(sources[0] || item.source)}`;
  return `${offers.length} ${publicationWord(offers.length)} ${location}`;
}

function sellerTypeLabel(seller) {
  const group = sellerGroupFor(seller);
  if (seller.is_official_seller) return "Официальный продавец";
  if (group === "owner") return "Собственник / вероятный собственник";
  if (group === "professional") return "Риэлтор / агентство";
  return readableValue(seller.seller_status || seller.source_seller_role);
}

function addDetailValue(container, label, value, options = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "listing-detail__value";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  if (options.href && String(options.href).startsWith("http")) {
    const link = document.createElement("a");
    link.href = options.href;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = value || "Открыть";
    description.append(link);
  } else {
    description.textContent = readableValue(value);
    if (value === null || value === undefined || value === "") description.classList.add("is-empty");
  }
  wrapper.append(term, description);
  container.append(wrapper);
}

function appendTags(container, values, limit = 5) {
  const unique = [...new Set((values || []).filter(Boolean))];
  if (!unique.length) {
    const empty = document.createElement("p");
    empty.className = "listing-detail__empty";
    empty.textContent = "Данные не указаны";
    container.append(empty);
    return;
  }
  unique.slice(0, limit).forEach((value) => container.append(fact(value)));
  if (unique.length > limit) container.append(fact(`+${unique.length - limit}`));
}

function sellerContactsFor(offer) {
  if (Array.isArray(offer.seller_contacts) && offer.seller_contacts.length) {
    return offer.seller_contacts.map((contact) => ({
      ...contact,
      seller_group: contact.seller_group || offer.seller_group,
      seller_status: contact.seller_status || offer.seller_status,
      source_seller_role: contact.source_seller_role || offer.source_seller_role,
    }));
  }
  return [offer];
}

function openListingDialog(item) {
  if (!elements.listingDialog || !elements.listingDialogContent) return;
  const offers = offersFor(item);
  const detail = document.createElement("article");
  detail.className = "listing-detail";

  const header = document.createElement("header");
  header.className = "listing-detail__header";
  const headerCopy = document.createElement("div");
  headerCopy.className = "listing-detail__header-copy";
  const badges = document.createElement("div");
  badges.className = "listing-detail__badges";
  const sourceBadge = document.createElement("span");
  sourceBadge.className = "source-badge";
  sourceBadge.textContent = [...new Set(offers.map((offer) => sourceDisplayName(offer.source)))].join(" · ");
  const duplicateBadge = document.createElement("span");
  duplicateBadge.className = `duplicate-badge ${offers.length > 1 ? "is-duplicate" : "is-unique"}`;
  duplicateBadge.textContent = duplicateSummary(item);
  badges.append(sourceBadge, duplicateBadge);
  const title = document.createElement("h2");
  title.id = "listing-dialog-title";
  title.textContent = item.title || "Объявление без заголовка";
  const address = document.createElement("p");
  address.className = "listing-detail__address";
  address.textContent = [item.city, item.district, item.street].filter(Boolean).join(", ") || "Адрес не указан";
  headerCopy.append(badges, title, address);
  header.append(headerCopy);

  const imageUrl = item.image_url || offers.find((offer) => offer.image_url)?.image_url;
  if (imageUrl) {
    const media = document.createElement("div");
    media.className = "listing-detail__media";
    const image = document.createElement("img");
    image.src = imageUrl;
    image.alt = item.title ? `Фото: ${item.title}` : "Фото жилья";
    image.addEventListener("error", () => media.remove(), { once: true });
    media.append(image);
    header.append(media);
  }
  detail.append(header);

  const columns = document.createElement("div");
  columns.className = "listing-detail__columns";
  const main = document.createElement("div");
  main.className = "listing-detail__main";
  const housingSection = document.createElement("section");
  const housingTitle = document.createElement("h3");
  housingTitle.textContent = "Характеристики жилья";
  const housingGrid = document.createElement("dl");
  housingGrid.className = "listing-detail__grid";
  addDetailValue(housingGrid, "Комнаты", item.rooms);
  addDetailValue(housingGrid, "Площадь", item.area_m2 !== null && item.area_m2 !== undefined ? `${formatNumber(item.area_m2)} м²` : null);
  addDetailValue(housingGrid, "Этаж", item.floor !== null && item.floor !== undefined ? `${item.floor}${item.floors_total ? ` из ${item.floors_total}` : ""}` : null);
  addDetailValue(housingGrid, "Тип жилья", item.building_type);
  addDetailValue(housingGrid, "Фонд", booleanValue(item.is_new_building, "Новостройка", "Вторичный фонд"));
  addDetailValue(housingGrid, "Материал / основание", item.foundation_type);
  addDetailValue(housingGrid, "Жилой комплекс", item.residential_complex_name);
  addDetailValue(housingGrid, "Мебель", booleanValue(item.furnished, "Есть", "Нет"));
  addDetailValue(housingGrid, "Цена за м²", item.price_per_m2 !== null && item.price_per_m2 !== undefined ? `${formatNumber(item.price_per_m2)} ${item.rent_currency || item.currency || ""}`.trim() : null);
  housingSection.append(housingTitle, housingGrid);

  const descriptionSection = document.createElement("section");
  const descriptionTitle = document.createElement("h3");
  descriptionTitle.textContent = "Описание";
  const description = document.createElement("p");
  description.className = "listing-detail__description";
  description.textContent = item.description || "Описание не указано";
  descriptionSection.append(descriptionTitle, description);

  const features = document.createElement("div");
  features.className = "listing-detail__feature-columns";
  [["Удобства", item.amenities], ["Рядом", item.nearby_places]].forEach(([label, values]) => {
    const section = document.createElement("section");
    const heading = document.createElement("h3");
    heading.textContent = label;
    const tags = document.createElement("div");
    tags.className = "listing-detail__tags";
    appendTags(tags, values);
    section.append(heading, tags);
    features.append(section);
  });
  main.append(housingSection, descriptionSection, features);

  const sidebar = document.createElement("aside");
  sidebar.className = "listing-detail__offers";
  sidebar.classList.toggle("is-multiple", offers.length > 1);
  const offersTitle = document.createElement("h3");
  offersTitle.textContent = offers.length > 1 ? "Публикации и продавцы" : "Публикация и продавец";
  sidebar.append(offersTitle);

  offers.forEach((offer) => {
    const offerCard = document.createElement("section");
    offerCard.className = "listing-detail__offer";
    const offerHead = document.createElement("div");
    offerHead.className = "listing-detail__offer-head";
    const offerSource = document.createElement("strong");
    offerSource.textContent = sourceDisplayName(offer.source);
    const offerStatus = document.createElement("span");
    offerStatus.textContent = offer.status === "active" ? "Активно" : offer.status === "closed" ? "Закрыто" : readableValue(offer.status);
    offerHead.append(offerSource, offerStatus);
    const offerPrice = document.createElement("p");
    offerPrice.className = "listing-detail__offer-price";
    const currentPrice = currentOfferPrice(offer);
    offerPrice.textContent = currentPrice !== null ? `${formatNumber(currentPrice)} ${offer.currency || ""}`.trim() : "Цена не указана";
    offerCard.append(offerHead, offerPrice);

    if (offer.previous_price !== null && offer.previous_price !== undefined && Number(offer.previous_price) !== Number(currentPrice)) {
      const change = document.createElement("p");
      change.className = "price-change-badge";
      const direction = currentPrice !== null && Number(currentPrice) < Number(offer.previous_price) ? "↓" : "↑";
      change.textContent = `${direction} было ${formatNumber(offer.previous_price)} ${offer.previous_currency || offer.currency || ""}${offer.price_changed_at ? ` · ${formatDateTime(offer.price_changed_at)}` : ""}`.trim();
      offerCard.append(change);
    }

    const publicationGrid = document.createElement("dl");
    publicationGrid.className = "listing-detail__offer-meta";
    addDetailValue(publicationGrid, "Опубликовано", formatDateTime(offer.published_at) || formatDate(offer.published_at));
    offerCard.append(publicationGrid);

    const contacts = sellerContactsFor(offer);
    const sellerBlock = document.createElement("div");
    sellerBlock.className = "listing-detail__seller";
    const sellerName = document.createElement("h4");
    const sellerNames = [...new Set(contacts.map((seller) => seller.name || seller.seller_name).filter(Boolean))];
    sellerName.textContent = sellerNames.join(" · ") || "Продавец не указан";
    const sellerGrid = document.createElement("dl");
    sellerGrid.className = "listing-detail__seller-grid";
    const phones = [...new Set(contacts.map((seller) => seller.phone || seller.seller_phone).filter(Boolean))];
    const sellerTypes = [...new Set(contacts.map(sellerTypeLabel).filter((value) => value && value !== "Не указано"))];
    addDetailValue(sellerGrid, "Телефон", phones.join(" · ") || null);
    addDetailValue(sellerGrid, "Тип", sellerTypes.join(" · ") || null);
    sellerBlock.append(sellerName, sellerGrid);
    offerCard.append(sellerBlock);

    if (offer.url) {
      const sourceLink = document.createElement("a");
      sourceLink.className = "listing-detail__source-link";
      sourceLink.href = offer.url;
      sourceLink.target = "_blank";
      sourceLink.rel = "noreferrer";
      sourceLink.textContent = "Открыть";
      const arrow = document.createElement("span");
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "↗";
      sourceLink.append(arrow);
      offerCard.append(sourceLink);
    }
    sidebar.append(offerCard);
  });

  columns.append(main, sidebar);
  detail.append(columns);
  elements.listingDialogContent.replaceChildren(detail);
  if (!elements.listingDialog.open) elements.listingDialog.showModal();
  document.body.classList.add("has-open-dialog");
}

function listingCard(item) {
  const card = elements.template.content.firstElementChild.cloneNode(true);
  const sources = item.sources?.length ? item.sources : [item.source];
  card.querySelector(".source-badge").textContent = sources.map(sourceDisplayName).join(" · ");
  card.querySelector(".date").textContent = formatDate(item.published_at || item.first_seen_at);
  card.querySelector("h2").textContent = item.title;

  const media = card.querySelector(".card-media");
  const image = media.querySelector("img");
  if (item.image_url) {
    image.src = item.image_url;
    image.alt = item.title ? `Фото: ${item.title}` : "Фото объявления";
    media.hidden = false;
    card.classList.add("has-image");
    image.addEventListener("error", () => {
      media.hidden = true;
      card.classList.remove("has-image");
    }, { once: true });
  }

  const facts = card.querySelector(".property-facts");
  if (item.rooms) facts.append(fact(`${item.rooms} комн.`));
  if (item.area_m2) facts.append(fact(`${formatNumber(item.area_m2)} м²`));
  if (item.floor) facts.append(fact(`${item.floor}${item.floors_total ? `/${item.floors_total}` : ""} этаж`));
  if (item.is_new_building === 1) facts.append(fact("Новостройка"));
  if (item.furnished === 1) facts.append(fact("С мебелью"));
  if (item.residential_complex_name) facts.append(fact(item.residential_complex_name));
  if (!facts.children.length) facts.remove();

  const location = [item.district, item.street].filter(Boolean).join(", ") || item.city || "Локация не указана";
  card.querySelector(".location-line span").textContent = location;

  const price = card.querySelector(".price");
  const offers = offersFor(item);
  price.classList.toggle("has-multiple-offers", offers.length > 1);
  offers.forEach((offer) => {
    const row = document.createElement("div");
    row.className = "offer-price-row";
    if (offers.length > 1) {
      const source = document.createElement("span");
      source.className = "offer-price-source";
      source.textContent = sourceDisplayName(offer.source);
      row.append(source);
    }
    const value = document.createElement("strong");
    const currentPrice = currentOfferPrice(offer);
    value.textContent = currentPrice !== null
      ? `${formatNumber(currentPrice)} ${offer.currency || ""}`.trim()
      : "Цена не указана";
    row.append(value);
    if (item.transaction_type === "rent") {
      const small = document.createElement("small");
      small.textContent = "в месяц";
      row.append(small);
    }
    price.append(row);

    const previousPrice = offer.previous_price;
    if (previousPrice !== null && previousPrice !== undefined && Number(previousPrice) !== Number(currentPrice)) {
      const history = document.createElement("div");
      history.className = "price-change-badge";
      const direction = currentPrice !== null && Number(currentPrice) < Number(previousPrice) ? "↓" : "↑";
      const changedAt = formatDateTime(offer.price_changed_at);
      history.textContent = `${direction} было ${formatNumber(previousPrice)} ${offer.previous_currency || offer.currency || ""}${changedAt ? ` · ${changedAt}` : ""}`.trim();
      price.append(history);
    }
  });

  card.setAttribute("aria-label", `Открыть подробности: ${item.title || "объявление"}`);
  card.addEventListener("click", () => openListingDialog(item));
  card.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openListingDialog(item);
  });
  return card;
}

function renderListings(items) {
  elements.list.replaceChildren();
  if (!items.length) {
    elements.list.innerHTML = `
      <div class="empty-state">
        <span aria-hidden="true">⌁</span>
        <strong>Объявлений не найдено</strong>
        <p>Попробуйте выбрать другой район или ослабить фильтры.</p>
      </div>`;
    return;
  }
  if (!state.selectedDistrict) {
    const fragment = document.createDocumentFragment();
    items.forEach((item) => fragment.append(listingCard(item)));
    elements.list.append(fragment);
    return;
  }

  const mappedItems = items.filter((item) => listingLocationPrecision(item) !== "district");
  const districtOnlyItems = items.filter((item) => listingLocationPrecision(item) === "district");
  const appendSublist = (title, description, sublistItems, modifier = "") => {
    if (!sublistItems.length) return;
    const section = document.createElement("section");
    section.className = `listing-sublist${modifier ? ` ${modifier}` : ""}`;
    const heading = document.createElement("div");
    heading.className = "listing-sublist__heading";
    const copy = document.createElement("div");
    const name = document.createElement("h2");
    name.textContent = title;
    const note = document.createElement("p");
    note.textContent = description;
    const count = document.createElement("span");
    count.textContent = sublistItems.length;
    copy.append(name, note);
    heading.append(copy, count);
    const cards = document.createElement("div");
    cards.className = "listing-sublist__cards";
    sublistItems.forEach((item) => cards.append(listingCard(item)));
    section.append(heading, cards);
    elements.list.append(section);
  };

  appendSublist(
    "Квартиры на карте",
    "Координаты, адрес, улица или известный жилой комплекс",
    mappedItems,
  );
  appendSublist(
    "Локация в пределах района",
    "В публикации нет данных точнее названия района",
    districtOnlyItems,
    "listing-sublist--district-only",
  );
}

function sourceDisplayName(source) {
  const names = {
    olx: "OLX",
    etagi: "Этажи",
    uybor: "Uybor",
    realting: "Realting",
    realt24: "Realt24",
  };
  return names[String(source).toLowerCase()] || String(source);
}

function renderSourceStrip(sources) {
  if (!elements.sourceStrip) return;
  elements.sourceStrip.replaceChildren();
  const visibleSources = (sources || []).filter(Boolean);
  if (!visibleSources.length) {
    elements.sourceStrip.hidden = true;
    return;
  }
  elements.sourceStrip.hidden = false;
  visibleSources.forEach((source) => {
    const badge = document.createElement("span");
    badge.className = "source-strip__item";
    badge.textContent = sourceDisplayName(source);
    elements.sourceStrip.append(badge);
  });
}

function activeFiltersCount() {
  return [
    state.filters.minPrice,
    state.filters.maxPrice,
    state.filters.areaMin,
    state.filters.areaMax,
    state.filters.floorMin,
    state.filters.floorMax,
    state.filters.currency,
    state.filters.rooms,
    state.filters.buildingType,
    state.filters.transactionType,
    state.filters.newBuilding,
    state.filters.furnished,
    state.filters.source,
    state.filters.duplicateMode,
    state.filters.sellerGroup,
  ]
    .filter(Boolean).length + state.filters.amenities.length + state.filters.nearbyPlaces.length + (state.filters.activeOnly ? 0 : 1);
}

function syncMapFilterDock() {
  const groups = [
    ["amenity-selected-count", state.filters.amenities.length],
    ["nearby-selected-count", state.filters.nearbyPlaces.length],
  ];
  groups.forEach(([id, count]) => {
    const badge = document.querySelector(`#${id}`);
    badge.hidden = count === 0;
    badge.textContent = count;
    badge.closest(".map-filter-menu").classList.toggle("has-selection", count > 0);
  });
  document.querySelector("#reset-map-filters").hidden = !(
    state.filters.amenities.length || state.filters.nearbyPlaces.length || state.filters.sellerGroup
  );
  document.querySelectorAll("[data-quick-filter]").forEach((button) => {
    const key = button.dataset.quickFilter;
    if (key === "metro") {
      button.setAttribute("aria-pressed", String(state.filters.nearbyPlaces.includes("__metro__")));
      return;
    }
    const active = key === "any" ? !state.filters.sellerGroup : state.filters.sellerGroup === key;
    button.setAttribute("aria-checked", String(active));
  });
}

function toggleQuickFilter(key) {
  if (key === "metro") {
    const values = new Set(state.filters.nearbyPlaces);
    if (values.has("__metro__")) values.delete("__metro__");
    else values.add("__metro__");
    state.filters.nearbyPlaces = [...values];
    document.querySelectorAll("#nearby-options input").forEach((input) => {
      if (input.value === "__metro__") input.checked = values.has("__metro__");
    });
  } else if (key === "any") {
    state.filters.sellerGroup = "";
  } else if (key === "owner" || key === "professional") {
    state.filters.sellerGroup = key;
  }
  updateView();
}

function setListVisibility(visible) {
  state.listVisible = visible;
  elements.workspace.classList.toggle("list-collapsed", !visible);
  elements.listToggle.setAttribute("aria-expanded", String(visible));
  elements.listToggle.setAttribute("aria-label", visible ? "Скрыть список" : "Показать список");
}

function resetMapFilters() {
  state.filters.amenities = [];
  state.filters.nearbyPlaces = [];
  state.filters.sellerGroup = "";
  document.querySelectorAll("#amenity-options input, #nearby-options input").forEach((input) => {
    input.checked = false;
  });
  updateView();
}

function updateView() {
  if (!state.data) return;
  const items = filteredItems();
  const scopeTitle = state.scope === "city" ? "Ташкент" : "Ташкентская область";
  const selectedCount = districtCounts().get(state.selectedDistrict) || 0;

  elements.eyebrow.textContent = state.selectedDistrict ? scopeTitle : "Вся территория";
  elements.title.textContent = state.selectedDistrict ? districtDisplayName(state.selectedDistrict) : scopeTitle;
  elements.copy.textContent = state.selectedDistrict
    ? `${formatCount(selectedCount)} по текущим фильтрам`
    : state.scope === "city"
      ? "Объявления во всех районах города"
      : "Объявления по районам и городам Ташкентской области";
  elements.clearDistrict.hidden = !state.selectedDistrict;
  elements.resultCount.textContent = items.length;
  elements.resultWord.textContent = countWord(items.length);

  const filterCount = activeFiltersCount();
  elements.activeFilterCount.hidden = filterCount === 0;
  elements.activeFilterCount.textContent = filterCount;
  syncMapFilterDock();
  renderPropertyMarkers(items);
  renderListings(items);
  updateBoundaryStyles();
}

function populateSelect(select, values, label) {
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label ? label(value) : value;
    select.append(option);
  });
}

function populateChoiceOptions(container, options, filterKey) {
  options.forEach(({ name, value, count }) => {
    const label = document.createElement("label");
    label.className = "choice-chip";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value || name;
    const text = document.createElement("span");
    text.className = "choice-label";
    text.append(document.createTextNode(name));
    const countNode = document.createElement("small");
    countNode.textContent = count;
    text.append(countNode);
    label.append(input, text);
    input.addEventListener("change", () => {
      state.filters[filterKey] = [...container.querySelectorAll("input:checked")].map((node) => node.value);
      updateView();
    });
    container.append(label);
  });
  if (!options.length) {
    const empty = document.createElement("small");
    empty.className = "advanced-empty";
    empty.textContent = "В текущей базе пока нет данных";
    container.append(empty);
  }
}

function wireEvents() {
  document.querySelectorAll(".scope-button").forEach((button) => {
    button.addEventListener("click", () => selectScope(button.dataset.scope));
  });
  elements.airQualityToggle.addEventListener("change", () => {
    setAirQualityEnabled(elements.airQualityToggle.checked);
  });
  elements.clearDistrict.addEventListener("click", () => {
    resetDistrictSelection();
  });
  elements.filterToggle.addEventListener("click", () => {
    const collapsed = elements.filters.classList.toggle("is-collapsed");
    elements.filterToggle.setAttribute("aria-expanded", String(!collapsed));
    elements.resultsPanel.classList.toggle("filters-open", !collapsed);
  });
  document.querySelectorAll("[data-quick-filter]").forEach((button) => {
    button.addEventListener("click", () => toggleQuickFilter(button.dataset.quickFilter));
  });
  elements.mapFilterMasterToggle.addEventListener("click", () => {
    const collapsed = elements.mapFilterDock.classList.toggle("is-collapsed");
    elements.mapFilterMasterToggle.setAttribute("aria-expanded", String(!collapsed));
    elements.mapFilterMasterToggle.setAttribute(
      "aria-label",
      collapsed ? "Открыть фильтры удобств и окружения" : "Свернуть фильтры удобств и окружения",
    );
  });
  elements.listToggle.addEventListener("click", () => {
    setListVisibility(!state.listVisible);
  });
  document.querySelector("#reset-map-filters").addEventListener("click", resetMapFilters);
  elements.listingDialogClose.addEventListener("click", () => elements.listingDialog.close());
  elements.listingDialog.addEventListener("click", (event) => {
    if (event.target === elements.listingDialog) elements.listingDialog.close();
  });
  elements.listingDialog.addEventListener("close", () => {
    document.body.classList.remove("has-open-dialog");
    elements.listingDialogContent.replaceChildren();
  });

  const bindings = {
    "price-min": "minPrice",
    "price-max": "maxPrice",
    "area-min": "areaMin",
    "area-max": "areaMax",
    "floor-min": "floorMin",
    "floor-max": "floorMax",
    currency: "currency",
    rooms: "rooms",
    "building-type": "buildingType",
    "transaction-type": "transactionType",
    "new-building": "newBuilding",
    furnished: "furnished",
    source: "source",
    "duplicate-mode": "duplicateMode",
    "active-only": "activeOnly",
  };
  Object.entries(bindings).forEach(([id, key]) => {
    const control = document.querySelector(`#${id}`);
    const eventName = control.tagName === "INPUT" && control.type === "number" ? "input" : "change";
    control.addEventListener(eventName, () => {
      state.filters[key] = control.type === "checkbox" ? control.checked : control.value;
      updateView();
    });
  });
  document.querySelectorAll('input[name="sort"]').forEach((control) => {
    control.addEventListener("change", () => {
      if (control.checked) {
        state.filters.sort = control.value;
        elements.sortCurrent.textContent = control.dataset.label || control.value;
        elements.sortMenu.removeAttribute("open");
        updateView();
      }
    });
  });
  document.addEventListener("pointerdown", (event) => {
    if (elements.sortMenu.open && !elements.sortMenu.contains(event.target)) {
      elements.sortMenu.removeAttribute("open");
    }
  });

  document.querySelector("#reset-filters").addEventListener("click", () => {
    state.filters = {
      minPrice: "", maxPrice: "", areaMin: "", areaMax: "", floorMin: "", floorMax: "",
      currency: "", rooms: "", buildingType: "", transactionType: "", newBuilding: "", furnished: "",
      source: "", duplicateMode: "", activeOnly: true, sort: "newest", amenities: [], nearbyPlaces: [], sellerGroup: "",
    };
    ["price-min", "price-max", "area-min", "area-max", "floor-min", "floor-max"].forEach((id) => {
      document.querySelector(`#${id}`).value = "";
    });
    ["currency", "rooms", "building-type", "transaction-type", "new-building", "furnished", "source", "duplicate-mode"].forEach((id) => {
      document.querySelector(`#${id}`).value = "";
    });
    const defaultSort = document.querySelector('input[name="sort"][value="newest"]');
    if (defaultSort) defaultSort.checked = true;
    elements.sortCurrent.textContent = defaultSort?.dataset.label || "Новые";
    elements.sortMenu.removeAttribute("open");
    document.querySelector("#active-only").checked = true;
    document.querySelectorAll("#amenity-options input, #nearby-options input").forEach((input) => { input.checked = false; });
    updateView();
  });
}

function showError(error) {
  console.error(error);
  elements.list.innerHTML = `
    <div class="error-state">
      <span aria-hidden="true">!</span>
      <strong>Не удалось загрузить данные</strong>
      <p>${error.message || "Проверьте, запущен ли локальный сервер."}</p>
    </div>`;
}

async function init() {
  wireEvents();
  syncAirQualityAvailability();
  try {
    const response = await fetch("/api/bootstrap");
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    state.data = payload;

    document.querySelector("#city-tab-count").textContent = formatCount(payload.stats.city);
    document.querySelector("#country-tab-count").textContent = formatCount(payload.stats.region);
    document.querySelector("#updated-at").textContent = `Обновлено ${formatDate(payload.generated_at)}`;
    renderSourceStrip(payload.options.sources || []);
    populateSelect(document.querySelector("#source"), payload.options.sources, (value) => value.toUpperCase());
    populateSelect(document.querySelector("#currency"), payload.options.currencies);
    populateSelect(document.querySelector("#rooms"), payload.options.rooms, (value) => `${value} комн.`);
    populateSelect(document.querySelector("#building-type"), payload.options.building_types || []);
    populateChoiceOptions(document.querySelector("#amenity-options"), payload.options.amenities || [], "amenities");
    populateChoiceOptions(document.querySelector("#nearby-options"), payload.options.nearby_places || [], "nearbyPlaces");

    await renderBoundaries();
    updateView();
    if (state.airQualityEnabled) loadAirQuality();
  } catch (error) {
    showError(error);
  }
}

loadSchematicBasemap();
init();
