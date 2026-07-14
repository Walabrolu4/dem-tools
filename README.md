# dem-tools

A collection of tools for working with DEM (Digital Elevation Model) GeoTIFFs.

## Tools

- [dem-to-grid](dem-to-grid/) — reduce a DEM into a small grid of averaged, height-remapped values for a physical relief model (e.g. pin/actuator array, PVC pipe rig), with preview and satellite-imagery-alignment support. See [dem-to-grid/README.md](dem-to-grid/README.md).
- [dem-data-extractor](dem-data-extractor/) — extract a DEM into a raw float32 binary + JSON metadata (plus optional PNG preview/CSV) for fast loading in an external runtime such as Unity. See [dem-data-extractor/README.md](dem-data-extractor/README.md).
