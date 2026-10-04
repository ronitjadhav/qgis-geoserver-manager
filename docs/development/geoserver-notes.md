# GeoServer and library notes

Everything here was measured, not assumed: against GeoServer **2.28.5** in the
[docker sandbox](environment.md), and against the
[python-geoservercloud](https://github.com/camptocamp/python-geoservercloud)
version bundled in `geoserver_manager/extras/`. Use these notes when you change
server calls.

Two rules frame all of it. Every GeoServer call goes through the library. Each
gap in the library is recorded as a row in
[issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1)
before it is worked around here. That way it can be fixed upstream. A workaround
carries a `TODO(#1)` comment at the call site.

## What the library does, and does not

- Every REST verb calls `raise_for_status()` **except** GET/DELETE on 404 and POST on
  409. Those three come back as `(content, status)`, which is exactly why `_check`
  exists. `requests` exceptions all subclass `OSError`, so catch `HTTPError` *before*
  `OSError` (see `toolbelt/probe.py`).
- `create_workspace` and `create_datastore` **upsert**. There is no `update_*`, no
  `delete_datastore`, no workspace rename, no "set default workspace" call (the
  `set_default_workspace=True` kwarg only sets a client-side attribute). Those are
  `_raw_rest` workarounds carrying `TODO(#1)`, each with a row in
  [issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1). The
  library-first rule in Conventions says how new ones are handled.
- **GeoServer always has exactly one default workspace, and it cannot be unset.**
  `GET /rest/workspaces/default.json` never 404s: with `default.xml` deleted it
  answers the first workspace. The "Default Workspace" checkbox of GeoServer's web
  interface only *sets*. `WorkspaceEditPage` has `if (defaultWs) setDefaultWorkspace(ws)`
  with no else, so unchecking it and saving is a no-op. The plugin therefore shows the
  default read-only and checked, and marks it in the Workspaces list. It reads it
  live from the server on every load and every edit form.
- The client strips a trailing `/` from the URL itself. It has no timeout parameter
  at all: `TIMEOUT = 120` is a module constant, and `RestClient.get` takes no
  `timeout`. That is why `toolbelt/probe.py` uses `requests` directly: a dead host must
  cost 10 s, not 2 minutes (row 20 of #1). The Server tab's log view does too
  (`_log_tail`, row 60). It streams the file, keeps only its end, and stops between
  two chunks on Cancel. The library's client reads a body whole. `verifytls` is
  the *Verify the server's TLS certificate* setting (default on). The probe catches
  `requests.exceptions.SSLError` before `OSError`, so a private-CA server is reported
  as a certificate problem, not as "is the server running?".
- **Layers of every type** (rows 39, 48 and 49 of #1): `GET /rest/layers.json` is the
  one list where vector, raster and cascaded layers all appear. Walking datastores
  then feature types, as the Layers tab did, misses the others.
  `GET /rest/layers/{ws}:{name}.json` gives `type` (VECTOR / RASTER / WMS / WMTS) and
  `defaultStyle` (`{"name": ""}` for a cascaded WMS layer). It also gives `resource`
  with `@class` (featureType / coverage / wmsLayer / wmtsLayer) and an `href`, except
  a **wmtsLayer, which has no href** on 2.28.5. Such a store is found by asking the
  workspace's WMTS stores for their layers. The href carries GeoServer's own idea of
  its base URL (behind a proxy, an inside name). So the tab parses the store segment
  out of it and never follows it. `rest_service.get_layer()` exists, but its `Layer`
  model keeps only the resource's name.
  Per type, the detail view, the browser preview and the delete go to the resource.
  A feature type goes through the library. A coverage goes through the Coverage
  Stores tab's `_coverage_detail` and a raw `DELETE …/coverages/{name}?recurse=true`
  (no `delete_coverage()` upstream). A cascaded layer goes through the Cascaded
  Stores tab's helpers. All of them are reached on the shared dialog class.
  *Add to QGIS* offers WFS for VECTOR only.
- **Legend and browser preview** (rows 39–40 of #1): `get_legend_graphic()` is a plain
  GET through the REST client, stateless, so worker-safe. But it returns the raw
  `Response`. An OGC exception is **HTTP 200 with an XML body**, so the content type
  decides, and it runs with the client's 120 s timeout. GetLegendGraphic needs a
  `LAYER` even for a stored style, and **of the style's kind** (measured on 2.28.5).
  A raster style drawn with a vector layer is a blank 20x20 image. A vector style
  drawn with a raster layer is a ServiceException "we need a RasterSymbolizer". So
  for an SLD with a `RasterSymbolizer`, `tab_styles.py` takes the first coverage of
  `/rest/workspaces/{ws}/coverages.json`. Otherwise it takes the first feature type
  of `…/featuretypes.json`: a workspace's resources across its stores, names **bare**,
  which it qualifies. The library lists them per store only. It tries the style's
  own workspace first, and explains in the `image` field when there is none. The
  legend lands through `_run_quietly`, in its own task slot, so it neither supersedes
  a load nor turns Refresh into Cancel. It lands into a modal dialog that may already
  be closed, so the landing checks `finished` and `sip.isdeleted` first. The Styles
  table's Format and Version columns cost one definition GET per style, fanned out.
  Whether a style is SLD decides what *Apply to a QGIS layer* can do with it.
  *Preview in a browser* is GeoServer's own OpenLayers GetMap page. The pure
  `_preview_url` builds it from `latLonBoundingBox` (a group: its `bounds`), with a
  world fallback. The browser's session is not the plugin's, so a secured server asks
  it to log in, which the tooltip says.
- **Embedded preview** (`gui/dlg_preview.py`): a `QgsMapCanvas` with a WMS
  `QgsRasterLayer` from `_layer_uri`, never added to the project, and the provider's
  own `identify()` for GetFeatureInfo. `IdentifyText` is what the WMS provider offers,
  and GeoServer answers as `text/plain`. A file raster offers `IdentifyValue`, which
  is how the dialog is tested without a server. The WMS provider needs the canvas
  extent and size to turn the point into a pixel. One map tool does both: a drag
  pans, a release within 3 px of the press identifies. `WA_DeleteOnClose` plus
  `stopRendering()` in `closeEvent` make closing mid-render safe, and the window is
  non-modal, so the main dialog's tasks carry on. Every `QgsMapCanvas` connects
  itself to the project's `readProject` and `writeProject`. Opening a project moved
  the preview to that project's CRS and extent, where identify missed, and a save
  wrote a nameless `<mapcanvas>` into the .qgs. PyQt cannot disconnect a connection
  QGIS made in C++ ("disconnect() failed", measured), so the dialog connects after the
  canvas and undoes both.
- **Thread safety:** the REST methods are stateless `requests.*` calls and are safe to
  run through `_fan_out` (the datastore list does this). `self.wms` / `self.wmts` on
  the client are shared state. OWS calls must not be fanned out the same way.
- **File-based datastores** (row 30 of #1): the library's typed creators stop at
  PostGIS, JNDI and PMTiles. So the Shapefile, *Directory of spatial files (shapefiles)*
  and GeoPackage forms build their parameter map and go through the generic
  `create_datastore`. That map has to get two things right. A GeoPackage store must
  carry **`dbtype: geopkg`**: that is how GeoServer picks the factory. An empty
  `charset` is omitted, not sent blank, because blank is not "use your default".
  GeoServer fills in `namespace` itself, and the edit merge keeps it, along with
  everything else the form does not show.
- **Editing a store is one partial PUT too** (rows 54 and 55 of #1). Measured on
  2.28.5: a `PUT …/coveragestores/{cs}.json`, `…/wmsstores/{s}.json` or
  `…/wmtsstores/{s}.json` with only the changed fields merges. A coverage store rename
  keeps its coverages and layers. A datastore rename (one PUT with the new `name` on
  the old path) keeps its feature types, layers, groups and GWC layers. A cascaded
  store **cannot** be renamed (403). A cascaded store's password comes back
  `crypt1:…`. A PUT without `password` keeps it, and the ciphertext is accepted back.
  Removing the authentication needs JSON `null` for `user` and `password`. An empty
  string is stored as an encrypted empty password, and the store then fails to load.
  `POST …/{datastores|coveragestores}/{s}/reset` makes GeoServer re-read a store;
  cascaded stores have no reset (404). **A disabled store still answers its reads.**
  With `enabled` false, a coverage store's `coverages.json?list=all` answers 200 with
  the same list as before (measured). So does a WMS store's
  `wmslayers.json?list=available`. So the reachability check after a save that disables a store is a real
  check, not a false alarm. **An empty connection parameter** is written
  `{"@key": "Session startup SQL"}`, with no `$`, and the library reads it as `None`.
  Sent back as the text "None", GeoServer ran it as the startup SQL of every
  connection, and the store stopped loading. The form shows it blank, and a row left
  as it was prefilled goes back as stored, number or `None` alike. **A store's
  namespace** is the workspace's URI when the store is created without the parameter.
  The library's PostGIS, JNDI and PMTiles creates send `http://{workspace}` instead,
  so the plugin merges the workspace's URI on after them (row 64).
- **Editing a layer is one partial resource PUT** (row 53 of #1). Measured on 2.28.5:
  a `PUT …/featuretypes/{ft}.json` or `…/coverages/{c}.json` with only some of title,
  abstract, keywords, srs, projectionPolicy, enabled, advertised, cqlFilter and name
  merges. It keeps the rest (bounds, attributes, grid, bands). An empty title,
  abstract, keyword list or filter clears it. The library's `FeatureType` drops
  `cqlFilter`, and drops `title` when an `internationalTitle` is set. So the edit form
  reads the feature type with a raw GET (row 63). A rename carries the layer groups
  that use the layer and its GWC layer along (the native name stays). `enabled` is
  the resource's: `/rest/layers` ignores it. `?recalculate=nativebbox,latlonbbox`
  recomputes both boxes, and `POST …/{ft|c}/reset` makes GeoServer re-read the
  source. The other allowed styles are the layer's `styles`. The library's
  `update_layer` sends them: a workspace style as `ws:style`, and an empty list
  clears. A cascaded WMS layer takes **no default style**. A layer PUT with a
  `defaultStyle` answers 200 and keeps `{"name": ""}`, while its `styles` are stored
  and listed in the capabilities. A cascaded WMTS layer takes one like any layer.
  **Cascaded WMS layers cannot be edited over REST**: any PUT on `…/wmslayers/{l}`,
  JSON, XML or the document a GET returned, fails with
  `UnsupportedOperationException`.
- **Publishing a table: send no bounding box** (row 52 of #1). The facade's
  `create_feature_type(epsg=…)` fills both boxes from a table of 3 EPSG codes. So it
  raises `KeyError` for any other code, and gives those three a world extent. A POST
  without either box makes GeoServer compute both from the data (measured on 2.28.5
  with an EPSG:25832 PostGIS table). So the plugin posts the library's `FeatureType`
  model without `epsg_code`.
- **An emptied datastore description has to be sent as `""`.** The library leaves a
  `None` field out of the payload. A PUT without `description` keeps the old one.
  Measured on 2.28.5: "old text" survived a PUT with `description=None`, and `""`
  cleared it. The edit sends the form's value when the user changed it, empty
  included, and leaves an untouched one out, so GeoServer keeps its own.
- **An edit meets the server as it is now** (measured on 2.28.5). A datastore PUT
  replaces the whole `connectionParameters` map, and a tile cache XML PUT the whole
  document. So a form's snapshot sent back reverted what another client saved while
  the form was open. Both also create what is gone. `create_datastore()` POSTs when
  its GET is a 404. That brought a store deleted meanwhile back empty (its feature
  types and layers stayed 404). The XML PUT cached a layer again. A workspace's
  service settings merge a partial PUT. But a PUT onto removed ones recreates them
  from the fields it carries alone (`maxRenderingTime` 0, no abstract). A
  workspace rename moves them to the new name. So each edit form reads the resource
  again at Save, applies only the fields the user changed, and refuses one that is
  gone.
- **A datastore without a type** (#1, measured): GeoServer writes no `type` for a
  store saved without one. That is 4 of the 5 demo stores on 2.27, where 2.28.5's demo data
  added it. It is also one POSTed without it on 2.28.5. Such a store works (`list=available`
  names its shapefiles). But `DataStore.from_get_response_payload()` reads the type
  without a default, so `get_datastore()` raises `KeyError('type')`. `_get_datastore`
  reads such a store raw and gives it the type `None`. A PUT with `"type": null`
  keeps it without one, and the edit form opens it in the parameter editor.
- **Cascaded WFS datastores** (row 41 of #1): type `Web Feature Server (NG)`, every
  parameter prefixed `WFSDataStoreFactory:` (`GET_CAPABILITIES_URL`, `USERNAME`,
  `PASSWORD`, `TIMEOUT`, `MAXFEATURES`, `LENIENT`); GeoServer adds `namespace` itself.
  A PUT without a key drops it (the map is replaced, invariant 3).
  `featuretypes.json?list=available` lists the remote's feature types, so
  *Publish a Layer → a table in a datastore* cascades them. The typed creators stop
  before it; the form goes through the generic `create_datastore`.
- **Add to QGIS as WFS**: no `srsname`. Without it, QGIS takes the type's own CRS from
  the capabilities, and the features arrive native. Measured on 2.28.5 / QGIS 3.40:
  `sf:archsites`, EPSG:26713, easting first. With `srsname=EPSG:4326`, GeoServer
  reprojected every feature, and QGIS reprojected them again to the canvas. The
  embedded preview's WMS layer likewise reads its extent from the capabilities, in the
  URI's EPSG:4326. So no resource GET is needed for its bounds, nor a group GET for a
  layer group's. Spearfish, stored in EPSG:26713, comes back as its lon/lat box.
- **Add to QGIS as WMTS**: the URI names the tile matrix set (`EPSG:900913`) and *no*
  `crs=`. With `crs=EPSG:4326` beside it, QGIS accepted the layer, reported it as 4326
  and reprojected every tile on the fly (measured against the sandbox). Without it,
  the layer takes the tile matrix's own CRS.
- **Add to QGIS goes through the workspace's own service** (measured on 2.28.5 /
  QGIS 3.44). An isolated workspace's layers are in no global capabilities. On
  `{base}/ows` and `{base}/gwc/service/wmts`, its WMS and WMTS layers were invalid
  ("Cannot calculate extent", "Tile layer or tile matrix set not found"). Its WFS
  layer was invalid too. `{base}/{ws}/ows` and `{base}/{ws}/gwc/service/wmts` gave valid ones.
  There, WMS and WMTS take the **bare** name: the qualified one is invalid the same
  way, for a normal workspace too. WFS takes either, so the URI keeps the qualified
  type name. `sf:archsites`, `topp:states` and `nurc:mosaic` are valid through these
  URIs, and so are the `ne:world` group and the global `tasmania` group (on the global
  service). The embedded preview and the layer groups use the same URIs. The layer
  tree reads the workspace back from the URL path to know which server layer such a
  QGIS layer is.
- **Add to QGIS behind a proxy.** Measured with a proxy that forwards
  `Host: inside.invalid:8080`, as nginx's default `proxy_set_header Host $proxy_host`
  does. Also measured on QGIS 3.44 with a proxy that forwards another port and logs
  what reaches it. GeoServer writes every OnlineResource of its capabilities from its Proxy base
  URL or, without one, from the Host it receives. The WMS and WMTS layers were still
  valid, since the capabilities came from the right URL. But every GetMap, GetTile and
  GetFeatureInfo went to the inside address, with the saved user name and password,
  and drew nothing when it was unreachable. With `IgnoreGetMapUrl=1` and
  `IgnoreGetFeatureInfoUrl=1`, which the provider accepts for both, they went to the
  URL the plugin gave, the legend too. A WMTS layer needs the second one as well: its
  identify uses GeoWebCache's RESTful FeatureInfo template, which went to the
  advertised address without it. The WFS provider has no such option. It sent
  DescribeFeatureType and every GetFeature, with the user name and password, to the
  advertised address. It was invalid with an empty error when that address was
  unreachable. So *Add to QGIS* reads the workspace's WFS capabilities first, through
  the library's `ows_service.get_wfs_capabilities()`. It refuses a WFS layer whose
  operation URLs name another scheme, host or port than the plugin's URL, before QGIS
  sends a request.
- **An invalid layer's reason** (measured on QGIS 3.44): `layer.error().message()` is
  HTML. For a failed request it only says "Provider is not valid" with the URI.
  The WMS provider keeps the cause in `dataProvider().lastError()` ("Download of
  capabilities failed: Connection refused"). It keeps a failed check in
  `dataProvider().error().summary()` ("Cannot calculate extent", "Tile layer or tile
  matrix set not found"). The WFS provider only writes its reason to QGIS's log, on
  the WFS tab, so `dlg_preview.load_error()` says so when the provider gives nothing.
- **A pushed style is confirmed before it replaces one.** `create_style_definition()`
  upserts, and a style is shared by every layer that references it. So
  `_push_qgis_style` checks `get_style_definition()` first and asks. It returns False
  when the user keeps the existing style. Its callers (the Layers row action, the
  publish, the layer-tree menu) then say "left as it is" instead of claiming an
  upload.
  The Styles tab, by contrast, refuses an existing name; there a new name is the
  point.
- **One publish entry point for both kinds.** *Publish a Layer → A layer from this
  QGIS project* offers vectors and rasters. A `QgsRasterLayer` is handed to
  `_publish_qgis_raster(values, layer=…)` on the shared dialog class. So the Coverage
  Stores tab's Add form and the Layers tab's publish are the same path. Every upload
  goes through `_upload_file`. Every create path calls `_require_safe_name()` on a
  typed name before its first request.
- **Publishing a QGIS layer** (rows 28–29 of #1) uploads a GeoPackage:
  `PUT .../datastores/{name}/file.gpkg?update=overwrite`. GeoServer then creates the
  store *and* configures one feature type per table in the file. The SRS, the
  bounding box and the attributes are read from the data. So the layer is published by
  that one request, and the table name inside the GeoPackage is the layer's name.
  Three things follow from that. Metadata is added with a **partial** feature-type
  PUT, which merges (a `create_feature_type()` template would replace the computed
  values). The store is marked `read_only` by merging onto its own parameters, the
  recommended setting for a file store nobody writes to. That is best-effort, because
  the data is already published by then, and a flag must not fail the publish. And
  deleting the store later **leaves the uploaded file** in the data directory. A
  *Replace* (the same PUT onto the existing store) makes GeoServer re-read the table.
  A column added to the GeoPackage is served at once, by REST and by
  DescribeFeatureType (measured on 2.28.5). No reset has to follow it. A QGIS layer
  name must pass `toolbelt/qgis_export.geoserver_name()` first: it becomes a WFS type
  name, so it has to be an XML NCName.
- **Publishing a QGIS raster** (rows 31–32 of #1) uploads a GeoTIFF:
  `PUT .../coveragestores/{name}/file.geotiff?configure=first&coverageName={name}`
  with `Content-Type: image/tiff`. Measured on 2.28.5: GeoServer saves the body as
  `data/{ws}/{store}/{store}.geotiff` and creates a GeoTIFF store. It configures one
  coverage named by `coverageName` (the store's name without it), published as a
  layer with the SRS and bounds read from the file. A second PUT to the same store
  **replaces the file and re-reads the coverage** (no `update` parameter needed), so
  *Replace* is the same request again. A partial coverage PUT merges (`title`,
  `abstract`, `keywords`). Deleting the store, or even its workspace, leaves the file
  in the data directory. On the QGIS side, `QgsRasterFileWriter` honours
  `COMPRESS=DEFLATE` and `TILED=YES`. Given the layer's CRS, it writes a CRS override
  into the file *without* reprojecting the pixels, which is what an override means.
  So `export_to_geotiff` passes `layer.crs()`. A raster that already is a plain local
  GeoTIFF (no subdataset, no `/vsicurl/`, no override) is uploaded as it is.
  GeoServer's `description` on a configured coverage is its own "Generated from
  <file>" note; the abstract is `abstract`, and the viewer prefers that.
  **A cancelled upload** (measured with the body aborted at 1.5 of 18 MB): a first
  upload leaves *nothing* (no store, no coverage, no file). A *Replace* keeps the
  store, the coverage and the layer configured, while GeoServer has already deleted
  the previous file. That is a layer with no data behind it. So the upload streams
  through `_run_upload`. Its `on_cancel` (`_store_upload_cancelled`, then
  `_report_cancelled_upload`) GETs the store afterwards and says which of the two
  happened. Closing the dialog lets an upload finish instead of stopping it. A
  *Replace* whose PUT fails before it completes (a reset, a proxy, a timeout) drops
  the connection mid-body as the cancel does (not measured apart). So
  `_store_upload_ended` warns the same way when the store is still there, and logs
  it when the upload ends after the dialog closed. **CRS**: GeoServer declares an SRS
  by EPSG code. So `qgis_export.require_crs()` refuses a layer without a CRS, and
  `reprojection_target()` names EPSG:4326 for a CRS without an EPSG code. A vector is
  reprojected on export (`export_to_geopackage(target_crs=…)`). A raster is refused,
  because it is uploaded as it is.
- **SLD versions decide the content type** (row 27 of #1). GeoServer picks its SLD
  parser from the request's content type, not from the document:
  `application/vnd.ogc.sld+xml` for 1.0, `application/vnd.ogc.se+xml` for 1.1.
  `rest_service.create_style()` only sends the former. So `toolbelt/sld.py` sniffs
  the version (`StyledLayerDescriptor/@version`, else the `se:` namespace), and
  `_put_sld_body()` raw-PUTs the 1.1 case. Every SLD write in the plugin goes through
  it. The facts behind that: `QgsMapLayer.saveSldStyle()` writes **SLD 1.1** for a
  vector layer on QGIS 3.40, even for a single-symbol renderer. It writes **SLD 1.0**
  (a `UserLayer`) for a raster layer (measured on 3.44 for the pseudocolor, gray,
  hillshade, paletted and multiband renderers). A 1.1 body sent as 1.0 is accepted
  and rendered, but recorded as `languageVersion 1.0.0`. And `GET {style}.sld`
  returns GeoServer's **1.0 rendition** of a stored 1.1 document, so the editor shows
  converted text and says so. Exporting or applying a style touches a live QGIS
  layer, so it happens on the GUI thread before any upload (invariant 9).
- **What a QGIS export carries, and what QGIS reads back** (measured on 2.28.5 and
  QGIS 3.44). An SVG or image marker is written as its file's path on this machine
  (`/usr/share/qgis/svg/gpsicons/plane.svg?fill=…`), with a fallback relative to
  QGIS's SVG folders (`gpsicons/plane.svg`). GeoServer looks for such a path in its
  own data directory, logs `can't parse … as a java resource` and draws the fallback
  square. So an SLD that names files of this machine goes as a zip with them
  (`sld.icon_package`). Each file is renamed `{style}_{file}`, since a workspace's
  styles share one folder. Only an SLD made on this machine (a push, the upload form) is
  packaged. A body the server supplied (Copy, the edit form's Save) could name any
  file here, which would be read and uploaded unasked. Hence `local_icons=True` on
  `_create_style` and `_put_sld_body`. GeoServer unpacks only `svg`, `png`, `jpg`,
  `bmp` and `gif` files from a style zip (`validImageFileExtensions` in gs-restconfig,
  the extension lower-cased). A `.jpeg` icon was answered 201 and was not on the
  server (404), so it goes as `.jpg`. Any other image is refused before a request.
  A zip `POST ?name=` keeps an SLD 1.1 document byte for byte, recorded as 1.1.0, and
  draws the icons. A zip `PUT` unpacks the icons and writes the SLD into the style's
  file as it is, but keeps the style's recorded format and version. A CSS style then
  held SLD in its `.css` file, and GetMap answered a CSSParseException. An SLD
  1.0.0 style stayed 1.0.0. So a replace sends the zip's SLD again as
  `_put_sld_body`'s plain PUT, which records both. A font marker is
  `ttf://DejaVu Sans`, which GeoServer's SE parser refuses with a 500
  (URISyntaxException). `ttf://DejaVu%20Sans` draws the letter, and QGIS reads it
  back as the family. What QGIS cannot write, 3.44 refuses whole with its reason: an
  expression label, even `"name"`; a heatmap; a raster fill. QGIS 3.40 writes a "… not
  implemented yet" comment and reports success (read in its source, not run).
  `layer_to_sld` refuses both. The other way, QGIS reads no SLD into a raster layer
  ("Layer type 1 not supported"). It keeps a relative href relative and draws a "?",
  and it fetches an http one. GeoServer serves a style's folder without a login at
  `{base}/styles/{file}` and `{base}/styles/{ws}/{file}`. Its 1.0 rendition names
  those files `file:{data dir}/workspaces/{ws}/styles/{file}`. So *Apply* reads an
  SLD 1.1 style as stored and makes its icons such URLs. QGIS also reads a `<Size>`
  given as `<ogc:Literal>` as 0, which draws nothing (the demo style `burg`). So
  `apply_sld_to_layer` hands such a Size over as its number.
- **Per-workspace services and the namespace URI** (row 61, measured on 2.28.5):
  `/rest/services/{wfs|wcs|wmts}/workspaces/{ws}/settings.json` answers 404 without
  own settings. A `PUT` creates them or merges into them. `DELETE` falls back to the
  global ones, as for WMS. A freshly created WFS override has `maxFeatures` 0, not
  the global value, so the form prefills from the global settings. A `PUT` of
  `{"namespace": {"uri": …}}` alone does **not** keep `isolated`: it stores false, on
  the namespace and the workspace. So the plugin sends `{"uri": …, "isolated": …}`
  from the form. A workspace rename keeps the URI. A URI another workspace uses is a
  500 "Namespace with URI … already exists", unless the PUT carries
  `"isolated": true` (200, the URI shared).
- **Server-wide settings** (row 60, measured on 2.28.5):
  `…/services/{wms|wfs|wcs|wmts}/settings.json` merges a partial `PUT`, like the
  resources. But `/settings.json` (global), `/settings/contact.json` and
  `/logging.json` **replace** the stored object. A `PUT` of `proxyBaseUrl` alone
  wiped the contact and the charset. One of `contactPerson` alone cleared the city.
  One of `level` alone turned standard-output logging off. So `tab_server.py` reads
  them again, merges the form, and sends them whole. A `null` `proxyBaseUrl` unsets
  it (`""` stores an empty one). The log is `GET /rest/resource/{location}`: served
  whole, gzip, no length, no Range, so it is streamed and only its end kept. A file
  that is not there is a 404 "Undefined resource path." (2.27 and 2.28.5). GeoServer
  Cloud writes no log file: each service logs to its standard output. There the log
  GET is that 404 on its pgconfig backend. On its datadir backend it is a 0-byte
  file (measured on Cloud 2.28.5.1). The dialog says so for both. Nothing tells
  beforehand without reading the file. `?operation=metadata` is a 500 on 2.27. The
  library's `get_resource_directory()` gets an HTML page on 2.28.5 and 3.0: the API
  reads `format=json`, not the `Accept` header it sends. `POST /rest/reload` and
  `/rest/reset` answer 200 at once on the sandbox. The pages of GeoServer's web
  interface are `/web/wicket/bookmarkable/{class}` (a wrong class is a 404).
- **Non-administrator accounts** (measured on 2.28.5 with restricted accounts). REST
  lists and GETs only what the account administers. A resource it cannot see is a
  404 ("No such workspace"), not a 403. `workspaces/default.json` is a 404 when the
  default workspace is one of those. A workspace administrator can still
  `POST /rest/workspaces` (201), and the new workspace is hidden from it, so
  *Add a Workspace* reads it back and says so. Global styles and layer groups are
  readable. Writing one is a 405 `Cannot edit global resource , full admin credentials required`.
  GeoWebCache's REST applies no catalog filter: it lists every cached layer, and a
  workspace administrator's DELETE of another workspace's cached layer answered 200.
  `/rest/security/acl/catalog.json` is a 403 "Administrative privileges required".
  For whoever sets up test accounts: `rest.properties` is first-match in file order.
  Rules POSTed through REST are appended after `/**`, so they can never narrow it.
  `DELETE /rest/security/acl/rest/{rule}` cannot address a rule that starts with `/`:
  a 404 with the slash stripped, a 400 for `%2F`, and Spring's firewall rejects
  `%25`. A rule change through REST took effect only much later, and a deleted
  account's cached login still passed for about 7 minutes.
- **Library models that lose data** (row 62, measured on 2.28.5):
  `rest_service.get_layer()` keeps a single other style as the bare
  `{"name", "href"}` object GeoServer writes. `Layer.asdict()` then reads its keys as
  two styles named "name" and "href" (11 demo layers). `get_wms_store()`'s model drops
  `user`, `password`, `maxConnections`, `readTimeout` and `connectTimeout`. Both are
  read raw. So is a cascaded WMS layer (row 65). GeoServer sends one with an
  international title as `internationalTitle` alone, no `title`, and `WmsLayer` reads
  the plain title only, so `get_wms_layer()` returned none. A style is created in
  **one** `POST` to the collection, with `?name=` and the body's content type.
  Creating the definition first left an empty style behind when the body was refused
  (a retry then "already exists"). A GeoPackage upload whose name matches a layer in
  another store is published as `name1`. A Replace upload onto a store of another
  type makes GeoServer import into that store (a PostGIS database). So
  `_refuse_layer_clash` checks both first.
- **Seeding** (row 59, measured on 2.28.5): `POST /gwc/rest/seed/{layer}.json` with a
  `seedRequest` answers 200 and starts `threadCount` tasks. The request holds name,
  gridSetId, format, type seed/reseed/truncate, zoomStart/Stop and threadCount, with
  an optional `bounds.coords.double` and `parameters.entry[].string[key, value]`. An
  unknown gridset is a 500 naming it; a zoom beyond the published range is accepted.
  `GET` of the same path lists the tasks as
  `[tiles done, tiles total, seconds left, task id, state]`. The state is -1 aborted,
  0 pending, 1 running, 2 done; a count not made yet is -1. A form `POST` of
  `kill_all=all` to `/seed/{layer}` stops them and answers GWC's HTML seed page. A
  gridSubset's `zoomStart`/`zoomStop` (published levels, either one alone too),
  `min`/`maxCachedLevel`, and `parameterFilters` of any kind survive an XML `PUT`. A
  misspelt filter element is a bare 500 naming it.
- **Other style formats, rename and usage** (row 58, measured on 2.28.5): CSS, YSLD
  and MBStyle each need their extension. Without it, GeoServer answers 500 "No such
  style handler". Their bodies read and write at `{style}.css` / `.ysld` /
  `.mbstyle` with their own content types. A new one is created by a `POST` to the
  collection with `?name=` (a `PUT` is refused, 400). `GET {style}.sld` returns any
  format **converted to SLD**, which is how *Apply to a QGIS layer* reads a CSS or
  YSLD style. A bad body gets a 400 whose Tomcat page carries the parser's reason in
  its "Message" line, which `summarise_body` keeps. A well-formed but meaningless SLD
  is accepted, even with `validate=true`. A `PUT` of `name` renames a style, and the
  layers and groups that use it follow. They link by id; a layer names a workspace
  style `ws:name`. There is no endpoint that lists a style's users, so *Used by*
  reads every layer and group. **An SLD body is read as UTF-8** whatever its XML
  declaration names. An ISO-8859-1 SLD 1.0 `POST`ed or `PUT` without a `charset` is
  stored with replacement characters (GeoServer re-serialises it as UTF-8). An SLD
  1.1 one is stored byte for byte, but read, rendered and served as `.sld` with them.
  With `; charset=ISO-8859-1`, or sent as UTF-8, the accents are kept.
  `toolbelt/sld.utf8_sld()` sends every SLD as UTF-8 under a declaration rewritten to
  say so. A **`.zip`** (an SLD and its images) is created by the same `POST ?name=`
  with `application/zip`. The images land beside the style, and the SLD inside is
  renamed after `name`. A zip without an SLD is a 403 "No sld file provided" that
  leaves nothing (the library's `create_style_from_file()` creates the definition
  first). A relative `OnlineResource` href is resolved against the style's own
  folder. A workspace's zip style drew its icon in the legend, and its SLD posted as a
  global style drew none. That is why *Copy* names those files. A
  `DELETE ?purge=true` does not remove the style's file: it renames it `<file>.bak`
  (then `.bak.1`, and so on) in the data directory. A save of a style deleted
  meanwhile fails where a GET says 404. A body is a 400 "Invalid style: … info is
  null", a rename a 500 NullPointerException, a zip a 500 "Error processing the
  style". So a failed save reads the style again before it reports.
- **Workspace WMS settings** (rows 25–26 of #1): `WmsSettings` models none of the
  service metadata (`title`, `abstrct`, `keywords`, `srs`, …), and there is no delete.
  So `tab_workspaces.py` GETs, PUTs and DELETEs the settings path itself. The
  GeoServer facts behind that code: the abstract's JSON key is **`abstrct`**. A
  partial PUT **merges**. Sending only the form's fields is what keeps the
  watermark and the metadata links intact; a full template would overwrite them.
  The settings are **created with PUT** (POST answers 405) and removed with DELETE.
  `defaultLocale` must be `""` when empty: `null` makes GeoServer's
  `LocaleConverter` throw an NPE (500). The library's
  `unset_default_locale_for_service()` silently does nothing at all.
- **Coverages** (rows 21–24 of #1): there is no `get_coverage_stores(ws)` at all.
  `get_coverages` hardcodes `list=all`, so "what is published" needs its own call
  (`list=configured`; `list=available` answers the same). The two do not compare as
  they are. `list=all` answers the store's native names, `list=configured` the
  published ones. `sfdem` published as `elev` keeps `nativeName` `sfdem` (an uploaded
  raster also writes it as `nativeCoverageName`, and a rename keeps both). GeoServer
  accepts a second publish of the same native coverage (201, a duplicate layer). So
  the *Publish* action offers `list=all` minus each published coverage's
  `nativeCoverageName`, else its `nativeName`, read with `get_coverage()`.
  `CoverageStore` drops the store's description, and its `put_payload()` raises
  `NotImplementedError` (no store edit anywhere). `Coverage.asdict()` drops the
  bounding boxes and keywords. Two GeoServer facts the tab depends on: a grid range's
  `high` is the **exclusive** bound (size = high − low, checked against gdalinfo).
  Store metadata GeoServer does not understand (`CogSettings.Key` without the COG
  extension) is dropped silently, so the create warns when it comes back missing.
- **Cascaded WMS / WMTS stores** (rows 33–38 of #1): the library creates, gets and
  deletes a WMS store and its layers. It creates and deletes a WMTS store. But it
  **lists nothing**: no store listing per workspace, no cascaded-layer listing
  (`get_wms_layers()` is this GeoServer's own capabilities), no WMTS getter or layer
  delete. So `tab_cascaded.py` GETs the collections itself. The GeoServer facts
  behind it, measured on 2.28.5: the collections are `wmsStores.wmsStore` and
  `wmtsStores.wmtsStore` (WMTS layers live under `.../wmtsstores/{s}/layers`, not
  `wmtslayers`). `?list=available` on a layer collection answers
  `{"list": {"string": [...]}}` with the remote's own layer names, a single entry
  written as a bare string. A POST of only `name` + `nativeName` publishes a cascaded
  layer, and GeoServer fills title, abstract, SRS and bounds from the capabilities.
  That is why the WMTS publish does not use `create_wmts_layer()`. It fetches the
  remote capabilities from the *plugin's* machine, forces EPSG:4326 and deletes an
  existing layer first. A cascaded layer DELETE needs `recurse=true`, or GeoServer
  answers 403 "wms layer referenced by layer(s)". A store DELETE with `recurse=true`
  takes its layers along. Cascaded layers also appear in the Layers tab (it reads
  `/rest/layers`), which reaches this tab's detail and delete helpers for them. The
  tab's own *Cascaded layers* dialog is a viewer; deleting is the Layers tab's action.
  Names go into the library's path builders **pre-quoted** (`_q`,
  `quote(name, safe="")`): `RestEndpoints` interpolates them raw, and `requests`
  sends `stores/a#b.json` as `stores/a`. `tab_styles.py` (`_style_path`) and
  `tab_gwc.py` (`_gwc_layer_path`, `safe=":"` for `ws:layer`) do the same, all to
  drop once the library quotes.
- **Tile cache (GeoWebCache)** (rows 42–47 of #1): GeoServer caches every layer and
  layer group by itself. So `GET /gwc/rest/layers.json` (a bare JSON array of names,
  `ws:name`, a global group bare) lists about everything published.
  *Add a Layer to the Cache* only ever offers what was removed. GWC's REST is
  XML-first, and on 2.28.5 its **JSON writes are broken**. A PUT of the very document
  a GET returned fails with "Duplicate field mimeFormats" (any array) or
  "defaultValue" (the STYLES parameter filter loses its class). The one JSON shape it
  accepts is the library's `publish_gwc_layer()` template. That comes back as a
  degraded configuration: no formats, 0×0 meta-tiles, one gridset, no STYLES filter.
  So `tab_gwc.py` reads JSON and writes XML. `GET .xml` → `PUT .xml` round-trips byte
  for byte (200 "layer saved"). A new layer's document is the one GeoServer
  writes itself, the id left to the server. Truncate is `POST /gwc/rest/masstruncate`
  with `<truncateLayer><layerName>…` sent as **`text/xml`** (200, empty body).
  `application/xml` there is a 400 "Format extension unknown", while the layer PUTs
  take `application/xml`. The seed endpoint wants one request per gridset × format.
  `DELETE /gwc/rest/layers/{name}.json` drops the tiles and the configuration and
  leaves the layer published. `get_gwc_layer()` / `delete_gwc_layer()` take a
  workspace and a layer, so a global layer group (cached under its bare name) goes
  raw, and `GwcEndpoints.layers(ws)` ignores its argument. A GET of a layer GWC does
  not cache is a 404 "Unknown layer" on 2.28.5. On 2.27 (GWC 1.27) and 3.0
  (GWC 2.0) it is a 500. So is one of a gridset that does not exist. `get_gwc_layer()` raises on
  the 500, so whether a layer is cached is read from `layers.json`. Gridsets: the
  list is a JSON array of names. A JSON PUT fails the same way ("Duplicate field
  coords"). An XML PUT creates one (201), and DELETE removes it. Deleting a gridset
  in use answers 500 with an empty body.
- **Layer-group modes** are shown as GeoServer's web interface names them: Single,
  Opaque Container, Named Tree, Container Tree, Earth Observation Tree
  (`tab_layergroups._mode_label`). They are mapped back to the enum for the payload. `MODES`
  still holds the enum, which `test_tab_layergroups` checks against the library's
  model.
- **Layer groups** are the biggest library gap so far (rows 16–19 of #1). Every
  layer-group call requires a `workspace_name`, so the *global* groups are
  unreachable. `create_layer_group` re-qualifies every layer with the group's own
  workspace (no cross-workspace and no nested group). It always sends a world bbox
  from a three-entry `EPSG_BBOX` table (GeoServer computes the real union when
  `bounds` is omitted). It writes the abstract as `abstract`, which GeoServer drops
  (its key is `abstractTxt`, which the model also fails to read). Hence
  `tab_layergroups.py` builds its own payload and GETs the group itself. It still
  uses the facade for the per-workspace listing and delete.
- **Editing a layer group** (row 57, measured on 2.28.5): a partial `PUT` merges. On
  a group deleted meanwhile it is a 500 NullPointerException, and a `DELETE` a 500
  with no body, where a GET says 404. So a failed save or delete reads the group
  again before it reports. A new `publishables` list needs a `styles` list of the
  same length (`""` for a layer's default), or it is refused. A group that holds a
  nested group needs `styles` even on a create (HTTP 500 without). GeoServer **never
  recomputes the bounds on a PUT**: a new layer list keeps the old box, and
  `"bounds": null` stores a zero one. So the plugin sends the union of the members'
  lon/lat boxes (`_group_bounds`), and of an Earth Observation group's root layer.
  It does so whenever the layers or that root change. GeoServer's own box for a new EO group
  holds the root layer too. A name GeoServer does not know is **dropped with a
  200**, so every line is checked first. A rename is forbidden (403). An EO group
  needs `rootLayer` and `rootLayerStyle`, and cannot leave EO mode: JSON null, `""`,
  `{}` and an empty XML element are all refused. A workspace group holds that
  workspace's layers, groups and styles. It holds a global group only when
  everything in it is in that workspace too. GeoServer follows its nested groups,
  their styles and an EO root layer. A global group of the workspace's own layers is
  created, 201, and stored bare. One that holds `topp:states`, even 2 levels down,
  or an EO root of `topp`, is a 500. The message is "Layer group within a workspace
  (ws) can not contain resources from other workspace: topp". A style of another workspace is a
  500 that "can not contain styles from other workspace". A layer of another
  workspace in the group itself, or as its EO root, gets the same 500. The form
  refuses those layers, and such a global group (`_foreign_member`, one GET per
  global group reached). In a workspace group, a bare `layerGroup` name is the global
  group, even when the workspace has a group of that name (`ws:name`). A group may
  share a layer's qualified name.
- The bundled wheel is the upstream 0.8.5 with `geoserver_acceptance_tests/` removed
  (15 MB of fixtures): 16 MB → 49 KB. On a version bump, strip the new wheel the same
  way. The procedure is in `toolbelt/dependencies.py`; see
  [packaging and release](packaging.md). `GSC_REQUIRED` pins the version.
  `ensure_dependencies()` logs which copy was imported and from where, and pushes a
  warning when it is not the pin. An install in the QGIS profile still wins over the
  bundled wheel; the warning is how you notice. `tests/qgis/test_library_contract.py`
  asserts that the pin equals the shipped wheel.
- To read the library source, unzip the wheel into a scratch folder. The plugin only
  uses `geoservercloud/geoservercloud.py`, `services/restclient.py`,
  `services/restservice.py`, `models/datastore.py`, `models/workspace.py`,
  `models/featuretype.py` (a table publish) and `models/layer.py` (a layer's styles).
