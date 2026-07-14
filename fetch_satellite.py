#!/usr/bin/env python3
"""Fetch a satellite basemap image aligned exactly to a DEM's bounds and CRS."""

import argparse
import sys
from pathlib import Path

import contextily as cx
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject


def resolve_provider(name: str):
    """Look up a contextily tile provider by dotted name, e.g. 'Esri.WorldImagery'."""
    provider = cx.providers
    for part in name.split("."):
        try:
            provider = getattr(provider, part)
        except AttributeError:
            raise ValueError(f"Unknown tile provider: {name!r}")
    return provider


def fetch_satellite_image(
    dem_path: Path, output_path: Path, provider_name: str, zoom: int | None, scale: float
) -> None:
    """Download satellite tiles covering the DEM's bounding box, then reproject
    them onto a raster with the DEM's own bounds and CRS, so the result can be
    used directly as --preview-image / --export-texture input with guaranteed
    pixel-perfect alignment (no manual bounding-box matching required)."""
    with rasterio.open(dem_path) as dem:
        dem_bounds = dem.bounds
        dem_crs = dem.crs
        dst_width = max(1, int(round(dem.width * scale)))
        dst_height = max(1, int(round(dem.height * scale)))

    provider = resolve_provider(provider_name)

    # bounds2img expects (west, south, east, north); ll=True means they're lon/lat
    # (EPSG:4326) rather than already in Web Mercator. Returns an (H, W, 4) RGBA
    # array plus its extent, natively in EPSG:3857 (how XYZ tile servers work).
    tile_image, tile_extent = cx.bounds2img(
        dem_bounds.left,
        dem_bounds.bottom,
        dem_bounds.right,
        dem_bounds.top,
        zoom=zoom if zoom is not None else "auto",
        source=provider,
        ll=True,
    )
    west_m, east_m, south_m, north_m = tile_extent
    tile_height, tile_width = tile_image.shape[0], tile_image.shape[1]
    src_transform = from_bounds(west_m, south_m, east_m, north_m, tile_width, tile_height)

    # Reproject from the fetched Web Mercator tile mosaic onto a raster whose
    # transform/CRS exactly match the DEM's own bounds, so it lines up with
    # process_dem.py's coordinate-alignment logic without further conversion.
    dst_transform = from_bounds(
        dem_bounds.left, dem_bounds.bottom, dem_bounds.right, dem_bounds.top, dst_width, dst_height
    )
    band_count = tile_image.shape[2]
    destination = np.zeros((band_count, dst_height, dst_width), dtype=np.uint8)
    for band in range(band_count):
        reproject(
            source=tile_image[:, :, band],
            destination=destination[band],
            src_transform=src_transform,
            src_crs="EPSG:3857",
            dst_transform=dst_transform,
            dst_crs=dem_crs,
            resampling=Resampling.bilinear,
        )

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=dst_height,
        width=dst_width,
        count=band_count,
        dtype=np.uint8,
        crs=dem_crs,
        transform=dst_transform,
    ) as dst:
        dst.write(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a satellite basemap GeoTIFF aligned to a DEM's exact bounds and CRS."
    )
    parser.add_argument("input", type=Path, help="Path to the DEM .tif whose bounds/CRS to match")
    parser.add_argument("output", type=Path, help="Output GeoTIFF path for the aligned satellite image")
    parser.add_argument(
        "--provider",
        type=str,
        default="Esri.WorldImagery",
        help="Dotted contextily provider name (default: Esri.WorldImagery, free/no API key)",
    )
    parser.add_argument(
        "--zoom", type=int, default=None, help="Tile zoom level (default: auto-selected by contextily)"
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=3.0,
        help="Output resolution as a multiple of the DEM's own pixel dimensions (default: 3.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if not args.input.exists():
            raise FileNotFoundError(f"Input DEM not found: {args.input}")
        if args.scale <= 0:
            raise ValueError("--scale must be a positive number")

        fetch_satellite_image(args.input, args.output, args.provider, args.zoom, args.scale)
        print(f"Saved satellite image to {args.output}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
