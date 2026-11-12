"""Streamlit application interface for SurvivorMapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pydeck as pdk
import rasterio
import streamlit as st
from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"

VECTOR_SUFFIXES = {".geojson", ".json", ".gpkg", ".shp"}
RASTER_SUFFIXES = {".tif", ".tiff"}

MAP_STYLES = {
    "Light": pdk.map_styles.CARTO_LIGHT,
    "Dark": pdk.map_styles.CARTO_DARK,
    "Road": pdk.map_styles.ROAD,
}

DEFAULT_VIEW_STATE = {
    "latitude": 37.7470,
    "longitude": -119.6150,
    "zoom": 11,
    "pitch": 0,
}


@dataclass(frozen=True)
class DatasetInfo:
    """Metadata for a processed dataset discovered on local disk."""

    name: str
    path: str
    kind: str
    suffix: str


def main() -> None:
    """Render the Streamlit application."""
    st.set_page_config(
        page_title="SurvivorMapping",
        page_icon="SM",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    ensure_processed_directory()
    initialize_session_state()

    st.title("SurvivorMapping")
    st.caption("Interactive container for processed park layers, map previews, and scoring dashboards.")

    datasets = discover_processed_datasets(str(PROCESSED_DATA_DIR))
    selected_dataset = render_sidebar(datasets)

    map_column, detail_column = st.columns([3, 1], gap="large")
    with map_column:
        render_map_canvas(selected_dataset)
    with detail_column:
        render_dashboard(selected_dataset, datasets)


def ensure_processed_directory() -> None:
    """Create the local processed-data directory when it is absent."""
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)


def initialize_session_state() -> None:
    """Set default state values used across reruns."""
    defaults = {
        "selected_dataset": None,
        "map_style": "Light",
        "show_attribute_table": False,
        "last_refresh": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


@st.cache_data(show_spinner=False)
def discover_processed_datasets(directory: str) -> list[DatasetInfo]:
    """Find supported processed vector and raster datasets."""
    root = Path(directory)
    if not root.exists():
        return []

    datasets: list[DatasetInfo] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in VECTOR_SUFFIXES:
            kind = "vector"
        elif suffix in RASTER_SUFFIXES:
            kind = "raster"
        else:
            continue
        datasets.append(
            DatasetInfo(
                name=path.relative_to(root).as_posix(),
                path=str(path),
                kind=kind,
                suffix=suffix,
            )
        )
    return datasets


def render_sidebar(datasets: list[DatasetInfo]) -> DatasetInfo | None:
    """Render sidebar controls and return the selected dataset."""
    with st.sidebar:
        st.header("Controls")
        st.caption(str(PROCESSED_DATA_DIR))

        if st.button("Refresh datasets", use_container_width=True):
            st.cache_data.clear()
            st.session_state.last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st.rerun()

        st.selectbox(
            "Map style",
            options=list(MAP_STYLES),
            key="map_style",
        )
        st.toggle(
            "Show attributes",
            key="show_attribute_table",
            help="Display the first rows from the selected vector dataset.",
        )

        options = ["No dataset"] + [dataset.name for dataset in datasets]
        current = st.session_state.selected_dataset
        if current not in options:
            current = options[0]
            st.session_state.selected_dataset = current

        selected_name = st.selectbox(
            "Processed dataset",
            options=options,
            index=options.index(current),
            key="selected_dataset",
        )

        st.metric("Processed layers", len(datasets))
        st.caption(f"Last refresh: {st.session_state.last_refresh}")

    if selected_name == "No dataset":
        return None
    return next(dataset for dataset in datasets if dataset.name == selected_name)


def render_map_canvas(dataset: DatasetInfo | None) -> None:
    """Render the map viewer for the current dataset selection."""
    st.subheader("Map Viewer")

    if dataset is None:
        render_empty_map()
        st.info("No processed dataset is selected. Add GeoJSON, GeoPackage, Shapefile, or GeoTIFF files to data/processed/.")
        return

    if dataset.kind == "raster":
        render_raster_preview(dataset)
        return

    try:
        gdf = load_vector_dataset(dataset.path)
    except Exception as error:  # pragma: no cover - surfaced in Streamlit UI.
        st.error(f"Unable to load {dataset.name}: {error}")
        render_empty_map()
        return

    if gdf.empty:
        st.warning(f"{dataset.name} loaded, but it does not contain any features.")
        render_empty_map()
        return

    layers = build_vector_layers(gdf)
    if not layers:
        st.warning(f"{dataset.name} loaded, but its geometry types are not supported for preview yet.")
        render_empty_map()
        return

    st.pydeck_chart(
        pdk.Deck(
            map_style=MAP_STYLES[st.session_state.map_style],
            initial_view_state=build_view_state(gdf),
            layers=layers,
            tooltip={"text": "{fixture_id}\n{name}\n{park_name}"},
        ),
        use_container_width=True,
    )

    if st.session_state.show_attribute_table:
        st.dataframe(
            gdf.drop(columns="geometry").head(50),
            use_container_width=True,
            hide_index=True,
        )


def render_dashboard(dataset: DatasetInfo | None, datasets: list[DatasetInfo]) -> None:
    """Render dataset and app-state details beside the map."""
    st.subheader("Dashboard")
    st.metric("Available datasets", len(datasets))

    if dataset is None:
        st.write("Select a processed dataset to inspect feature counts, bounds, and layer metadata.")
        return

    st.write(dataset.name)
    st.caption(dataset.path)
    st.metric("Dataset type", dataset.kind.title())

    if dataset.kind == "raster":
        try:
            metadata = load_raster_metadata(dataset.path)
        except Exception as error:  # pragma: no cover - surfaced in Streamlit UI.
            st.error(f"Unable to inspect raster metadata: {error}")
            return
        render_key_value_table(metadata)
        return

    try:
        gdf = load_vector_dataset(dataset.path)
    except Exception as error:  # pragma: no cover - surfaced in Streamlit UI.
        st.error(f"Unable to inspect vector metadata: {error}")
        return

    minx, miny, maxx, maxy = gdf.total_bounds if not gdf.empty else (None, None, None, None)
    metadata = {
        "features": len(gdf),
        "crs": str(gdf.crs) if gdf.crs else "unknown",
        "geometry_types": ", ".join(sorted(gdf.geometry.geom_type.dropna().unique())),
        "west": minx,
        "south": miny,
        "east": maxx,
        "north": maxy,
    }
    render_key_value_table(metadata)


def render_empty_map() -> None:
    """Render a default empty map centered on the project example area."""
    st.pydeck_chart(
        pdk.Deck(
            map_style=MAP_STYLES[st.session_state.map_style],
            initial_view_state=pdk.ViewState(**DEFAULT_VIEW_STATE),
            layers=[],
        ),
        use_container_width=True,
    )


def render_raster_preview(dataset: DatasetInfo) -> None:
    """Display raster metadata until score rasters have a map renderer."""
    st.info(f"{dataset.name} is available. Raster map rendering will be added with scoring layers.")
    metadata = load_raster_metadata(dataset.path)
    render_key_value_table(metadata)
    render_empty_map()


@st.cache_data(show_spinner=False)
def load_vector_dataset(path: str) -> gpd.GeoDataFrame:
    """Load a vector layer and normalize it to WGS84 for map rendering."""
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    return gdf


@st.cache_data(show_spinner=False)
def load_raster_metadata(path: str) -> dict[str, Any]:
    """Load lightweight raster metadata without reading full raster arrays."""
    with rasterio.open(path) as dataset:
        bounds = dataset.bounds
        return {
            "driver": dataset.driver,
            "crs": str(dataset.crs),
            "width": dataset.width,
            "height": dataset.height,
            "bands": dataset.count,
            "west": bounds.left,
            "south": bounds.bottom,
            "east": bounds.right,
            "north": bounds.top,
        }


def build_vector_layers(gdf: gpd.GeoDataFrame) -> list[pdk.Layer]:
    """Build pydeck layers for point, line, and polygon vector previews."""
    layers: list[pdk.Layer] = []
    wgs84 = gdf[gdf.geometry.notna()].copy()
    if wgs84.empty:
        return layers

    point_records = extract_point_records(wgs84)
    if point_records:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=point_records,
                get_position="[longitude, latitude]",
                get_radius=55,
                get_fill_color=[35, 128, 205, 180],
                get_line_color=[255, 255, 255],
                line_width_min_pixels=1,
                pickable=True,
            )
        )

    line_records = extract_line_records(wgs84)
    if line_records:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=line_records,
                get_path="path",
                get_width=4,
                get_color=[245, 171, 53, 210],
                width_min_pixels=2,
                pickable=True,
            )
        )

    polygon_records = extract_polygon_records(wgs84)
    if polygon_records:
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                data=polygon_records,
                get_polygon="polygon",
                get_fill_color=[72, 150, 87, 90],
                get_line_color=[32, 96, 54, 220],
                line_width_min_pixels=2,
                pickable=True,
            )
        )

    return layers


def extract_point_records(gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    """Convert point geometries into pydeck records."""
    records: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geometry = row.geometry
        points = []
        if isinstance(geometry, Point):
            points = [geometry]
        elif isinstance(geometry, MultiPoint):
            points = list(geometry.geoms)
        for point in points:
            records.append(record_with_geometry(row, longitude=point.x, latitude=point.y))
    return records


def extract_line_records(gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    """Convert line geometries into pydeck records."""
    records: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geometry = row.geometry
        lines = []
        if isinstance(geometry, LineString):
            lines = [geometry]
        elif isinstance(geometry, MultiLineString):
            lines = list(geometry.geoms)
        for line in lines:
            records.append(record_with_geometry(row, path=list(map(list, line.coords))))
    return records


def extract_polygon_records(gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    """Convert polygon geometries into pydeck records."""
    records: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geometry = row.geometry
        polygons = []
        if isinstance(geometry, Polygon):
            polygons = [geometry]
        elif isinstance(geometry, MultiPolygon):
            polygons = list(geometry.geoms)
        for polygon in polygons:
            records.append(record_with_geometry(row, polygon=list(map(list, polygon.exterior.coords))))
    return records


def record_with_geometry(row: pd.Series, **geometry_values: Any) -> dict[str, Any]:
    """Build a JSON-friendly pydeck record from a GeoDataFrame row."""
    properties = row.drop(labels=["geometry"]).to_dict()
    properties.update(geometry_values)
    return properties


def build_view_state(gdf: gpd.GeoDataFrame) -> pdk.ViewState:
    """Build a map view centered on the selected vector dataset."""
    minx, miny, maxx, maxy = gdf.total_bounds
    longitude = float((minx + maxx) / 2)
    latitude = float((miny + maxy) / 2)
    span = max(float(maxx - minx), float(maxy - miny), 0.001)
    zoom = max(4, min(13, 10 - span * 20))
    return pdk.ViewState(latitude=latitude, longitude=longitude, zoom=zoom, pitch=0)


def render_key_value_table(values: dict[str, Any]) -> None:
    """Render metadata in a compact table."""
    rows = [{"Field": key, "Value": str(value)} for key, value in values.items()]
    st.dataframe(rows, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
