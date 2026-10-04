# CHANGELOG

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## Unreleased

The first release: a QGIS plugin that manages a GeoServer through its REST
API, built on [python-geoservercloud](https://github.com/camptocamp/python-geoservercloud).

### Added

- **Connection.** A settings page with the server URL, the login and *Test
  connection*. Several saved server profiles, with a switch beside the
  dialog's status line when there are two or more. The password is plain
  text in the QGIS settings, or kept in QGIS's encrypted authentication
  database when the profile asks for it. The plugin follows QGIS's proxy
  settings, and a connection that fails says why: a wrong password, an
  account without rights, a private certificate authority, a pasted web
  address, a server that is not GeoServer.
- **Workspaces.** List, add, edit and delete workspaces; set the default
  workspace, the isolated flag and the namespace URI; give a workspace its
  own WMS, WFS, WCS and WMTS settings or send it back to the global ones.
- **Datastores.** Every datastore type: PostGIS, shapefile, directory,
  GeoPackage and Web Feature Server stores have a form of their own, any
  other type takes GeoServer's name and parameters. Add, edit, rename, enable
  or disable, reset and delete; a saved store is checked at once, and a
  stored password is kept when the form leaves it blank.
- **Coverage stores.** GeoTIFF, COG, ImageMosaic, ArcGrid and WorldImage
  stores: add, edit, reset and delete, read a coverage's SRS, format, pixel
  size, bounds and bands, publish a coverage as a layer, and publish a QGIS
  raster layer as a new store.
- **Cascaded stores.** WMS and WMTS stores that proxy another server: create
  one from a GetCapabilities URL, publish the layers the remote advertises,
  view and delete the cascaded layers, delete the store.
- **Layers.** Every published layer with its workspace, type, store and
  default style. Publish a table of a datastore with its SRS, title,
  abstract and keywords. Edit a layer's name, title, abstract, keywords,
  SRS, projection policy, enabled and advertised flags and CQL filter; only
  what changed is sent. Update a layer from its data, set its default and
  additional styles, delete.
- **Layer groups.** Global and per-workspace groups with their mode, layers,
  order, styles, nested groups, Earth Observation root layer and bounds:
  add, edit and delete.
- **Styles.** SLD, CSS, YSLD and MBStyle styles, global and per workspace:
  upload from pasted text, a file, a zip with its images, or the symbology
  of a QGIS layer; edit the body in place, rename, copy, save to disk,
  delete; see where a style is used; a legend in the style dialog.
- **Tile cache.** The cached layers with their gridsets and formats: edit a
  layer's cache configuration, zoom levels and parameter filters; seed,
  reseed and truncate part of a cache and follow or stop the running tasks;
  stop caching a layer, or cache one again.
- **Server.** The contact details, the global settings, each service's
  settings with its capabilities URL, the logging profile with a view of the
  log's last lines, and the catalog's reload and reset. Each row also opens
  GeoServer's own page for it.
- **QGIS both ways.** Add a layer or a layer group to the project as WMS,
  WFS or WMTS; preview a layer on a map of its own, in the dialog or in a
  browser; publish a vector or raster layer of the open project, its
  symbology with it, after a check of its CRS; push a QGIS layer's style to
  GeoServer or apply a GeoServer style to a QGIS layer, from the dialog or
  from the layer tree's context menu.
- **In the dialog.** Loads, uploads and deletes run in the background and can
  be cancelled; tables sort, filter and page, and a large server lists its
  names at once with the details following for the page on screen; F5,
  Ctrl+F, Esc and Del; every error says what to do next and stays until
  closed; readable on dark themes; translated into French, plural forms
  included.
