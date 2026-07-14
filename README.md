# dem-to-grid

Reduce a DEM GeoTIFF into a small grid of averaged, height-remapped values.

See [docs.md](docs.md) for a detailed technical explanation of how the script works internally.

## WSL setup

Requires Python 3 with the following packages: `rasterio`, `numpy`, `matplotlib`. `fetch_satellite.py` (see below) additionally needs `contextily`.

```bash
cd dem-to-grid
pip install rasterio numpy matplotlib contextily
```

## Fetching a matching satellite image

If you don't already have a reference image for `--preview-image`/`--export-texture`, `fetch_satellite.py` downloads one for you, pre-aligned to a DEM's exact bounds and CRS — no manual bounding-box matching needed:

```bash
python3 fetch_satellite.py terrain_dem.tif terrain_satellite.tif
```

This fetches Esri World Imagery tiles (free, no API key) covering the DEM's bounding box, reprojects them onto a raster with the DEM's own bounds/CRS, and saves a GeoTIFF you can pass straight to `--preview-image`. Options:

- `--scale` (default `3.0`): output resolution as a multiple of the DEM's own pixel dimensions
- `--zoom`: tile zoom level (default: auto-selected)
- `--provider`: dotted contextily provider name (default `Esri.WorldImagery`)

Requires internet access to the tile server at runtime.

## Usage

Auto-detect source elevation range from the DEM:

```bash
python3 process_dem.py terrain.tif --rows 6 --columns 10 --target-min 0.1 --target-max 2.0
```

Use a fixed source elevation range:

```bash
python3 process_dem.py terrain.tif --rows 6 --columns 10 \
  --source-min 2644 --source-max 6911 --target-min 0.1 --target-max 2.0
```

Save the grid to CSV or JSON:

```bash
python3 process_dem.py terrain.tif --rows 6 --columns 10 \
  --target-min 0.1 --target-max 2.0 --output terrain_grid.csv
```

## Rounding

Round the remapped height values to the nearest multiple of a step size with `--round-to`, e.g. for a platform that only supports increments of 5:

```bash
python3 process_dem.py terrain.tif --rows 6 --columns 10 \
  --target-min 0 --target-max 300 --round-to 5
```

`191.6` becomes `190`, `193.2` becomes `195`, etc. Applies to console output, `--output`, and `--preview` alike.

## Preview

Save an image showing the grid overlaid on the DEM: each cell is tinted by a color scale based on its remapped height, labeled with its value, with a color-key colorbar and the crop boundary (red) — so the grid can be sanity-checked visually before running it on the physical platform:

```bash
python3 process_dem.py terrain.tif --rows 8 --columns 12 \
  --target-min 0 --target-max 300 --preview preview.png
```

Overlay it on a *different* georeferenced image instead (e.g. an orthophoto or reference map covering the same area) with `--preview-image`. Both rasters must share the same CRS; the overlay is aligned using each raster's geotransform, so it lines up correctly even if the two images have different pixel resolutions or extents:

```bash
python3 process_dem.py terrain_dem.tif --rows 8 --columns 12 \
  --target-min 0 --target-max 300 --preview preview.png --preview-image terrain_photo.tif
```

The unit label appended to each cell's value and the colorbar defaults to `mm`; override with `--units` (e.g. `--units cm`, or `--units ""` for none).

## Texture export

Save a cropped copy of `--preview-image` (or the DEM itself, if `--preview-image` isn't given), trimmed to exactly the same crop footprint used for the grid, with `--export-texture`. This lets a renderer map the texture and the height grid onto the physical model in perfect alignment:

```bash
python3 process_dem.py terrain_dem.tif --rows 8 --columns 12 \
  --target-min 0 --target-max 300 --preview-image terrain_photo.tif --export-texture texture.png
```

Output is a plain `.png`/`.jpg`/`.jpeg` (no geospatial metadata) — just the pixels inside the same region shown by the red boundary in `--preview`.

## Square (or rectangular) cells

`--rows`/`--columns` describe a fixed physical display grid (e.g. a pin/actuator array under a projector). Since the DEM's pixel resolution and aspect ratio can differ from that grid, the script automatically **center-crops** the DEM before averaging so every output cell corresponds to a real-world patch of terrain of the correct shape, rather than a rectangle stretched to fit:

- By default (no spacing flags) each cell is **square**: the script computes the largest square cell size that fits within the DEM's real-world extent (using the raster's pixel resolution), then crops off any excess margin evenly from both sides of the longer axis.
- If your physical grid's cells *aren't* square — e.g. a peg/pipe array with different spacing along each axis — pass `--row-spacing` and `--col-spacing` (any consistent unit, only their ratio matters) to make the crop match that real aspect ratio instead of forcing 1:1. For example, a grid of pegs spaced 1.75in apart along the rows and 2.5in apart along the columns:

  ```bash
  python3 process_dem.py terrain.tif --rows 10 --columns 12 \
    --target-min 0 --target-max 300 --row-spacing 1.75 --col-spacing 2.5
  ```

  Both flags must be given together; omitting both keeps the default square-cell behavior.
- If the crop changes the DEM's dimensions, a line is printed reporting the original/cropped pixel sizes and the resulting cell size (or width x height, for rectangular cells).
- If the DEM's CRS is **geographic** (degrees, e.g. EPSG:4326) rather than **projected** (meters, e.g. UTM), a warning is printed: cells will be sized in degrees, not true ground meters, because a degree of longitude and a degree of latitude cover different distances. Reproject the DEM to a projected CRS (e.g. `gdalwarp -t_srs EPSG:32633 in.tif out.tif`) for physically accurate cell dimensions.

Notes:
- Grid cells with no valid (non-nodata) source pixels are output as `NaN` / empty CSV cells / `null` in JSON.
- Values outside a user-supplied `--source-min`/`--source-max` are clamped to the target range.
