# Synthetic GeoJSON Fixtures

These files provide tiny WGS84 GeoJSON layers for fast backend unit tests.
They are synthetic and should not be used as real park data.

## Files

| File | Geometry | Purpose |
| --- | --- | --- |
| `water_points.geojson` | `Point` | Mock springs, creek access, and lake outlet records for water proximity tests. |
| `elevation_contours.geojson` | `LineString` | Mock contour lines for vector loading and terrain-adjacent test cases. |
| `park_boundary.geojson` | `Polygon` | Mock park extent for clipping, bounds checks, and in-park point validation. |

## Shared Conventions

All fixtures use GeoJSON `FeatureCollection` format with longitude/latitude
coordinates in EPSG:4326 order. Every feature includes:

| Property | Type | Description |
| --- | --- | --- |
| `fixture_id` | string | Stable test identifier unique within a fixture file. |
| `source` | string | Always `synthetic` for these test fixtures. |

## Fixture Schemas

### `water_points.geojson`

| Property | Type | Description |
| --- | --- | --- |
| `name` | string | Human-readable mock feature name. |
| `water_type` | string | One of `spring`, `creek_access`, or `lake_outlet`. |
| `perennial` | boolean | Whether the mock water source should be treated as year-round. |

### `elevation_contours.geojson`

| Property | Type | Description |
| --- | --- | --- |
| `elevation_m` | integer | Synthetic contour elevation in meters. |
| `index_contour` | boolean | Whether the contour should be treated as an index contour. |

### `park_boundary.geojson`

| Property | Type | Description |
| --- | --- | --- |
| `park_name` | string | Synthetic park name. |
| `unit_code` | string | Mock four-letter park code. |
| `boundary_type` | string | Fixture boundary role, currently `test_extent`. |

## Performance Expectation

Each fixture is intentionally tiny and should load with `geopandas.read_file`
in under 100 ms on a normal development machine. Keep future fixture additions
small enough that automated test runs never depend on large raster downloads or
local raw-data caches.
