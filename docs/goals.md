# Project goals

Build an interactive, gamified geospatial tool that evaluates wilderness
survival resources, terrain difficulty, and risk zones for a target National
Park. The six-week deliverable is a documented, interactive demo covering one
park area.

## Primary user personas

| Persona | What they need |
| --- | --- |
| Wilderness scenario player | Explore unfamiliar terrain, find nearby water features and campsites, and compare locations using visible resource and difficulty scores. |
| Scenario designer or facilitator | Choose locations for a reality-competition-style scenario, compare their tradeoffs, and explain how terrain and water proximity affect each score. |

## Six-week milestones

| Weeks | Milestone | Completion criteria |
| --- | --- | --- |
| 1–2 | Data ingestion and CRS standardization | Select and document the park area and its bounding box. Run the downloader for USGS 3DEP elevation, OpenStreetMap features, and ESA WorldCover land cover. Verify the raw files and prepare analysis layers in a common projected coordinate reference system (CRS). |
| 3–4 | Spatial analysis and scoring | Generate aligned water-proximity and terrain-rugosity score rasters. Verify scores against known sample locations and check that NoData cells stay masked. |
| 5 | Interactive map and styling | Implement the reality-competition UI and Mapbox styling. Let users select a location and inspect its scores, nearby features, and relevant map layers. |
| 6 | Verification and demo | Measure the performance targets below, document setup and data sources in the GitHub repository, and record a 60-second video demo. |

## Performance benchmarks

| Operation | Target | Measurement |
| --- | --- | --- |
| Raster point query | Under 3 seconds at the 95th percentile | Measure from backend request receipt to return of the elevation and available score values for one point. |
| Map score update | Under 4 seconds at the 95th percentile | Measure from selecting a location to rendering its score panel with the map already open. |

Use the selected park dataset on the same machine with processed rasters on
local disk. Start a fresh app session, run one warm-up query, then time 30
queries at distinct in-bounds locations spread across the study area. Record
the machine specs, dataset extent, raster dimensions, and observed timings so
the benchmark can be repeated.
