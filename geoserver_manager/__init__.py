#! python3  # noqa: E265

# ----------------------------------------------------------
# Copyright (C) 2015 Martin Dobias
# ----------------------------------------------------------
# Licensed under the terms of GNU GPL 2
# --------------------------------------------------------------------


def classFactory(iface):
    """Load the plugin class.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    from .plugin_main import GeoServerManagerPlugin

    return GeoServerManagerPlugin(iface)
