# dem-data-extractor

Extract a DEM GeoTIFF into a raw float32 binary + JSON metadata (and optional PNG preview / CSV), for fast loading in an external runtime such as Unity — no GDAL/rasterio dependency needed at runtime, just a flat binary and a small JSON file.

## Setup

```bash
cd dem-data-extractor
pip install -r requirements.txt
```

## Usage

```bash
python3 extract_dem.py terrain.tif --output-dir out/
```

Writes into `out/`:
- **`dem.bin`** — every pixel's elevation as a little-endian 32-bit float, row-major (row 0 = the top/north row of the source raster), with no header. Size is exactly `width * height * 4` bytes.
- **`dem_metadata.json`** — everything needed to interpret `dem.bin`:

  ```json
  {
    "width": 900,
    "height": 900,
    "minElevation": 2644.0,
    "maxElevation": 6911.0,
    "noDataValue": -32768.0,
    "bounds": { "left": 88.4998, "right": 88.7498, "bottom": 27.7501, "top": 28.0001 },
    "crs": "EPSG:4326",
    "epsg": 4326,
    "pixelSize": { "x": 0.0002777, "y": 0.0002777 },
    "dataType": "float32",
    "byteOrder": "little",
    "pixelOrder": "row-major, row 0 = top (north) row, matching the source raster"
  }
  ```

  `minElevation`/`maxElevation` are computed over valid (non-nodata, non-NaN) pixels only. `noDataValue` is `null` if the source raster has no nodata tag set — `dem.bin` still contains whatever raw sentinel/NaN values the source had, unmodified.

Add `--preview` to also save **`dem-preview.png`**, a normalized grayscale visualization (nodata pixels transparent) — for eyeballing only; `dem.bin` holds the real measurements. Use `--preview-max-dim N` to downsample large DEMs so the preview's longer side is at most `N` pixels.

Add `--csv` to also save `dem.csv` (one row per DEM row) — useful for quick prototyping/inspection, but avoid it for large DEMs; it's much bigger and slower to parse than `dem.bin`.

All output filenames can be overridden: `--bin-name`, `--metadata-name`, `--preview-name`, `--csv-name`.

## Reading dem.bin

Any runtime that can read a flat binary file works. E.g. in C# (Unity):

```csharp
byte[] bytes = File.ReadAllBytes("dem.bin");
float[] elevations = new float[bytes.Length / 4];
Buffer.BlockCopy(bytes, 0, elevations, 0, bytes.Length);
// elevations[row * width + col], row 0 = top (north)
```
