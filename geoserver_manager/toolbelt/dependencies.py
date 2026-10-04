#! python3  # noqa: E265

"""
Dependency management module.
Ensures geoservercloud and its deps are available in the QGIS Python env.

QGIS's Python often does not include system site-packages, so transitive
dependencies like xmltodict may be missing. We bundle all required WHL
files in extras/ and add them to sys.path at plugin startup.
"""

# standard
import importlib
import logging
import sys

# PyQGIS
from qgis.core import Qgis
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QMessageBox

# project
from geoserver_manager.__about__ import DIR_PLUGIN_ROOT
from geoserver_manager.toolbelt.log_handler import PlgLogger

# All bundled WHLs. Order matters: deps first, then geoservercloud
#
# The geoservercloud wheel is the upstream one with its geoserver_acceptance_tests
# package removed (15.3 MB of test fixtures, none of it imported): 16 MB -> 49 KB.
# When bumping the version, strip the fresh wheel the same way, e.g.
#   zip -d geoservercloud-<v>-py3-none-any.whl 'geoserver_acceptance_tests/*'
# and drop the matching lines from its dist-info/RECORD.
EXTRAS_DIR = DIR_PLUGIN_ROOT / "extras"
BUNDLED_WHLS = [
    EXTRAS_DIR / "xmltodict-1.0.4-py3-none-any.whl",
    EXTRAS_DIR / "geoservercloud-0.8.5-py3-none-any.whl",
]
# The version the plugin is written against. Its raw-REST workarounds ride on
# library internals, so a different version in the QGIS profile is worth a
# loud warning even when the import works.
GSC_REQUIRED = "0.8.5"


def _report_resolved_version(logger) -> None:
    """Log which geoservercloud was imported, and warn if it is not the pin."""
    import importlib.metadata

    import geoservercloud

    try:
        version = importlib.metadata.version("geoservercloud")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    origin = getattr(geoservercloud, "__file__", "?")
    if version == GSC_REQUIRED:
        logger(
            f"geoservercloud {version} from {origin}", log_level=Qgis.MessageLevel.Info
        )
    else:
        logger(
            f"geoservercloud {version} from {origin}. The plugin is tested with "
            f"{GSC_REQUIRED}; workspace/datastore workarounds may misbehave.",
            log_level=Qgis.MessageLevel.Warning,
            push=True,
        )


def _add_whls_to_path(logger=None):
    """Add all bundled WHLs to sys.path if not already present."""
    for whl in BUNDLED_WHLS:
        whl_str = str(whl)
        if not whl.exists():
            if logger:
                logger(
                    f"Bundled WHL not found: {whl}",
                    log_level=Qgis.MessageLevel.Warning,
                )
            continue
        if whl_str not in sys.path:
            sys.path.insert(0, whl_str)
            if logger:
                logger(
                    f"Added to sys.path: {whl.name}",
                    log_level=Qgis.MessageLevel.Info,
                )


def _try_import(logger=None) -> bool:
    """Invalidate import caches and try importing geoservercloud."""
    importlib.invalidate_caches()
    try:
        import geoservercloud  # noqa: F401

        return True
    except Exception as e:
        if logger:
            import traceback

            tb = traceback.format_exc()
            logger(
                f"geoservercloud import error: {e}\nTraceback:\n{tb}",
                log_level=Qgis.MessageLevel.Warning,
            )
        return False


def ensure_dependencies() -> bool:
    """Ensure geoservercloud is installed and importable.

    Strategy:
    1. Direct import: already available, done.
    2. Add bundled WHLs (deps + geoservercloud) to sys.path.
    3. Show error dialog and return False.

    :return: True if dependency is available, False otherwise.
    """
    # The library logs every request payload at DEBUG: a PostGIS or WFS
    # store create with its plaintext password included. A DEBUG root handler
    # set up by anything else in QGIS would then write it to disk.
    logging.getLogger("geoservercloud").setLevel(logging.INFO)

    logger = PlgLogger().log

    # 1. Already importable? (an install in the QGIS profile wins over the
    #    bundled wheel, say so, and which version it is)
    if _try_import():
        _report_resolved_version(logger)
        return True

    logger(
        "'geoservercloud' not found. Loading bundled WHLs...",
        log_level=Qgis.MessageLevel.Warning,
    )

    # 2. Add all bundled WHLs to sys.path
    _add_whls_to_path(logger)

    if _try_import(logger):
        _report_resolved_version(logger)
        return True

    # 3. All methods failed
    logger(
        "Could not import geoservercloud even after adding WHLs.",
        log_level=Qgis.MessageLevel.Critical,
    )
    error_msg = QCoreApplication.translate(
        "Dependencies",
        "<b>GeoServer Manager could not start.</b><br><br>"
        "The bundled <code>geoservercloud</code> library did not import. "
        "Reinstalling the plugin usually fixes it; otherwise install it into "
        "QGIS's Python by running in a terminal:<br>"
        "<code>pip install geoservercloud</code><br><br>"
        "Details are in the QGIS log panel, GeoServer Manager tab.",
    )
    QMessageBox.critical(
        None,
        QCoreApplication.translate(
            "Dependencies", "GeoServer Manager: missing library"
        ),
        error_msg,
    )
    return False
