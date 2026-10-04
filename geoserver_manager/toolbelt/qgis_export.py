#! python3  # noqa: E265

"""
Turning a QGIS layer into something GeoServer can ingest: a name it can carry,
a GeoPackage for a vector layer, a GeoTIFF for a raster.

`geoserver_name` imports nothing from QGIS, so the naming rules (the part that
is easy to get subtly wrong) are tested in an interpreter without QGIS, like
the CI unit job. The export functions read a live layer and must run on the
GUI thread (invariant 9 in docs/development/invariants.md).
"""

import re
import unicodedata

# A published layer's name ends up in XML element positions (a WFS type name is
# an XML NCName) and in URLs, so only these characters are safe.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_RUNS = re.compile(r"_{2,}")


def geoserver_name(text):
    """A GeoServer-safe name derived from `text`, a QGIS layer name, usually.

    Accents are folded ("Rivière" → "Riviere"), anything else outside
    [A-Za-z0-9_.-] becomes an underscore, and runs of underscores collapse.
    Separators at either end go, so "  Roads (2024) " is "Roads_2024" rather
    than "_Roads_2024_", except a leading underscore the caller actually
    typed, which is kept. A name left starting with a digit gets one
    underscore in front: an NCName may start with an underscore, so the digit
    does not have to be thrown away.
    """
    text = text or ""
    folded = "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )
    cleaned = _RUNS.sub("_", _UNSAFE.sub("_", folded)).strip("_-.")
    if not cleaned:
        return "layer"
    if text.startswith("_"):
        cleaned = f"_{cleaned}"
    elif cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def require_crs(layer):
    """Refuse, before any request, a layer GeoServer could not declare an SRS for.

    Raises ValueError with the sentence the forms show. Both publish paths
    call it first, so a vector and a raster without a CRS get the same answer.
    """
    if not layer.crs().isValid():
        from qgis.PyQt.QtCore import QCoreApplication

        raise ValueError(
            QCoreApplication.translate(
                "QgisExport", "'{}' has no CRS. Set one in its layer properties first."
            ).format(layer.name())
        )


def reprojection_target(layer):
    """The CRS to write the layer in for GeoServer, or None when its own will do.

    GeoServer declares a layer's SRS by EPSG code; a CRS without one (a custom
    or user-defined CRS, `USER:100001`) would be published as UNKNOWN and the
    layer would not render in any client. EPSG:4326 is the code every
    GeoServer knows. Vectors are reprojected on export
    (`export_to_geopackage(..., target_crs=…)`); a raster is uploaded as it
    is, so its caller refuses instead.
    """
    from qgis.core import QgsCoordinateReferenceSystem

    if layer.crs().authid().upper().startswith("EPSG:"):
        return None
    return QgsCoordinateReferenceSystem("EPSG:4326")


def export_to_geopackage(layer, path, table_name, target_crs=None):
    """Write one QGIS vector layer into a single-table GeoPackage.

    GUI thread only. The table name becomes the published layer's name, because
    GeoServer configures a feature type per table when the file is uploaded.
    With `target_crs` the geometries are reprojected on the way and the table
    is written in that CRS. Raises RuntimeError with QGIS's own message when
    the write fails.
    """
    from qgis.core import (
        QgsCoordinateTransform,
        QgsCoordinateTransformContext,
        QgsVectorFileWriter,
    )

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table_name
    options.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    )
    if target_crs is not None and target_crs != layer.crs():
        options.ct = QgsCoordinateTransform(
            layer.crs(), target_crs, QgsCoordinateTransformContext()
        )
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsCoordinateTransformContext(), options
    )
    # (error code, message, …); the tuple grew across QGIS versions.
    if result[0] != QgsVectorFileWriter.WriterError.NoError:
        from qgis.PyQt.QtCore import QCoreApplication

        detail = next(
            (item for item in result[1:] if isinstance(item, str) and item), ""
        )
        raise RuntimeError(
            QCoreApplication.translate(
                "QgisExport", "QGIS could not write '{}' as a GeoPackage: {}"
            ).format(layer.name(), detail or result[0])
        )
    return path


def local_geotiff_path(layer):
    """The layer's own file when it is a plain local GeoTIFF, else None.

    Such a file is uploaded as it is: re-encoding costs time and, without the
    source's compression and overviews, often space. A subdataset, a remote
    /vsicurl/ file or a layer whose CRS was overridden in QGIS is not "the file
    as it is", so those go through export_to_geotiff.
    """
    from pathlib import Path

    from qgis.core import QgsProviderRegistry

    if layer.providerType() != "gdal":
        return None
    parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
    # A subdataset or an open option stays inside "path" (NETCDF:"a.nc":var,
    # a.tif|option=…), so the suffix check turns those down as well.
    path = Path(str(parts.get("path") or layer.source()))
    if path.suffix.lower() not in (".tif", ".tiff") or not path.is_file():
        return None
    if layer.crs() != layer.dataProvider().crs():
        return None
    return path


def export_to_geotiff(layer, path):
    """Write a QGIS raster layer as a tiled, DEFLATE-compressed GeoTIFF.

    GUI thread only. The grid is the provider's own (no resampling), and the
    CRS is the layer's: a CRS override set in QGIS is written into the file,
    which is what an override means, without reprojecting the pixels
    (measured). Raises RuntimeError when QGIS cannot write it.
    """
    from qgis.core import (
        Qgis,
        QgsCoordinateTransformContext,
        QgsRasterFileWriter,
        QgsRasterPipe,
    )
    from qgis.PyQt.QtCore import QCoreApplication

    provider = layer.dataProvider()
    pipe = QgsRasterPipe()
    if not pipe.set(provider.clone()):
        raise RuntimeError(
            QCoreApplication.translate(
                "QgisExport", "QGIS could not read '{}' for export."
            ).format(layer.name())
        )
    writer = QgsRasterFileWriter(str(path))
    writer.setOutputFormat("GTiff")
    # setCreateOptions is deprecated in favour of setCreationOptions, which
    # QGIS 3.40 does not have yet.
    set_options = getattr(writer, "setCreationOptions", None) or writer.setCreateOptions
    set_options(["COMPRESS=DEFLATE", "TILED=YES"])
    result = writer.writeRaster(
        pipe,
        provider.xSize(),
        provider.ySize(),
        provider.extent(),
        layer.crs(),
        QgsCoordinateTransformContext(),
    )
    if result != Qgis.RasterFileWriterResult.Success:
        raise RuntimeError(
            QCoreApplication.translate(
                "QgisExport", "QGIS could not write '{}' as a GeoTIFF ({})."
            ).format(layer.name(), result)
        )
    return path
