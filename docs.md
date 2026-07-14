# process_dem.py — Technical Documentation

This document explains what `process_dem.py` does internally, function by function, for anyone maintaining or extending it. For install/usage instructions, see [README.md](README.md).

## Purpose

Takes a DEM (Digital Elevation Model) GeoTIFF and reduces it to a small `rows x columns` grid of height values, scaled to a target range (e.g. millimeters for a physical relief model). The grid is meant to drive a device that raises/lowers a fixed-size array of cells (e.g. pins under a projector, or pegs on a physical rig), so:

- every output cell must represent a real-world patch of ground of the correct **shape** — square by default, or a fixed rectangular aspect ratio if the physical grid's cell pitch isn't equal on both axes — regardless of the DEM's aspect ratio or pixel resolution
- the number of rows/columns is fixed by the physical device, not by the DEM's shape

## Pipeline overview

`main()` runs these steps in order:

1. **Read** the DEM's first band and its geospatial metadata (`read_dem`)
2. **Crop** the DEM so its extent divides evenly into fixed-aspect-ratio cells (`crop_to_fixed_aspect_cells`)
3. **Average** each cell's source pixels (`average_to_grid`)
4. **Remap** averaged elevations to the target height range (`remap_range`)
5. **Round** to a step size, if requested (`round_to_step`)
6. **Print** the grid to the console, and optionally **save** it (`save_csv` / `save_json`), render a **preview** image (`save_preview`), and/or export a matching **cropped texture** (`export_cropped_texture`)

## Function reference

### `read_dem(path) -> (band, xres, yres, is_geographic, transform)`

Opens the GeoTIFF with `rasterio` and reads band 1 as a `numpy.ma.MaskedArray`, using `masked=True` so nodata pixels (per the raster's nodata tag) are masked. `np.ma.masked_invalid` additionally masks any `NaN`/`Inf` values, since some float DEMs use those instead of a nodata tag.

Also returns:
- `xres, yres`: pixel size in CRS units (`dataset.res`)
- `is_geographic`: `True` if the CRS is in degrees (e.g. EPSG:4326) rather than a projected, linear-unit CRS (e.g. UTM/meters)
- `transform`: the raster's affine geotransform (pixel → world coordinates), needed later to align preview overlays

### `read_display_image(path) -> (array, transform)`

Like `read_dem`, but for loading a *different* image as a preview background (e.g. an orthophoto). Returns a 2D grayscale array (single-band raster) or an `(H, W, bands)` array (multi-band, up to 4 bands — RGB or RGBA) suitable for `matplotlib.imshow`, plus its geotransform.

### `crop_to_fixed_aspect_cells(band, xres, yres, rows, columns, cell_aspect=1.0) -> (cropped_band, cell_width, cell_height, row_start, col_start)`

This is the core of the "correctly-shaped cells" requirement. Given a fixed `rows x columns` grid, the DEM's real-world extent (`width * xres`, `height * yres`) generally won't divide into that many cells of the target shape — the extent's aspect ratio rarely lines up exactly.

`cell_aspect` is the target cell's `width / height` ratio. The default, `1.0`, means square cells. Any other value produces rectangular cells with that ratio — e.g. for a physical grid whose pegs/pins are spaced 1.75in apart along the rows and 2.5in apart along the columns, `cell_aspect = 2.5 / 1.75 ≈ 1.4286` (only the *ratio* between the two spacings matters, not their absolute unit).

The fix: pick the largest cell height that fits *both* axes at the target aspect ratio —

```python
cell_height = min(real_height / rows, real_width / (cell_aspect * columns))
cell_width = cell_height * cell_aspect
```

— then compute how many source pixels that implies along each axis (`cell_width * columns / xres` and `cell_height * rows / yres`), and **center-crop** the DEM down to exactly that many pixels, discarding any excess margin evenly from both sides of the longer axis. The result: every one of the `rows x columns` cells covers an equal-shaped patch of ground, at the requested aspect ratio.

Returns the cropped array, the resulting cell width and height (in CRS units), and the pixel offset of the crop within the original (uncropped) band — the offset is needed later to draw the crop boundary on a preview of the full DEM.

**Caveat:** if the DEM's CRS is geographic (degrees), cell dimensions are in *degrees*, not true ground meters, since a degree of longitude covers a different ground distance than a degree of latitude (except at the equator). `main()` prints a warning in this case. For physically accurate cell dimensions, reproject the DEM to a projected CRS first (e.g. `gdalwarp -t_srs EPSG:32633 in.tif out.tif`).

`main()` derives `cell_aspect` from the `--row-spacing`/`--col-spacing` CLI flags (`cell_aspect = col_spacing / row_spacing`) when both are given, defaulting to `1.0` (square) otherwise. `validate_args` requires the two flags to be given together and both positive.

### `block_edges(source_size, num_blocks) -> np.ndarray`

```python
np.round(np.linspace(0, source_size, num_blocks + 1)).astype(int)
```

Splits a pixel dimension of length `source_size` into `num_blocks` contiguous chunks of nearly-equal size, returning the `num_blocks + 1` integer boundary indices. Because the boundaries are computed as evenly-spaced floats and then rounded independently, this works for *any* ratio of `source_size` to `num_blocks` — including cases that don't divide evenly, and cases where `num_blocks` exceeds `source_size` (in which case some blocks end up empty; see `average_to_grid`).

Used both for slicing the DEM into averaging blocks and for drawing grid lines in the preview.

### `average_to_grid(band, rows, columns) -> np.ndarray`

For each of the `rows x columns` output cells, slices the corresponding block of the (cropped) DEM using `block_edges` on both axes, and takes `np.ma.mean` over the unmasked pixels in that block. If a block has zero unmasked pixels (e.g. it's entirely nodata, or the grid is finer than the source resolution so a block is empty), the cell is set to `NaN` instead of raising an error.

### `remap_range(values, source_min, source_max, target_min, target_max) -> np.ndarray`

Standard linear remap:

```python
scale = (target_max - target_min) / (source_max - source_min)
remapped = target_min + (values - source_min) * scale
```

Output is then clamped to `[target_min, target_max]` — this matters when `--source-min`/`--source-max` are supplied manually and don't actually bound the DEM's real values, which would otherwise push some cells outside the intended target range. `NaN` inputs stay `NaN`.

### `round_to_step(values, step) -> np.ndarray`

```python
np.round(values / step) * step
```

Snaps each value to the nearest multiple of `step` (e.g. `step=5`: `191.6 -> 190`, `193.2 -> 195`). `NaN` is preserved (rounding a NaN yields NaN). Applied after remapping, if `--round-to` is given.

### `determine_source_range(band, source_min, source_max) -> (float, float)`

If both `--source-min` and `--source-max` are given, they're used as-is (useful for keeping a consistent scale across multiple DEM tiles processed independently). Otherwise, both are computed from the DEM's own valid (unmasked) pixel values via `np.ma.min`/`np.ma.max`. Raises if the DEM has no valid pixels at all. Note `validate_args` requires the two flags to be given together — a partial override isn't allowed, since a lopsided source range would silently skew the remap.

### `format_grid(grid) -> str`

Console rendering: comma-separated rows, values to 2 decimal places, `NaN` cells shown as the literal string `nan`.

### `save_csv` / `save_json`

Write the grid to disk. CSV: one row per line, values to 4 decimal places, `NaN` cells written as an empty field. JSON: nested list of lists, `NaN` cells become `null`, values rounded to 4 decimal places.

### `export_cropped_texture(background, background_transform, dem_transform, crop_row_start, crop_col_start, crop_height, crop_width, path)`

Crops a background image (`--preview-image`, or the DEM itself as a fallback) down to exactly the footprint that `crop_to_fixed_aspect_cells` computed for the grid, and saves it as a plain `.png`/`.jpg`/`.jpeg` — no geospatial metadata, just pixels. The intent is a texture asset that a renderer can lay over the physical model in exact alignment with the height grid, since both were cropped to the same real-world region.

Uses the same `dem_px_to_bg_px` coordinate conversion as `save_preview` (DEM pixel → world via `dem_transform` → background pixel via the inverse of `background_transform`) to convert the crop's four corners into the background image's pixel space, then takes the axis-aligned bounding box of those corners, clamped to the background image's actual bounds, and slices it out with plain NumPy indexing. Raises `ValueError` if that bounding box is empty (e.g. the DEM and background don't actually overlap in world coordinates — likely a CRS mismatch or unrelated images).

Written with `matplotlib.pyplot.imsave`: 2D (grayscale DEM fallback) arrays are colormapped with `"gray"`; multi-band arrays are written as-is (RGB/RGBA).

### `save_preview(...)`

Renders a PNG/JPG/PDF/SVG (matplotlib, `Agg` backend — no display required) showing:

- the background image (the DEM itself in grayscale, or a `--preview-image` raster in its natural colors)
- a red polygon marking the crop boundary computed by `crop_to_fixed_aspect_cells`
- each grid cell drawn as a semi-transparent, `viridis`-colored polygon (color = its remapped height, via `Normalize(target_min, target_max)`), outlined in yellow
- the cell's rounded value as centered text (white with a black outline, for legibility over any background color)
- cells that are `NaN` are hatched instead of colored
- a colorbar keyed to the same `Normalize` range, labeled with `--units`

**Coordinate alignment.** The crop boundary and grid cell edges are computed in the *DEM's* pixel space (from `crop_to_fixed_aspect_cells` and `block_edges`). To draw them on a background image that may have a different resolution, extent, or pixel grid (e.g. an orthophoto covering a larger or differently-sampled area), each DEM pixel coordinate is converted to world coordinates via the DEM's affine transform, then to the background image's pixel space via the *inverse* of the background's affine transform:

```python
def dem_px_to_bg_px(col, row):
    x, y = dem_transform * (col, row)       # DEM pixel -> world (CRS units)
    return (~background_transform) * (x, y)  # world -> background pixel
```

This only produces a correct overlay if the DEM and the background image share the same CRS. (Affine transforms preserve straight lines, so grid lines and the crop rectangle remain straight after conversion even without assuming both rasters are axis-aligned the same way — the code draws each edge as a line between two converted endpoints rather than assuming a simple rectangle.)

## CLI reference

| Flag | Required | Description |
|---|---|---|
| `input` | yes | Path to the input DEM `.tif` |
| `--rows` | yes | Number of output grid rows |
| `--columns` | yes | Number of output grid columns |
| `--target-min` / `--target-max` | yes | Output height range (e.g. `0` / `300` for mm) |
| `--source-min` / `--source-max` | no | Fix the source elevation range instead of auto-detecting from the DEM; must be given together |
| `--row-spacing` / `--col-spacing` | no | Physical pitch along each axis (any consistent unit); sets `cell_aspect = col_spacing / row_spacing` for rectangular cells instead of square. Must be given together, both positive |
| `--output` | no | Save the grid to `.csv` or `.json` |
| `--round-to` | no | Snap remapped values to the nearest multiple of this number |
| `--preview` | no | Save a `.png`/`.jpg`/`.jpeg`/`.pdf`/`.svg` visualization of the grid |
| `--preview-image` | no | Use a different georeferenced raster as the preview background instead of the DEM (same CRS required); also used as the source for `--export-texture` |
| `--units` | no | Unit label for `--preview` cell text/colorbar (default `mm`; pass `""` for none) |
| `--export-texture` | no | Save a `.png`/`.jpg`/`.jpeg` crop of `--preview-image` (or the DEM) matching the grid's crop footprint exactly |

All validation happens up front in `validate_args` before any raster I/O, so bad arguments fail fast with a specific message (e.g. `--rows and --columns must be positive integers`, `--source-min and --source-max must be provided together`).

## Error handling

`main()` wraps the whole pipeline in a single `try/except Exception`, printing `Error: <message>` to stderr and exiting with status 1 on any failure (missing file, invalid args, empty DEM, etc.) — there's no partial/silent output on error.

## Known limitations

- Cell sizing assumes the CRS's linear units are consistent across both axes; for geographic CRSs a warning is printed but the script still proceeds using degree-based cell dimensions (see `crop_to_fixed_aspect_cells` above).
- `--preview-image` alignment assumes both rasters share the same CRS; there's no reprojection step, so mismatched CRSs will silently misalign the overlay.
- Center-cropping discards DEM margin outside the largest cell-fitting extent; there's no option to pad instead of crop.
- `--row-spacing`/`--col-spacing` only capture a single fixed aspect ratio; a physical grid with non-uniform pitch (spacing that varies cell-to-cell) isn't representable.

## `fetch_satellite.py`

A separate, standalone script (not imported by `process_dem.py`) that solves the "I need a `--preview-image` but don't have one" problem: given a DEM, it downloads satellite tiles covering that exact bounding box and writes them out already aligned to the DEM's CRS, so there's no manual bounding-box matching to get wrong.

- Reads the DEM's `bounds` and `crs` via `rasterio` (does not touch pixel data).
- `contextily.bounds2img(..., ll=True)` fetches an XYZ tile mosaic (default provider `Esri.WorldImagery`) covering those bounds. `ll=True` tells contextily the input bounds are lon/lat (EPSG:4326); it internally reprojects to the tile servers' native Web Mercator (EPSG:3857) to select tiles, and returns the mosaic as an `(H, W, 4)` RGBA array plus its extent *in EPSG:3857*.
- Because that mosaic is in Web Mercator, not the DEM's CRS, it's reprojected band-by-band with `rasterio.warp.reproject` onto a destination array whose `transform` is built directly from the DEM's own `bounds` (`rasterio.transform.from_bounds`) — this is what guarantees the output lines up pixel-for-pixel with `process_dem.py`'s coordinate-alignment logic (`dem_px_to_bg_px` in `save_preview`/`export_cropped_texture`), which assumes the background raster shares the DEM's CRS.
- Output resolution is independent of the DEM's own pixel size — `--scale` (default `3.0`) multiplies the DEM's width/height to get a higher-detail texture from the same bounds.
- `--zoom` controls how much detail is requested from the tile server itself (higher = more source tiles = slower fetch, sharper before the final resample); left as `"auto"` by default, which lets contextily pick based on the requested bounds.
- `--provider` resolves a dotted name (e.g. `Esri.WorldImagery`) against `contextily.providers` via chained `getattr`, so any provider contextily knows about can be used, not just the default.

Needs live internet access to the tile server when run; there's no offline/cached fallback.
