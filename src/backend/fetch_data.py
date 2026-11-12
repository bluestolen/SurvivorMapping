"""Fetch elevation, OpenStreetMap features, and land cover for a WGS84 bounding box."""

import argparse
from http.client import HTTPException
import json
import math
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import rasterio


RAW = Path(__file__).resolve().parents[2] / "data" / "raw"
DEM_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
OSM_URL = "https://overpass-api.de/api/interpreter"
LAND_COVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"


def download(url, path, *, data=None):
    """Download an HTTP response to a staging file.

    Args:
        url: Public endpoint or file URL.
        path: Destination path. Each attempt overwrites the staged file.
        data: Optional encoded POST body. If omitted, use GET.

    Raises:
        OSError: A network or file operation failed, or the response was
            empty or did not match its declared length.
        HTTPException: The HTTP response could not be read.

    Transient failures are retried up to twice before being raised.
    """
    request = Request(url, data=data, headers={"User-Agent": "SurvivorMapping/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=120) as response, path.open("wb") as output:
                shutil.copyfileobj(response, output)
                expected = response.headers.get("Content-Length")
                if output.tell() == 0 or (expected and output.tell() != int(expected)):
                    raise OSError("Incomplete HTTP response")
            return
        except HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdecimal() else 2**attempt
        except (OSError, HTTPException):
            if attempt == 2:
                raise
            delay = 2**attempt
        print(f"Retrying {path.name} in {delay} seconds", file=sys.stderr)
        time.sleep(delay)


def validate_raster(path, *, size=None):
    """Read all blocks of a staged GeoTIFF to check that it is usable.

    Args:
        path: Raster file to validate.
        size: Optional expected (width, height) in pixels.

    Raises:
        ValueError: The format, georeferencing, band count, or dimensions
            are unexpected, or every pixel is NoData.
        rasterio.errors.RasterioError: The raster cannot be opened or read.
    """
    with rasterio.open(path) as dataset:
        if dataset.driver != "GTiff" or dataset.crs is None or dataset.count != 1:
            raise ValueError(f"{path.name}: expected a georeferenced single-band GeoTIFF")
        if size is not None and (dataset.width, dataset.height) != size:
            raise ValueError(f"{path.name}: unexpected raster dimensions")
        has_data = False
        for _, window in dataset.block_windows(1):
            block = dataset.read(1, window=window, masked=True)
            has_data |= bool(block.count())
        if not has_data:
            raise ValueError(f"{path.name}: raster contains only NoData")


def fetch_elevation(bounds, directory):
    """Stage and validate a USGS 3DEP elevation GeoTIFF.

    Args:
        bounds: WGS84 extent in (west, south, east, north) order.
        directory: Staging directory for elevation_3dep.tif.

    Requests roughly 30-meter spacing, using coarser spacing for larger areas
    to keep each raster dimension within 2,048 pixels.
    """
    west, south, east, north = bounds
    width = (east - west) * 111_320 * math.cos(math.radians((south + north) / 2))
    height = (north - south) * 111_320
    resolution = max(30, width / 2048, height / 2048)
    size = tuple(max(1, math.ceil(length / resolution)) for length in (width, height))
    params = urlencode({
        "bbox": ",".join(map(str, bounds)),
        "bboxSR": 4326,
        "imageSR": 4326,
        "size": ",".join(map(str, size)),
        "format": "tiff",
        "pixelType": "F32",
        "returnSquarePixels": "false",
        "f": "image",
    })
    path = directory / "elevation_3dep.tif"
    download(f"{DEM_URL}?{params}", path)
    validate_raster(path, size=size)


def fetch_land_cover(bounds, directory):
    """Stage and validate the ESA WorldCover 2021 tiles intersecting an extent.

    Args:
        bounds: WGS84 extent in (west, south, east, north) order.
        directory: Staging directory for the original 3-degree GeoTIFF tiles.
    """
    west, south, east, north = bounds
    for lat in range(math.floor(south / 3) * 3, math.ceil(north / 3) * 3, 3):
        for lon in range(math.floor(west / 3) * 3, math.ceil(east / 3) * 3, 3):
            tile = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            filename = f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
            path = directory / filename
            download(f"{LAND_COVER_URL}/{filename}", path)
            validate_raster(path)


def fetch_osm(bounds, directory):
    """Query Overpass and stage a GeoJSON FeatureCollection.

    Args:
        bounds: WGS84 extent in (west, south, east, north) order.
        directory: Staging directory for osm_features.geojson.

    Converts selected OSM nodes and ways into points, lines, or polygons
    and retains their tags as feature properties.

    Raises:
        ValueError: The response is invalid or reports incomplete results.
        KeyError: A required OSM element field is missing.
    """
    west, south, east, north = bounds
    # Overpass expects latitude before longitude in its bounding box.
    bbox = f"{south},{west},{north},{east}"
    query = f"""
[out:json][timeout:90];
(
  way["highway"~"^(path|footway|track|bridleway|steps)$"]({bbox});
  way["waterway"]({bbox});
  way["natural"="water"]({bbox});
  nw["natural"="spring"]({bbox});
  nw["amenity"="drinking_water"]({bbox});
  nw["tourism"~"^(camp_site|wilderness_hut|alpine_hut)$"]({bbox});
);
out geom;
"""
    response = directory / "overpass.json"
    download(OSM_URL, response, data=urlencode({"data": query}).encode("ascii"))
    payload = json.loads(response.read_bytes())
    if payload.get("remark") or not isinstance(payload.get("elements"), list):
        raise ValueError(f"Overpass returned an incomplete response: {payload.get('remark')}")

    features = []
    for element in payload["elements"]:
        kind = element["type"]
        tags = element["tags"]
        if kind == "node":
            geometry = {"type": "Point", "coordinates": [element["lon"], element["lat"]]}
        elif kind == "way":
            coordinates = [[point["lon"], point["lat"]] for point in element["geometry"]]
            if len(coordinates) < 2:
                raise ValueError(f"OSM way {element['id']} has incomplete geometry")
            closed = len(coordinates) >= 4 and coordinates[0] == coordinates[-1]
            area = tags.get("area") == "yes" or (
                tags.get("area") != "no" and ("tourism" in tags or tags.get("natural") == "water")
            )
            geometry = (
                {"type": "Polygon", "coordinates": [coordinates]}
                if closed and area else {"type": "LineString", "coordinates": coordinates}
            )
        else:
            raise ValueError(f"Unexpected OSM element type: {kind}")
        features.append({
            "type": "Feature",
            "id": f"{kind}/{element['id']}",
            "properties": tags,
            "geometry": geometry,
        })

    output = directory / "osm_features.geojson"
    output.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    response.unlink()


def main():
    """Parse the bounding box, stage the downloads, and publish validated files.

    Returns:
        Zero on success or one if fetching, validation, or publishing fails.

    Raises:
        SystemExit: The command-line arguments are invalid or help is requested.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", nargs=4, type=float, required=True,
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    bounds = parser.parse_args().bbox
    west, south, east, north = bounds
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        parser.error("--bbox must be an ordered WGS84 extent without an antimeridian crossing")
    if east - west > 3 or north - south > 3:
        parser.error("--bbox must span at most 3 degrees per axis")

    try:
        RAW.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".fetch-", dir=RAW) as temporary:
            directory = Path(temporary)
            fetch_elevation(bounds, directory)
            fetch_land_cover(bounds, directory)
            fetch_osm(bounds, directory)
            # Publish files only after all three sources have been validated.
            for path in sorted(directory.iterdir()):
                path.replace(RAW / path.name)
                print(RAW / path.name)
    except (OSError, HTTPException, ValueError, KeyError, TypeError, rasterio.errors.RasterioError) as error:
        print(f"Data fetch failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
