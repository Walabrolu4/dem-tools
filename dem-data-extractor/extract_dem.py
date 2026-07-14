#!/usr/bin/env python3
"""Extract a DEM GeoTIFF into a raw float32 binary + JSON metadata (and
optional PNG preview / CSV) for fast loading in an external runtime (e.g. Unity)."""

import argparse
import csv
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import rasterio


def read_dem(path: Path):
    """Read band 1 of the GeoTIFF, returning the raw (unmasked) pixel array
    plus a masked view (nodata/NaN/Inf excluded) for computing statistics,
    along with the raster's geospatial metadata."""
    with rasterio.open(path) as dataset:
        # rasterio's plain (non-masked) read triggers a spurious
        # DeprecationWarning on newer NumPy (internal to rasterio, not
        # actionable here); it doesn't affect the returned data.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            raw = dataset.read(1)
        masked = dataset.read(1, masked=True)
        xres, yres = dataset.res
        bounds = dataset.bounds
        crs = dataset.crs
        nodata = dataset.nodata
        width, height = dataset.width, dataset.height

    # Some float DEMs encode missing data as NaN/Inf instead of a nodata tag;
    # exclude those from the statistics too, but leave them as-is in `raw`.
    masked = np.ma.masked_invalid(masked)
    return raw, masked, xres, yres, bounds, crs, nodata, width, height


def build_metadata(masked, xres, yres, bounds, crs, nodata, width, height) -> dict:
    if masked.count() == 0:
        raise ValueError("DEM contains no valid (non-nodata) pixels to compute elevation range from")

    min_elevation = float(np.ma.min(masked))
    max_elevation = float(np.ma.max(masked))

    return {
        "width": width,
        "height": height,
        "minElevation": min_elevation,
        "maxElevation": max_elevation,
        "noDataValue": float(nodata) if nodata is not None else None,
        "bounds": {
            "left": bounds.left,
            "right": bounds.right,
            "bottom": bounds.bottom,
            "top": bounds.top,
        },
        "crs": crs.to_string() if crs is not None else None,
        "epsg": crs.to_epsg() if crs is not None else None,
        "pixelSize": {"x": xres, "y": yres},
        "dataType": "float32",
        "byteOrder": "little",
        "pixelOrder": "row-major, row 0 = top (north) row, matching the source raster",
    }


def write_binary(raw: np.ndarray, path: Path) -> None:
    """Write every pixel as a little-endian 32-bit float, row-major (row 0 =
    top row), with no header — just width * height * 4 raw bytes."""
    raw.astype("<f4").tofile(path)


def write_metadata(metadata: dict, path: Path) -> None:
    with path.open("w") as f:
        json.dump(metadata, f, indent=2)


def write_csv(raw: np.ndarray, path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        for row in raw:
            writer.writerow([f"{v}" for v in row])


def write_preview(masked, min_elevation: float, max_elevation: float, path: Path, max_dim: int | None) -> None:
    """Save a normalized grayscale PNG for visual reference only; dem.bin
    holds the real measurements. NoData/invalid pixels are transparent."""
    import matplotlib

    matplotlib.use("Agg")  # no display available under WSL
    import matplotlib.pyplot as plt

    span = max_elevation - min_elevation
    normalized = np.zeros(masked.shape, dtype=np.float64) if span == 0 else (masked - min_elevation) / span
    cmap = plt.get_cmap("gray").copy()
    cmap.set_bad(alpha=0.0)  # transparent nodata

    if max_dim is not None and max(masked.shape) > max_dim:
        from PIL import Image

        scale = max_dim / max(masked.shape)
        new_size = (max(1, round(masked.shape[1] * scale)), max(1, round(masked.shape[0] * scale)))
        rgba = (cmap(np.ma.filled(normalized, np.nan)) * 255).astype(np.uint8)
        Image.fromarray(rgba, mode="RGBA").resize(new_size, Image.BILINEAR).save(path)
    else:
        plt.imsave(path, normalized, cmap=cmap, vmin=0.0, vmax=1.0)


def validate_args(args: argparse.Namespace) -> None:
    if args.preview_max_dim is not None and args.preview_max_dim <= 0:
        raise ValueError("--preview-max-dim must be a positive integer")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a DEM GeoTIFF into a raw float32 binary + JSON metadata for external runtimes."
    )
    parser.add_argument("input", type=Path, help="Path to the input DEM .tif file")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="Directory to write outputs into (created if missing)")
    parser.add_argument("--bin-name", type=str, default="dem.bin", help="Filename for the binary elevation output (default: dem.bin)")
    parser.add_argument("--metadata-name", type=str, default="dem_metadata.json", help="Filename for the JSON metadata output (default: dem_metadata.json)")
    parser.add_argument("--preview", action="store_true", help="Also save a grayscale PNG preview (dem-preview.png) for visual reference")
    parser.add_argument("--preview-name", type=str, default="dem-preview.png", help="Filename for the preview PNG (default: dem-preview.png)")
    parser.add_argument("--preview-max-dim", type=int, default=None, help="Downsample the preview PNG so its longer side is at most this many pixels")
    parser.add_argument("--csv", action="store_true", help="Also save the elevation grid as CSV (prototyping only; large for big DEMs)")
    parser.add_argument("--csv-name", type=str, default="dem.csv", help="Filename for the CSV output (default: dem.csv)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        validate_args(args)

        if not args.input.exists():
            raise FileNotFoundError(f"Input DEM not found: {args.input}")

        raw, masked, xres, yres, bounds, crs, nodata, width, height = read_dem(args.input)
        metadata = build_metadata(masked, xres, yres, bounds, crs, nodata, width, height)

        args.output_dir.mkdir(parents=True, exist_ok=True)

        bin_path = args.output_dir / args.bin_name
        write_binary(raw, bin_path)
        print(f"Saved elevation binary to {bin_path} ({raw.size * 4} bytes, {width}x{height} float32)")

        metadata_path = args.output_dir / args.metadata_name
        write_metadata(metadata, metadata_path)
        print(f"Saved metadata to {metadata_path}")
        print(json.dumps(metadata, indent=2))

        if args.csv:
            csv_path = args.output_dir / args.csv_name
            write_csv(raw, csv_path)
            print(f"Saved CSV to {csv_path}")

        if args.preview:
            preview_path = args.output_dir / args.preview_name
            write_preview(masked, metadata["minElevation"], metadata["maxElevation"], preview_path, args.preview_max_dim)
            print(f"Saved preview to {preview_path}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
