#!/usr/bin/env python3
"""Reduce a DEM GeoTIFF into a small grid of averaged, remapped heights."""

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import rasterio


def read_dem(path: Path) -> tuple[np.ma.MaskedArray, float, float, bool, "rasterio.Affine"]:
    """Read band 1 of the GeoTIFF as a masked array (nodata pixels masked),
    plus its per-pixel resolution (x, y) in CRS units, whether that CRS is
    geographic (degrees) rather than projected (e.g. meters), and its
    geotransform (needed to align a preview against a different image)."""
    with rasterio.open(path) as dataset:
        band = dataset.read(1, masked=True)
        xres, yres = dataset.res
        is_geographic = dataset.crs is not None and dataset.crs.is_geographic
        transform = dataset.transform

    # Some float DEMs encode missing data as NaN/Inf instead of a nodata tag.
    band = np.ma.masked_invalid(band)
    return band, xres, yres, is_geographic, transform


def read_display_image(path: Path) -> tuple[np.ndarray, "rasterio.Affine"]:
    """Read a raster (DEM or ordinary photo/orthophoto) for use as a preview
    background: a 2D grayscale array or an (H, W, bands) array for RGB/RGBA,
    plus its geotransform so it can be geographically aligned with the DEM."""
    with rasterio.open(path) as dataset:
        transform = dataset.transform
        band_count = min(dataset.count, 4)
        if band_count == 1:
            arr = dataset.read(1, masked=True)
            arr = np.ma.masked_invalid(arr).astype(float).filled(np.nan)
        else:
            arr = dataset.read(list(range(1, band_count + 1)))
            arr = np.transpose(arr, (1, 2, 0))
    return arr, transform


def crop_to_fixed_aspect_cells(
    band: np.ma.MaskedArray, xres: float, yres: float, rows: int, columns: int, cell_aspect: float = 1.0
) -> tuple[np.ma.MaskedArray, float, float, int, int]:
    """Center-crop the DEM so its extent divides exactly into (rows x columns)
    real-world cells of a fixed aspect ratio (cell_width / cell_height).

    The physical display grid (rows x columns) is fixed, but the source DEM's
    pixel resolution and aspect ratio can vary. To keep every output cell the
    correct real-world shape (not a rectangle stretched to fit), we pick the
    largest cell height that fits within the DEM's real-world extent given the
    target cell_aspect, then crop off any excess margin evenly from both sides
    of the longer axis. cell_aspect=1.0 (the default) means square cells; any
    other value produces rectangular cells with that width:height ratio, e.g.
    for a physical grid whose row and column pitches differ (like PVC pipes
    spaced 1.75in apart on one axis and 2.5in on the other: cell_aspect =
    2.5 / 1.75).

    Returns the cropped array, the cell width and height (in CRS units), and
    the (row, column) pixel offset of the crop within the original band
    (needed to draw the crop boundary on a preview of the full, uncropped
    DEM).
    """
    height, width = band.shape
    real_width = width * xres
    real_height = height * yres

    # Largest cell height that fits both axes (at the given aspect) without exceeding the DEM extent.
    cell_height = min(real_height / rows, real_width / (cell_aspect * columns))
    cell_width = cell_height * cell_aspect

    crop_width_px = min(width, int(round(cell_width * columns / xres)))
    crop_height_px = min(height, int(round(cell_height * rows / yres)))

    col_start = (width - crop_width_px) // 2
    row_start = (height - crop_height_px) // 2

    cropped = band[row_start : row_start + crop_height_px, col_start : col_start + crop_width_px]
    return cropped, cell_width, cell_height, row_start, col_start


def block_edges(source_size: int, num_blocks: int) -> np.ndarray:
    """Compute integer pixel boundaries splitting source_size into num_blocks
    roughly equal, proportional chunks. Works for any source/target ratio,
    including cases where the source does not divide evenly."""
    return np.round(np.linspace(0, source_size, num_blocks + 1)).astype(int)


def average_to_grid(
    band: np.ma.MaskedArray, rows: int, columns: int
) -> np.ndarray:
    """Reduce a 2D masked array into a (rows x columns) grid of block means.
    Blocks with no valid (unmasked) pixels become NaN."""
    height, width = band.shape
    row_edges = block_edges(height, rows)
    col_edges = block_edges(width, columns)

    grid = np.full((rows, columns), np.nan, dtype=float)
    for i in range(rows):
        r0, r1 = row_edges[i], row_edges[i + 1]
        if r1 <= r0:
            continue
        for j in range(columns):
            c0, c1 = col_edges[j], col_edges[j + 1]
            if c1 <= c0:
                continue
            block = band[r0:r1, c0:c1]
            if block.count() == 0:
                continue
            grid[i, j] = np.ma.mean(block)
    return grid


def remap_range(
    values: np.ndarray,
    source_min: float,
    source_max: float,
    target_min: float,
    target_max: float,
) -> np.ndarray:
    """Linearly remap values from [source_min, source_max] to
    [target_min, target_max], preserving NaN and clamping any out-of-range
    inputs (e.g. from a user-supplied source range) to the target bounds."""
    scale = (target_max - target_min) / (source_max - source_min)
    remapped = target_min + (values - source_min) * scale
    with np.errstate(invalid="ignore"):
        remapped = np.clip(remapped, target_min, target_max)
    remapped[np.isnan(values)] = np.nan
    return remapped


def round_to_step(values: np.ndarray, step: float) -> np.ndarray:
    """Round remapped values to the nearest multiple of step (e.g. step=5
    turns 191.6 into 190 and 193.2 into 195), preserving NaN."""
    return np.round(values / step) * step


def determine_source_range(
    band: np.ma.MaskedArray, source_min: float | None, source_max: float | None
) -> tuple[float, float]:
    """Use user-supplied source min/max if given, otherwise derive them from
    the valid (unmasked) DEM pixels."""
    if source_min is not None and source_max is not None:
        return source_min, source_max

    if band.count() == 0:
        raise ValueError("DEM contains no valid (non-nodata) pixels to derive a source range from")

    computed_min = float(np.ma.min(band))
    computed_max = float(np.ma.max(band))
    return (
        source_min if source_min is not None else computed_min,
        source_max if source_max is not None else computed_max,
    )


def format_grid(grid: np.ndarray) -> str:
    """Render the grid as comma-separated rows with 2 decimal places."""
    lines = []
    for row in grid:
        cells = ["nan" if np.isnan(v) else f"{v:.2f}" for v in row]
        lines.append(", ".join(cells))
    return "\n".join(lines)


def save_csv(grid: np.ndarray, path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        for row in grid:
            writer.writerow(["" if np.isnan(v) else f"{v:.4f}" for v in row])


def save_json(grid: np.ndarray, path: Path) -> None:
    data = [[None if np.isnan(v) else round(float(v), 4) for v in row] for row in grid]
    with path.open("w") as f:
        json.dump(data, f, indent=2)


def export_cropped_texture(
    background: np.ndarray,
    background_transform: "rasterio.Affine",
    dem_transform: "rasterio.Affine",
    crop_row_start: int,
    crop_col_start: int,
    crop_height: int,
    crop_width: int,
    path: Path,
) -> None:
    """Crop a background image (an orthophoto from --preview-image, or the
    DEM itself) down to exactly the crop footprint used for the grid
    (as computed by crop_to_fixed_aspect_cells), and save it as a plain image file.

    This lets the same region processed into the height grid also be
    exported as a texture, so a renderer can map the two onto the physical
    model in alignment. Like save_preview, coordinates are converted from the
    DEM's pixel space to the background image's pixel space via each
    raster's geotransform, so this works even if the background has a
    different resolution or extent than the DEM (same CRS required)."""

    def dem_px_to_bg_px(col: float, row: float) -> tuple[float, float]:
        x, y = dem_transform * (col, row)
        return (~background_transform) * (x, y)

    corners_dem = [
        (crop_col_start, crop_row_start),
        (crop_col_start + crop_width, crop_row_start),
        (crop_col_start + crop_width, crop_row_start + crop_height),
        (crop_col_start, crop_row_start + crop_height),
    ]
    corners_bg = [dem_px_to_bg_px(col, row) for col, row in corners_dem]
    xs = [c[0] for c in corners_bg]
    ys = [c[1] for c in corners_bg]

    bg_height, bg_width = background.shape[0], background.shape[1]
    col0 = max(0, int(round(min(xs))))
    col1 = min(bg_width, int(round(max(xs))))
    row0 = max(0, int(round(min(ys))))
    row1 = min(bg_height, int(round(max(ys))))

    if col1 <= col0 or row1 <= row0:
        raise ValueError(
            "Computed texture crop is empty — the DEM and the texture image may not overlap in world coordinates"
        )

    cropped = background[row0:row1, col0:col1]

    import matplotlib

    matplotlib.use("Agg")  # no display available under WSL
    import matplotlib.pyplot as plt

    if cropped.ndim == 2:
        plt.imsave(path, cropped, cmap="gray")
    else:
        plt.imsave(path, cropped)


def save_preview(
    background: np.ndarray,
    background_transform: "rasterio.Affine",
    dem_transform: "rasterio.Affine",
    crop_row_start: int,
    crop_col_start: int,
    crop_height: int,
    crop_width: int,
    grid: np.ndarray,
    target_min: float,
    target_max: float,
    units: str,
    path: Path,
) -> None:
    """Render a background image (the DEM itself, or a different reference
    image such as an orthophoto) with the grid overlaid: each
    cell is tinted by a colormap based on its remapped height, labeled with
    its value, and a color-scale key is added, so the grid can be checked
    visually before running it on the physical platform.

    The crop/grid coordinates are computed in the DEM's own pixel space, then
    converted to world coordinates via the DEM's geotransform and back into
    the background image's pixel space via its geotransform. This lets the
    overlay line up correctly even when the background has a different
    resolution, extent, or pixel dimensions than the DEM (as long as both
    share the same CRS)."""
    import matplotlib

    matplotlib.use("Agg")  # no display available under WSL
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    from matplotlib.patches import Polygon

    rows, columns = grid.shape

    def dem_px_to_bg_px(col: float, row: float) -> tuple[float, float]:
        x, y = dem_transform * (col, row)
        return (~background_transform) * (x, y)

    fig, ax = plt.subplots(figsize=(max(8, columns), max(8, rows)))
    if background.ndim == 2:
        bg_cmap = plt.get_cmap("gray").copy()
        bg_cmap.set_bad("black")  # nodata / masked pixels
        ax.imshow(background, cmap=bg_cmap)
    else:
        ax.imshow(background)

    crop_corners_dem = [
        (crop_col_start, crop_row_start),
        (crop_col_start + crop_width, crop_row_start),
        (crop_col_start + crop_width, crop_row_start + crop_height),
        (crop_col_start, crop_row_start + crop_height),
    ]
    crop_corners_bg = [dem_px_to_bg_px(col, row) for col, row in crop_corners_dem]
    ax.add_patch(Polygon(crop_corners_bg, closed=True, linewidth=2, edgecolor="red", facecolor="none"))

    # Color each cell by its remapped height, and label it with the value.
    height_cmap = plt.get_cmap("viridis")
    norm = Normalize(vmin=target_min, vmax=target_max)
    unit_suffix = f"{units}" if units else ""
    row_edges = block_edges(crop_height, rows) + crop_row_start
    col_edges = block_edges(crop_width, columns) + crop_col_start
    for i in range(rows):
        r0, r1 = row_edges[i], row_edges[i + 1]
        for j in range(columns):
            c0, c1 = col_edges[j], col_edges[j + 1]
            cell_corners_bg = [
                dem_px_to_bg_px(c0, r0),
                dem_px_to_bg_px(c1, r0),
                dem_px_to_bg_px(c1, r1),
                dem_px_to_bg_px(c0, r1),
            ]
            value = grid[i, j]
            center_x = sum(p[0] for p in cell_corners_bg) / 4
            center_y = sum(p[1] for p in cell_corners_bg) / 4

            if np.isnan(value):
                ax.add_patch(
                    Polygon(cell_corners_bg, closed=True, facecolor="none", edgecolor="red", hatch="//", linewidth=0.6)
                )
                continue

            ax.add_patch(
                Polygon(
                    cell_corners_bg,
                    closed=True,
                    facecolor=height_cmap(norm(value)),
                    edgecolor="yellow",
                    linewidth=0.6,
                    alpha=0.6,
                )
            )
            ax.text(
                center_x,
                center_y,
                f"{value:.0f}{unit_suffix}",
                ha="center",
                va="center",
                fontsize=8,
                color="white",
                path_effects=[path_effects.withStroke(linewidth=2, foreground="black")],
            )

    sm = ScalarMappable(cmap=height_cmap, norm=norm)
    sm.set_array([])
    label = f"Height ({units})" if units else "Height"
    fig.colorbar(sm, ax=ax, label=label, fraction=0.046, pad=0.04)

    ax.set_title(f"{rows}x{columns} grid (red = crop boundary)")
    ax.set_xlabel("column (px)")
    ax.set_ylabel("row (px)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def validate_args(args: argparse.Namespace) -> None:
    if args.rows <= 0 or args.columns <= 0:
        raise ValueError("--rows and --columns must be positive integers")
    if args.target_min >= args.target_max:
        raise ValueError("--target-min must be less than --target-max")
    if (args.source_min is None) != (args.source_max is None):
        raise ValueError("--source-min and --source-max must be provided together")
    if args.source_min is not None and args.source_min >= args.source_max:
        raise ValueError("--source-min must be less than --source-max")
    if (args.row_spacing is None) != (args.col_spacing is None):
        raise ValueError("--row-spacing and --col-spacing must be provided together")
    if args.row_spacing is not None and (args.row_spacing <= 0 or args.col_spacing <= 0):
        raise ValueError("--row-spacing and --col-spacing must be positive numbers")
    if args.output is not None and args.output.suffix.lower() not in (".csv", ".json"):
        raise ValueError("--output must end in .csv or .json")
    if args.round_to is not None and args.round_to <= 0:
        raise ValueError("--round-to must be a positive number")
    if args.preview is not None and args.preview.suffix.lower() not in (".png", ".jpg", ".jpeg", ".pdf", ".svg"):
        raise ValueError("--preview must end in .png, .jpg, .jpeg, .pdf, or .svg")
    if args.export_texture is not None and args.export_texture.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("--export-texture must end in .png, .jpg, or .jpeg")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reduce a DEM GeoTIFF into an averaged, remapped grid of heights."
    )
    parser.add_argument("input", type=Path, help="Path to the input DEM .tif file")
    parser.add_argument("--rows", type=int, required=True, help="Number of output grid rows")
    parser.add_argument("--columns", type=int, required=True, help="Number of output grid columns")
    parser.add_argument("--target-min", type=float, required=True, help="Minimum output height")
    parser.add_argument("--target-max", type=float, required=True, help="Maximum output height")
    parser.add_argument("--source-min", type=float, default=None, help="Optional fixed minimum source elevation")
    parser.add_argument("--source-max", type=float, default=None, help="Optional fixed maximum source elevation")
    parser.add_argument(
        "--row-spacing",
        type=float,
        default=None,
        help="Physical spacing between adjacent cells along the rows axis (any unit, e.g. "
        "inches). Used with --col-spacing to make cells rectangular instead of square, "
        "matching a real physical grid (e.g. unevenly-spaced pegs). Only the ratio between "
        "the two matters. Must be given together with --col-spacing; omit both for square cells.",
    )
    parser.add_argument(
        "--col-spacing",
        type=float,
        default=None,
        help="Physical spacing between adjacent cells along the columns axis. See --row-spacing.",
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional output file path (.csv or .json)")
    parser.add_argument(
        "--round-to",
        type=float,
        default=None,
        help="Round remapped height values to the nearest multiple of this number "
        "(e.g. --round-to 5 turns 191.6 into 190 and 193.2 into 195)",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="Optional image path (.png/.jpg/.pdf/.svg) to save a preview with the "
        "crop boundary and grid overlaid",
    )
    parser.add_argument(
        "--preview-image",
        type=Path,
        default=None,
        help="Optional path to a different georeferenced image (e.g. an orthophoto) "
        "to draw the --preview overlay on top of, instead of the DEM itself. "
        "Must share the same CRS as the input DEM.",
    )
    parser.add_argument(
        "--export-texture",
        type=Path,
        default=None,
        help="Optional image path (.png/.jpg/.jpeg) to save a cropped copy of --preview-image "
        "(or the DEM itself, if not given) matching exactly the crop footprint used "
        "for the grid, for use as an aligned texture on the physical model",
    )
    parser.add_argument(
        "--units",
        type=str,
        default="mm",
        help="Unit label appended to each cell's value and the color key in --preview "
        "(default: mm; pass '' for no unit label)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        validate_args(args)

        if not args.input.exists():
            raise FileNotFoundError(f"Input DEM not found: {args.input}")

        band, xres, yres, is_geographic, dem_transform = read_dem(args.input)

        cell_aspect = 1.0
        if args.row_spacing is not None:
            cell_aspect = args.col_spacing / args.row_spacing

        if is_geographic:
            print(
                "Warning: DEM CRS is geographic (degrees) — cells will be sized in degrees, "
                "not true real-world meters, since a degree of longitude and a degree of "
                "latitude cover different ground distances. Reproject to a projected "
                "(metric) CRS for physically accurate cell dimensions.",
                file=sys.stderr,
            )

        original_band = band
        original_shape = band.shape
        band, cell_width, cell_height, crop_row_start, crop_col_start = crop_to_fixed_aspect_cells(
            band, xres, yres, args.rows, args.columns, cell_aspect
        )
        unit = "deg" if is_geographic else "unit"
        if band.shape != original_shape:
            shape_desc = (
                f"square of side ~{cell_height:.6f} {unit}"
                if cell_aspect == 1.0
                else f"rectangle of ~{cell_height:.6f} (rows) x ~{cell_width:.6f} (columns) {unit}"
            )
            print(
                f"Cropped DEM from {original_shape[1]}x{original_shape[0]} to "
                f"{band.shape[1]}x{band.shape[0]} pixels (centered) so each of the "
                f"{args.rows}x{args.columns} cells is a real-world {shape_desc}"
            )

        source_min, source_max = determine_source_range(band, args.source_min, args.source_max)

        averaged = average_to_grid(band, args.rows, args.columns)
        remapped = remap_range(averaged, source_min, source_max, args.target_min, args.target_max)
        if args.round_to is not None:
            remapped = round_to_step(remapped, args.round_to)

        print(f"Processed grid: {args.rows} rows x {args.columns} columns")
        print(f"Source elevation range: {source_min:.2f} to {source_max:.2f}")
        print()
        print(format_grid(remapped))

        if args.output is not None:
            if args.output.suffix.lower() == ".csv":
                save_csv(remapped, args.output)
            else:
                save_json(remapped, args.output)
            print(f"\nSaved grid to {args.output}")

        if args.preview is not None or args.export_texture is not None:
            if args.preview_image is not None:
                if not args.preview_image.exists():
                    raise FileNotFoundError(f"Preview image not found: {args.preview_image}")
                background, background_transform = read_display_image(args.preview_image)
            else:
                background = original_band.astype(float).filled(np.nan)
                background_transform = dem_transform

            if args.preview is not None:
                save_preview(
                    background,
                    background_transform,
                    dem_transform,
                    crop_row_start,
                    crop_col_start,
                    band.shape[0],
                    band.shape[1],
                    remapped,
                    args.target_min,
                    args.target_max,
                    args.units,
                    args.preview,
                )
                print(f"Saved preview to {args.preview}")

            if args.export_texture is not None:
                export_cropped_texture(
                    background,
                    background_transform,
                    dem_transform,
                    crop_row_start,
                    crop_col_start,
                    band.shape[0],
                    band.shape[1],
                    args.export_texture,
                )
                print(f"Saved cropped texture to {args.export_texture}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
