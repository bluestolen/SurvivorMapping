"""Terrain slope, aspect, rugosity, and difficulty metrics for DEM rasters."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import rasterio
from rasterio.transform import Affine
from scipy import ndimage


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESSED = PROJECT_ROOT / "data" / "processed"

FLOAT_NODATA = -9999.0
DIFFICULTY_NODATA = 0

DIFFICULTY_CLASSES = {
    0: "nodata",
    1: "flat",
    2: "moderate",
    3: "steep",
    4: "extreme",
}


@dataclass(frozen=True)
class TerrainMetrics:
    """Aligned terrain metric arrays derived from one DEM."""

    slope_degrees: np.ndarray
    aspect_degrees: np.ndarray
    rugosity: np.ndarray
    difficulty: np.ndarray
    valid_mask: np.ndarray


def main() -> int:
    """Parse command-line arguments and write terrain metric rasters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dem",
        type=Path,
        default=None,
        help="Standardized DEM GeoTIFF in data/processed/. Defaults to the first matching DEM.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROCESSED,
        help="Directory for terrain metric GeoTIFF outputs.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Output filename prefix. Defaults to the DEM stem.",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=3,
        help="Odd neighborhood size for local elevation variance.",
    )
    args = parser.parse_args()

    try:
        dem_path = args.dem or find_default_dem(PROCESSED)
        prefix = args.prefix or dem_path.stem
        outputs = process_dem(dem_path, args.output_dir, prefix, window_size=args.window_size)
    except (OSError, ValueError, rasterio.errors.RasterioError) as error:
        print(f"Terrain analysis failed: {error}", file=sys.stderr)
        return 1

    for path in outputs:
        print(path)
    return 0


def find_default_dem(directory: Path) -> Path:
    """Return the first likely processed DEM in a directory."""
    candidates = sorted(
        path
        for path in directory.glob("*.tif*")
        if any(token in path.stem.lower() for token in ("dem", "elevation"))
    )
    if not candidates:
        raise ValueError(f"No DEM GeoTIFF found in {directory}")
    return candidates[0]


def process_dem(
    dem_path: Path,
    output_dir: Path = PROCESSED,
    prefix: str | None = None,
    *,
    window_size: int = 3,
) -> list[Path]:
    """Read a DEM, compute terrain metrics, and write aligned GeoTIFFs."""
    dem_path = dem_path.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or dem_path.stem

    with rasterio.open(dem_path) as dataset:
        validate_dem(dataset)
        elevation = dataset.read(1, masked=True).astype("float32")
        metrics = calculate_terrain_metrics(elevation, dataset.transform, window_size=window_size)
        profile = dataset.profile.copy()

    outputs = {
        f"{prefix}_slope_degrees.tif": (
            metrics.slope_degrees,
            "float32",
            FLOAT_NODATA,
            "Slope in degrees",
        ),
        f"{prefix}_aspect_degrees.tif": (
            metrics.aspect_degrees,
            "float32",
            FLOAT_NODATA,
            "Downslope aspect in compass degrees",
        ),
        f"{prefix}_rugosity_variance.tif": (
            metrics.rugosity,
            "float32",
            FLOAT_NODATA,
            "Local elevation variance in square map units",
        ),
        f"{prefix}_terrain_difficulty.tif": (
            metrics.difficulty,
            "uint8",
            DIFFICULTY_NODATA,
            "Terrain difficulty class",
        ),
    }

    paths: list[Path] = []
    for filename, (array, dtype, nodata, description) in outputs.items():
        path = output_dir / filename
        write_metric_raster(
            path,
            array,
            profile,
            dtype=dtype,
            nodata=nodata,
            description=description,
        )
        paths.append(path)
    return paths


def validate_dem(dataset: rasterio.io.DatasetReader) -> None:
    """Validate that the input raster is suitable for terrain analysis."""
    if dataset.count != 1:
        raise ValueError("DEM must contain exactly one band")
    if dataset.crs is None:
        raise ValueError("DEM must have a coordinate reference system")
    if dataset.transform == Affine.identity():
        raise ValueError("DEM must have a georeferencing transform")
    if dataset.width < 3 or dataset.height < 3:
        raise ValueError("DEM must be at least 3x3 pixels")


def calculate_terrain_metrics(
    elevation: np.ma.MaskedArray,
    transform: Affine,
    *,
    window_size: int = 3,
) -> TerrainMetrics:
    """Calculate slope, aspect, rugosity, and difficulty arrays from elevation."""
    if window_size < 3 or window_size % 2 == 0:
        raise ValueError("--window-size must be an odd integer of at least 3")

    dem = np.asarray(elevation.filled(np.nan), dtype="float32")
    valid_mask = np.isfinite(dem)
    if not valid_mask.any():
        raise ValueError("DEM contains no valid elevation cells")

    filled_dem = fill_nodata_nearest(dem, valid_mask)
    x_resolution, y_resolution = pixel_resolution(transform)

    dzdx = convolve_valid(
        filled_dem,
        np.array(
            [
                [-1, 0, 1],
                [-2, 0, 2],
                [-1, 0, 1],
            ],
            dtype="float32",
        )
        / (8 * x_resolution),
    )
    dzdy = convolve_valid(
        filled_dem,
        np.array(
            [
                [1, 2, 1],
                [0, 0, 0],
                [-1, -2, -1],
            ],
            dtype="float32",
        )
        / (8 * y_resolution),
    )

    slope_radians = np.arctan(np.hypot(dzdx, dzdy))
    slope_degrees = np.degrees(slope_radians).astype("float32")
    aspect_degrees = ((np.degrees(np.arctan2(-dzdx, -dzdy)) + 360) % 360).astype("float32")
    aspect_degrees[slope_degrees < 0.001] = np.nan

    rugosity = local_elevation_variance(dem, valid_mask, window_size=window_size)
    difficulty = classify_terrain_difficulty(slope_degrees, rugosity, valid_mask)

    slope_degrees = mask_float_metric(slope_degrees, valid_mask)
    aspect_degrees = mask_float_metric(aspect_degrees, valid_mask)
    rugosity = mask_float_metric(rugosity, valid_mask)

    return TerrainMetrics(
        slope_degrees=slope_degrees,
        aspect_degrees=aspect_degrees,
        rugosity=rugosity,
        difficulty=difficulty,
        valid_mask=valid_mask,
    )


def pixel_resolution(transform: Affine) -> tuple[float, float]:
    """Return positive x/y pixel spacing from an affine transform."""
    x_resolution = float(abs(transform.a))
    y_resolution = float(abs(transform.e))
    if x_resolution == 0 or y_resolution == 0:
        raise ValueError("DEM pixel resolution must be non-zero")
    return x_resolution, y_resolution


def fill_nodata_nearest(dem: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    """Fill NoData cells with nearest valid elevations for convolution stability."""
    if valid_mask.all():
        return dem
    _, nearest_indices = ndimage.distance_transform_edt(~valid_mask, return_indices=True)
    return dem[tuple(nearest_indices)]


def convolve_valid(array: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Apply a convolution kernel with nearest-edge handling."""
    return ndimage.convolve(array, kernel, mode="nearest").astype("float32")


def local_elevation_variance(
    dem: np.ndarray,
    valid_mask: np.ndarray,
    *,
    window_size: int = 3,
) -> np.ndarray:
    """Compute local elevation variance with convolution-based neighborhood sums."""
    kernel = np.ones((window_size, window_size), dtype="float32")
    valid = valid_mask.astype("float32")
    filled = np.where(valid_mask, dem, 0).astype("float32")

    count = ndimage.convolve(valid, kernel, mode="constant", cval=0)
    total = ndimage.convolve(filled, kernel, mode="constant", cval=0)
    total_squared = ndimage.convolve(filled**2, kernel, mode="constant", cval=0)

    variance = np.full(dem.shape, np.nan, dtype="float32")
    usable = count > 0
    mean = np.zeros(dem.shape, dtype="float32")
    mean[usable] = total[usable] / count[usable]
    variance[usable] = (total_squared[usable] / count[usable]) - mean[usable] ** 2
    variance[variance < 0] = 0
    return variance


def classify_terrain_difficulty(
    slope_degrees: np.ndarray,
    rugosity: np.ndarray,
    valid_mask: np.ndarray,
) -> np.ndarray:
    """Classify terrain into discrete difficulty classes."""
    difficulty = np.full(slope_degrees.shape, DIFFICULTY_NODATA, dtype="uint8")
    valid = valid_mask & np.isfinite(slope_degrees) & np.isfinite(rugosity)

    difficulty[valid & (slope_degrees < 5) & (rugosity < 25)] = 1
    difficulty[valid & (difficulty == 0) & (slope_degrees < 15) & (rugosity < 200)] = 2
    difficulty[valid & (difficulty == 0) & (slope_degrees < 30) & (rugosity < 900)] = 3
    difficulty[valid & (difficulty == 0)] = 4
    return difficulty


def mask_float_metric(array: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    """Apply the DEM mask and replace NaN values with the float NoData value."""
    output = array.astype("float32", copy=True)
    output[~valid_mask | ~np.isfinite(output)] = FLOAT_NODATA
    return output


def write_metric_raster(
    path: Path,
    array: np.ndarray,
    profile: dict,
    *,
    dtype: str,
    nodata: float | int,
    description: str,
) -> None:
    """Write one metric array to a single-band GeoTIFF."""
    output_profile = profile.copy()
    output_profile.update(
        driver="GTiff",
        count=1,
        dtype=dtype,
        nodata=nodata,
        compress="deflate",
    )
    with rasterio.open(path, "w", **output_profile) as dataset:
        dataset.write(array.astype(dtype), 1)
        dataset.set_band_description(1, description)
        if dtype == "uint8":
            dataset.update_tags(
                1,
                **{f"class_{value}": label for value, label in DIFFICULTY_CLASSES.items()},
            )


def iter_metric_paths(prefix: str, directory: Path = PROCESSED) -> Iterable[Path]:
    """Yield expected metric output paths for a DEM prefix."""
    suffixes = (
        "slope_degrees",
        "aspect_degrees",
        "rugosity_variance",
        "terrain_difficulty",
    )
    for suffix in suffixes:
        yield directory / f"{prefix}_{suffix}.tif"


if __name__ == "__main__":
    sys.exit(main())
