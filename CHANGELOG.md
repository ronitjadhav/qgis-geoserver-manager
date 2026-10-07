# CHANGELOG

The format follows [Keep a Changelog](https://keepachangelog.com/), and the versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

The first release: a QGIS plugin that manages a GeoServer through its REST
API, built on [python-geoservercloud](https://github.com/camptocamp/python-geoservercloud).
Tested with GeoServer 2.28 and 3.0.

### Added

- **Connection.** A settings page with the server URL, the user name and
  password, and *Test connection*. Several saved connections, with a switch
  beside the dialog's status line when there are 2 or more. The password is
  plain text in the QGIS settings by default. A connection can keep it in
  QGIS's encrypted authentication database instead. The plugin follows
  QGIS's proxy settings. A connection that fails says why. The reasons it
  names: a wrong password, an account without rights, a private certificate
  authority, a pasted web address, a server that is not GeoServer.
- **Workspaces.** List, add, edit and delete workspaces. Set the default
  workspace, the isolated flag and the namespace URI. Give a workspace its
  own WMS, WFS, WCS and WMTS settings, or send it back to the global ones.
- **Datastores.** Every datastore type. PostGIS, shapefile, directory,
  GeoPackage and Web Feature Server stores have a form of their own. Any
  other type takes GeoServer's name and parameters. Add, edit, rename, enable
  or disable, reset and delete. The plugin checks a saved store at once. A
  stored password stays when the form leaves it blank.
- **Coverage stores.** GeoTIFF, COG, ImageMosaic, ArcGrid and WorldImage
  stores: add, edit, reset and delete. Read a coverage's SRS, format, pixel
  size, bounds and bands. Publish a coverage as a layer, or publish a QGIS
  raster layer as a new store.
- **Cascaded stores.** WMS and WMTS stores that proxy another server. Create
  one from a GetCapabilities URL. Publish the layers the remote advertises,
  view and delete the cascaded layers, delete the store.
- **Layers.** Every published layer with its workspace, type, store and
  default style. Publish a table of a datastore with its SRS, title,
  abstract and keywords. Edit a layer's name, title, abstract, keywords,
  SRS, projection policy, enabled and advertised flags, and CQL filter. The
  plugin sends only what changed. Update a layer from its data, set its
  default and additional styles, delete.
- **Layer groups.** Global and per-workspace groups with their mode, layers,
  order, styles, nested groups, Earth Observation root layer and bounds.
  Add, edit and delete them.
- **Styles.** SLD, CSS, YSLD and MBStyle styles, global and per workspace.
  Upload one from pasted text, a file, a zip with its images, or the
  symbology of a QGIS layer. Edit the body in place, rename, copy, save to
  disk, delete. See which layers and groups use a style. The style dialog
  shows the legend.
- **Tile cache.** The cached layers with their gridsets and formats. Edit a
  layer's cache configuration, zoom levels and parameter filters. Seed,
  reseed and truncate part of a cache, and follow or stop the running tasks.
  Stop caching a layer, or cache one again.
- **Server.** The contact details, the global settings, and each service's
  settings with its capabilities URL. The logging profile with the log's
  last lines, and the catalog's reload and reset. Each row also opens
  GeoServer's own page.
- **QGIS both ways.** Add a layer or a layer group to the project as WMS,
  WFS or WMTS. Preview a layer on a map of its own, in the dialog or in a
  browser. Publish a vector or raster layer of the open project, with its
  symbology, after a check of its CRS. Push a QGIS layer's style to
  GeoServer, or apply a GeoServer style to a QGIS layer. Both work from the
  dialog and from the layer tree's context menu.
- **In the dialog.** Loads, uploads and deletes run in the background, and
  *Cancel* stops them. Tables sort, filter and page. A large server lists
  its names at once. The details follow for the page on screen. F5, Ctrl+F,
  Esc and Del work as keyboard shortcuts. Every error says what to do next
  and stays until closed. The colours are readable on dark themes. The
  interface is translatable and ships a partial French locale, plural forms
  included.
