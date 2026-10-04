# GeoServer and library notes

Everything here was measured, not assumed: against GeoServer **2.28.5** in the
[docker sandbox](environment.md), and against the
[python-geoservercloud](https://github.com/camptocamp/python-geoservercloud)
version bundled in `geoserver_manager/extras/`. Use these notes when changing server calls.

Two rules frame all of it. Every GeoServer call goes through the library, and
each gap in the library is recorded as a row in
[issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1) before
it is worked around here, so it can be fixed upstream. A workaround carries a
`TODO(#1)` comment at the call site.

## What the library does, and does not

- Every REST verb calls `raise_for_status()` **except** GET/DELETE on 404 and POST on 409. Those three come
  back as `(content, status)`, which is exactly why `_check` exists. `requests` exceptions all subclass
  `OSError`, so catch `HTTPError` *before* `OSError` (see `toolbelt/probe.py`).
- `create_workspace` and `create_datastore` **upsert**. There is no `update_*`, no `delete_datastore`, no
  workspace rename, no "set default workspace" call (the `set_default_workspace=True` kwarg only sets a
  client-side attribute). Those are `_raw_rest` workarounds carrying `TODO(#1)`, each with a row in
  [issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1); the library-first rule in
  Conventions says how new ones are handled.
- **GeoServer always has exactly one default workspace and it cannot be unset.** `GET
  /rest/workspaces/default.json` never 404s (with `default.xml` deleted it answers the first workspace),
  and the web UI's "Default Workspace" checkbox only *sets*: `WorkspaceEditPage` has
  `if (defaultWs) setDefaultWorkspace(ws)` with no else, so unchecking it and saving is a no-op. The
  plugin therefore shows the default read-only-and-checked, marks it in the Workspaces list, and reads it
  live from the server on every load and every edit dialog.
- The client strips trailing `/` from the URL itself. It has no timeout parameter at all
  (`TIMEOUT = 120` is a module constant and `RestClient.get` takes no `timeout`), which is why
  `toolbelt/probe.py` uses `requests` directly: a dead host must cost 10 s, not two minutes (row 20 of
  #1). The Server tab's log view does too (`_log_tail`, row 60): it streams the file, keeps only its end
  and stops between two chunks on Cancel, where the library's client reads a body whole. `verifytls` is the *Verify the server's TLS certificate* setting (default on);
  it catches `requests.exceptions.SSLError` before `OSError` so a private-CA server is reported as a
  certificate problem, not as "is the server running?".
- **Layers of every type** (rows 39, 48 and 49 of #1): `GET /rest/layers.json` is the one list where vector, raster and cascaded layers
  all appear. Walking datastores then feature types, as the Layers tab did, misses the others.
  `GET /rest/layers/{ws}:{name}.json` gives `type` (VECTOR / RASTER / WMS / WMTS), `defaultStyle`
  (`{"name": ""}` for a cascaded WMS layer) and `resource` with `@class` (featureType / coverage /
  wmsLayer / wmtsLayer) and an `href`, except a **wmtsLayer, which has no href** on 2.28.5, so its
  store is found by asking the workspace's WMTS stores for their layers. The href carries GeoServer's
  own idea of its base URL (behind a proxy, an inside name), so the tab parses the store segment out
  of it and never follows it. `rest_service.get_layer()` exists, but its `Layer` model keeps only the
  resource's name. Per type, the detail view, the browser preview and the delete go to the resource:
  feature type through the library, coverage through the Coverage Stores tab's `_coverage_detail`
  and a raw `DELETE …/coverages/{name}?recurse=true` (no `delete_coverage()` upstream), cascaded
  layer through the Cascaded Stores tab's helpers. All of them are reached on the shared dialog class.
  *Add to QGIS* offers WFS for VECTOR only.
- **Legend and browser preview** (rows 39–40 of #1): `get_legend_graphic()` is a plain GET through the REST client
  (stateless, so worker-safe), but it returns the raw `Response`: an OGC exception is **HTTP 200 with an
  XML body**, so the content type decides, and it runs with the client's 120 s timeout. GetLegendGraphic
  needs a `LAYER` even for a stored style, and **of the style's kind** (measured on 2.28.5): a
  raster style drawn with a vector layer is a blank 20x20 image, a vector style drawn with a raster
  layer a ServiceException "we need a RasterSymbolizer". So `tab_styles.py` takes, for an SLD with a
  `RasterSymbolizer`, the first coverage of `/rest/workspaces/{ws}/coverages.json`, else the first
  feature type of `…/featuretypes.json` (a workspace's resources across its stores, names **bare**,
  which it qualifies; the library lists them per store only), the style's own workspace first, and
  explains in the `image` field when there is none. The legend lands through `_run_quietly` (its own
  task slot, so it neither supersedes a load nor turns Refresh into Cancel) into a modal dialog that may already be closed; the
  landing checks `finished` and `sip.isdeleted` first. The Styles table's Format and Version columns
  cost one definition GET per style, fanned out: whether a style is SLD decides what *Apply to a QGIS
  layer* can do with it. *Preview in a
  browser* is GeoServer's own OpenLayers GetMap page, built by the pure `_preview_url` from
  `latLonBoundingBox` (a group: its `bounds`) with a world fallback; the browser's session is not the
  plugin's, so a secured server asks it to log in, which the tooltip says.
- **Embedded preview** (`gui/dlg_preview.py`): a `QgsMapCanvas` with a WMS `QgsRasterLayer` from
  `_layer_uri` (never added to the project) and the provider's own `identify()` for GetFeatureInfo:
  `IdentifyText` is what the WMS provider offers and GeoServer answers as `text/plain`; a file raster
  offers `IdentifyValue`, which is how the dialog is tested without a server. The WMS provider needs the
  canvas extent and size to turn the point into a pixel. One map tool does both: a drag pans, a release
  within 3 px of the press identifies. `WA_DeleteOnClose` plus `stopRendering()` in `closeEvent` make
  closing mid-render safe, and the window is non-modal so the main dialog's tasks carry on. Every
  `QgsMapCanvas` connects itself to the project's `readProject` and `writeProject`: opening a
  project moved the preview to that project's CRS and extent, where identify missed, and a save
  wrote a nameless `<mapcanvas>` into the .qgs. PyQt cannot disconnect a connection QGIS made in
  C++ ("disconnect() failed", measured), so the dialog connects after the canvas and undoes both.
- **Thread safety:** the REST methods are stateless `requests.*` calls and are safe to run through
  `_fan_out` (the datastore list does this). `self.wms` / `self.wmts` on the client are shared state.
  OWS calls must not be fanned out the same way.
- **File-based datastores** (row 30 of #1): the library's typed creators stop at PostGIS, JNDI and PMTiles,
  so Shapefile, *Directory of spatial files (shapefiles)* and GeoPackage forms build their parameter map and
  go through the generic `create_datastore`. Two things that map has to get right: a GeoPackage store must
  carry **`dbtype: geopkg`** (that is how GeoServer picks the factory), and an empty `charset` is omitted
  rather than sent blank, because blank is not "use your default". GeoServer fills in `namespace` itself, and
  the edit merge keeps it along with everything else the form does not show.
- **Editing a store is one partial PUT too** (rows 54 and 55 of #1). Measured on 2.28.5: a
  `PUT …/coveragestores/{cs}.json`, `…/wmsstores/{s}.json` or `…/wmtsstores/{s}.json` with only the changed
  fields merges. A coverage store rename keeps its coverages and layers; a datastore rename (one PUT with
  the new `name` on the old path) keeps its feature types, layers, groups and GWC layers. A cascaded store
  **cannot** be renamed (403). Cascaded store passwords come back `crypt1:…`; a PUT without `password` keeps
  it, the ciphertext is accepted back, and removing authentication needs JSON `null` for `user` and
  `password`: an empty string is stored as an encrypted empty password, and the store then fails to load.
  `POST …/{datastores|coveragestores}/{s}/reset` makes GeoServer re-read a store; cascaded stores have no
  reset (404). **A disabled store still answers its reads**: with `enabled` false, a coverage store's
  `coverages.json?list=all` and a WMS store's `wmslayers.json?list=available` both answer 200 with the
  same lists as before (measured), so the reachability check after a save that disables a store is a
  real check, not a false alarm. **An empty connection parameter** is written `{"@key": "Session startup SQL"}`, with no
  `$`, and the library reads it as `None`. Sent back as the text "None", GeoServer ran it as the startup
  SQL of every connection and the store stopped loading. The form shows it blank, and a row left as it
  was prefilled goes back as stored, number or `None` alike. **A store's namespace** is the workspace's URI when
  it is created without the parameter; the library's PostGIS, JNDI and PMTiles creates send
  `http://{workspace}` instead, so the plugin merges the workspace's URI on after them (row 64).
- **Editing a layer is one partial resource PUT** (row 53 of #1). Measured on 2.28.5: a
  `PUT …/featuretypes/{ft}.json` or `…/coverages/{c}.json` with only some of title, abstract, keywords,
  srs, projectionPolicy, enabled, advertised, cqlFilter and name merges and keeps the rest (bounds,
  attributes, grid, bands). An empty title, abstract, keyword list or filter clears it. The library's
  `FeatureType` drops `cqlFilter`, and `title` when an `internationalTitle` is set, so the edit form reads
  the feature type with a raw GET (row 63). A rename carries
  the layer groups that use the layer and its GWC layer along (the native name stays). `enabled` is the
  resource's: `/rest/layers` ignores it. `?recalculate=nativebbox,latlonbbox` recomputes both boxes, and
  `POST …/{ft|c}/reset` makes GeoServer re-read the source. The other allowed styles are the layer's
  `styles` (the library's `update_layer` sends them; a workspace style as `ws:style`, an empty list
  clears). A cascaded WMS layer takes **no default style**: a layer PUT with a `defaultStyle` answers
  200 and keeps `{"name": ""}`, while its `styles` are stored and listed in the capabilities; a
  cascaded WMTS layer takes one like any layer. **Cascaded WMS layers cannot be edited over REST**:
  any PUT on `…/wmslayers/{l}`, JSON, XML or the document a GET returned, fails with
  `UnsupportedOperationException`.
- **Publishing a table: send no bounding box** (row 52 of #1). The facade's `create_feature_type(epsg=…)`
  fills both boxes from a table of three EPSG codes, so it raises `KeyError` for any other code and gives
  those three a world extent. A POST without either box makes GeoServer compute both from the data
  (measured on 2.28.5 with an EPSG:25832 PostGIS table), so the plugin posts the library's `FeatureType`
  model without `epsg_code`.
- **An emptied datastore description has to be sent as `""`.** The library leaves a `None` field out of the
  payload, and a PUT without `description` keeps the old one (measured on 2.28.5: "old text" survived a PUT
  with `description=None`, and `""` cleared it). The edit sends the form's value when the user changed it,
  empty included, and leaves an untouched one out, so GeoServer keeps its own.
- **An edit meets the server as it is now** (measured on 2.28.5). A datastore PUT replaces the whole
  `connectionParameters` map and a tile cache XML PUT the whole document, so a form's snapshot sent back
  reverted what another client saved while it was open. Both also create what is gone:
  `create_datastore()` POSTs when its GET is a 404, which brought a store deleted meanwhile back empty
  (its feature types and layers stayed 404), and the XML PUT cached a layer again. A workspace's service
  settings merge a partial PUT, but a PUT onto removed ones recreates them from the fields it carries
  alone (`maxRenderingTime` 0, no abstract), and a workspace rename moves them to the new name. So each
  edit form reads the resource again at Save, applies only the fields the user changed, and refuses one
  that is gone.
- **A datastore without a type** (#1, measured): GeoServer writes no `type` for a store saved without
  one: 4 of the 5 demo stores on 2.27 (2.28.5's demo data added it), or one POSTed without it on 2.28.5.
  Such a store works (`list=available` names its shapefiles), but `DataStore.from_get_response_payload()`
  reads the type without a default, so `get_datastore()` raises `KeyError('type')`. `_get_datastore`
  reads such a store raw and gives it the type `None`; a PUT with `"type": null` keeps it without one,
  and the edit form opens it in the parameter editor.
- **Cascaded WFS datastores** (row 41 of #1): type `Web Feature Server (NG)`, every parameter prefixed
  `WFSDataStoreFactory:` (`GET_CAPABILITIES_URL`, `USERNAME`, `PASSWORD`, `TIMEOUT`, `MAXFEATURES`, `LENIENT`);
  GeoServer adds `namespace` itself. A PUT without a key drops it (the map is replaced, invariant 3), and
  `featuretypes.json?list=available` lists the remote's feature types, so *Publish a Layer → a table in a
  datastore* cascades them. The typed creators stop before it; the form goes through the generic `create_datastore`.
- **Add to QGIS as WFS**: no `srsname`. Without it QGIS takes the type's own CRS from the
  capabilities and the features arrive native (`sf:archsites`, EPSG:26713, easting first; measured
  on 2.28.5 / QGIS 3.40); with `srsname=EPSG:4326` GeoServer reprojected every feature and QGIS
  reprojected them again to the canvas. The embedded preview's WMS layer likewise reads its extent
  from the capabilities, in the URI's EPSG:4326, so no resource GET is needed for its bounds, nor a
  group GET for a layer group's (spearfish, stored in EPSG:26713, comes back as its lon/lat box).
- **Add to QGIS as WMTS**: the URI names the tile matrix set (`EPSG:900913`) and *no* `crs=`. With
  `crs=EPSG:4326` beside it QGIS accepted the layer, reported it as 4326 and reprojected every tile on the
  fly (measured against the sandbox); without it the layer takes the tile matrix's own CRS.
- **Add to QGIS goes through the workspace's own service** (measured on 2.28.5 / QGIS 3.44): an
  isolated workspace's layers are in no global capabilities, so on `{base}/ows` and
  `{base}/gwc/service/wmts` its WMS and WMTS layers were invalid ("Cannot calculate extent", "Tile
  layer or tile matrix set not found") and its WFS layer too, while `{base}/{ws}/ows` and
  `{base}/{ws}/gwc/service/wmts` gave valid ones. There WMS and WMTS take the **bare** name (the
  qualified one is invalid the same way, for a normal workspace too) and WFS takes either, so the URI
  keeps the qualified type name. `sf:archsites`, `topp:states` and `nurc:mosaic`, the `ne:world`
  group and the global `tasmania` group (on the global service) are all valid through these URIs, and
  the embedded preview and the layer groups use the same ones. The layer tree reads the workspace back
  from the URL path to know which server layer such a QGIS layer is.
- **Add to QGIS behind a proxy** (measured with a proxy that forwards `Host: inside.invalid:8080`,
  as nginx's default `proxy_set_header Host $proxy_host` does, and on QGIS 3.44 with one that
  forwards another port and logs what reaches it): GeoServer writes every OnlineResource of its
  capabilities from its Proxy base URL or, without one, from the Host it receives. The WMS and
  WMTS layers were still valid, since the capabilities came from the right URL, but every GetMap,
  GetTile and GetFeatureInfo went to the inside address, with the saved credentials, and drew
  nothing when it was unreachable. With `IgnoreGetMapUrl=1` and `IgnoreGetFeatureInfoUrl=1`, which
  the provider accepts for both, they went to the URL the plugin gave, the legend too. A WMTS
  layer needs the second one as well: its identify uses GeoWebCache's RESTful FeatureInfo
  template, which went to the advertised address without it. The WFS provider has no such option:
  it sent DescribeFeatureType and every GetFeature, with the credentials, to the advertised
  address, and was invalid with an empty error when that one was unreachable. So *Add to QGIS*
  reads the workspace's WFS capabilities first, through the library's
  `ows_service.get_wfs_capabilities()`, and refuses a WFS layer whose operation URLs name another
  scheme, host or port than the plugin's URL, before QGIS sends a request.
- **An invalid layer's reason** (measured on QGIS 3.44): `layer.error().message()` is HTML, and for
  a failed request it only says "Provider is not valid" with the URI. The WMS provider keeps the
  cause in `dataProvider().lastError()` ("Download of capabilities failed: Connection refused") and
  a failed check in `dataProvider().error().summary()` ("Cannot calculate extent", "Tile layer or
  tile matrix set not found"). The WFS provider only writes its reason to QGIS's log, on the WFS
  tab, so `dlg_preview.load_error()` says so when the provider gives nothing.
- **A pushed style is confirmed before it replaces one.** `create_style_definition()` upserts and a style is
  shared by every layer that references it, so `_push_qgis_style` checks `get_style_definition()` first and
  asks; it returns False when the user keeps the existing style, and its callers (the Layers row action, the
  publish, the layer-tree menu) say "left as it is" rather than claiming an upload. The Styles tab, by
  contrast, refuses an existing name; there a new name is the point.
- **One publish entry point for both kinds.** *Publish a Layer → A layer from this QGIS project* offers
  vectors and rasters; a `QgsRasterLayer` is handed to `_publish_qgis_raster(values, layer=…)` on the shared
  dialog class, so the Coverage Stores tab's Add form and the Layers tab's publish are the same path.
  Every upload goes through `_upload_file`; every create path calls `_require_safe_name()` on a typed name
  before its first request.
- **Publishing a QGIS layer** (rows 28–29 of #1) uploads a GeoPackage: `PUT
  .../datastores/{name}/file.gpkg?update=overwrite`. GeoServer then creates the store *and* configures one
  feature type per table in the file, with the SRS, bounding box and attributes read from the data, so the
  layer is published by that one request, and the table name inside the GeoPackage is the layer's name. Three
  things follow from that: metadata is added with a **partial** feature-type PUT, which merges (a
  `create_feature_type()` template would replace the computed values); the store is marked `read_only` by
  merging onto its own parameters (the recommended setting for a file store nobody writes to), best-effort,
  because the data is already published by then and a flag must not fail the publish; and deleting the store later **leaves the
  uploaded file** in the data directory. A *Replace* (the same PUT onto the existing store) makes
  GeoServer re-read the table: a column added to the GeoPackage is served at once, by REST and by
  DescribeFeatureType (measured on 2.28.5), so no reset has to follow it. A QGIS layer name must pass
  `toolbelt/qgis_export.geoserver_name()` first: it becomes a WFS type name, so it has to be an XML NCName.
- **Publishing a QGIS raster** (rows 31–32 of #1) uploads a GeoTIFF: `PUT
  .../coveragestores/{name}/file.geotiff?configure=first&coverageName={name}` with `Content-Type: image/tiff`.
  Measured on 2.28.5: GeoServer saves the body as `data/{ws}/{store}/{store}.geotiff`, creates a GeoTIFF store
  and configures one coverage named by `coverageName` (the store's name without it), published as a layer with
  the SRS and bounds read from the file; a second PUT to the same store **replaces the file and re-reads the
  coverage** (no `update` parameter needed), so *Replace* is the same request again; a partial coverage PUT
  merges (`title`, `abstract`, `keywords`); and deleting the store, or even its workspace, leaves the file in
  the data directory. On the QGIS side, `QgsRasterFileWriter` honours `COMPRESS=DEFLATE`/`TILED=YES` and, given
  the layer's CRS, writes a CRS override into the file *without* reprojecting the pixels (which is what an
  override means), so `export_to_geotiff` passes `layer.crs()`; a raster that already is a plain local GeoTIFF
  (no subdataset, no `/vsicurl/`, no override) is uploaded as it is. GeoServer's `description` on a configured
  coverage is its own "Generated from <file>" note; the abstract is `abstract`, and the viewer prefers that.
  **A cancelled upload** (measured with the body aborted at 1.5 of 18 MB): a first upload leaves *nothing*
  (no store, no coverage, no file), but a *Replace* keeps the store, coverage and layer configured while
  GeoServer has already deleted the previous file, i.e. a layer with no data behind it. So the upload streams
  through `_run_upload`, and its `on_cancel` (`_store_upload_cancelled`, then `_report_cancelled_upload`) GETs the store afterwards and says which of the two
  happened, and closing the dialog lets an upload finish rather than stopping it. A *Replace* whose PUT fails
  before it completes (a reset, a proxy, a timeout) drops the connection mid-body as the cancel does (not
  measured apart), so `_store_upload_ended` warns the same way when the store is still there, and logs it
  when the upload ends after the dialog closed. **CRS**: GeoServer declares an
  SRS by EPSG code, so `qgis_export.require_crs()` refuses a layer without a CRS and `reprojection_target()`
  names EPSG:4326 for a CRS without an EPSG code. A vector is reprojected on export
  (`export_to_geopackage(target_crs=…)`), a raster is refused because it is uploaded as it is.
- **SLD versions decide the content type** (row 27 of #1). GeoServer picks its SLD parser from the request's
  content type, not from the document: `application/vnd.ogc.sld+xml` for 1.0, `application/vnd.ogc.se+xml` for
  1.1. `rest_service.create_style()` only sends the former, so `toolbelt/sld.py` sniffs the version
  (`StyledLayerDescriptor/@version`, else the `se:` namespace) and `_put_sld_body()` raw-PUTs the 1.1 case.
  Every SLD write in the plugin goes through it. Facts behind that: `QgsMapLayer.saveSldStyle()` writes
  **SLD 1.1** for a vector layer on QGIS 3.40, even for a single-symbol renderer, and **SLD 1.0** (a
  `UserLayer`) for a raster layer (measured on 3.44 for the pseudocolor, gray, hillshade, paletted and
  multiband renderers); a 1.1 body sent as 1.0 is accepted and rendered but recorded as `languageVersion
  1.0.0`; and `GET {style}.sld` returns GeoServer's **1.0 rendition** of a stored 1.1 document, so the editor
  shows converted text and says so. Exporting or applying a style touches a live QGIS layer, so it happens on
  the GUI thread before any upload (invariant 9).
- **What a QGIS export carries, and what QGIS reads back** (measured on 2.28.5 and QGIS 3.44). An SVG or image
  marker is written as its file's path on this machine (`/usr/share/qgis/svg/gpsicons/plane.svg?fill=…`), with
  a fallback relative to QGIS's SVG folders (`gpsicons/plane.svg`). GeoServer looks for such a path in its own
  data directory, logs "can't parse … as a java resource" and draws the fallback square. So an SLD naming files
  of this machine goes as a zip with them (`sld.icon_package`, each file renamed `{style}_{file}`, since a
  workspace's styles share one folder). Only an SLD made on this machine (a push, the upload form) is packaged:
  a body the server supplied (Copy, the edit form's Save) could name any file here, which would be read and
  uploaded unasked (`local_icons=True` on `_create_style` and `_put_sld_body`). GeoServer unpacks only `svg`, `png`, `jpg`, `bmp` and `gif` files from a
  style zip (`validImageFileExtensions` in gs-restconfig, the extension lower-cased): a `.jpeg` icon was answered
  201 and was not on the server (404), so it goes as `.jpg`, and any other image is refused before a request. A
  zip `POST ?name=` keeps an SLD 1.1 document byte for byte, recorded as 1.1.0, and draws the icons. A zip `PUT`
  unpacks the icons and writes the SLD into the style's file as it is, but keeps the style's recorded format and
  version: a CSS style then held SLD in its `.css` file and GetMap answered a CSSParseException, and an SLD
  1.0.0 style stayed 1.0.0. So a replace sends the zip's SLD again as `_put_sld_body`'s plain PUT, which records
  both. A font marker is `ttf://DejaVu Sans`, which GeoServer's SE parser refuses with a 500
  (URISyntaxException); `ttf://DejaVu%20Sans` draws the letter, and QGIS reads it back as the family. What QGIS
  cannot write, 3.44 refuses whole with its reason (an expression label, even
  `"name"`; a heatmap; a raster fill), where 3.40 writes a "… not implemented yet" comment and reports success
  (read in its source, not run); `layer_to_sld` refuses both. The other way, QGIS reads no SLD into a raster
  layer ("Layer type 1 not supported"), keeps a relative href relative and draws a "?", and fetches an http
  one. GeoServer serves a style's folder without a login at `{base}/styles/{file}` and
  `{base}/styles/{ws}/{file}`, and its 1.0 rendition names those files
  `file:{data dir}/workspaces/{ws}/styles/{file}`, so *Apply* reads an SLD 1.1 style as stored and makes its
  icons such URLs. QGIS also reads a `<Size>` given as `<ogc:Literal>` as 0, which draws nothing (the demo
  style `burg`), so `apply_sld_to_layer` hands such a Size over as its number.
- **Per-workspace services and the namespace URI** (row 61, measured on 2.28.5):
  `/rest/services/{wfs|wcs|wmts}/workspaces/{ws}/settings.json` answers 404 without own settings; a `PUT`
  creates them or merges into them; `DELETE` falls back to the global ones, as for WMS. A freshly created WFS
  override has `maxFeatures` 0, not the global value, so the form prefills from the global settings. A `PUT` of
  `{"namespace": {"uri": …}}` alone does **not** keep `isolated`: it stores false, on the namespace and the
  workspace, so the plugin sends `{"uri": …, "isolated": …}` from the form. A workspace rename keeps the URI.
  A URI another workspace uses is a 500 "Namespace with URI … already exists", unless the PUT carries
  `"isolated": true` (200, the URI shared).
- **Server-wide settings** (row 60, measured on 2.28.5): `…/services/{wms|wfs|wcs|wmts}/settings.json`
  merges a partial `PUT`, like the resources. But `/settings.json` (global), `/settings/contact.json` and
  `/logging.json` **replace** the stored object: a `PUT` of `proxyBaseUrl` alone wiped the contact and the
  charset, one of `contactPerson` alone cleared the city, one of `level` alone turned standard-output logging
  off. So `tab_server.py` reads them again, merges the form, and sends them whole. A `null` `proxyBaseUrl` unsets
  it (`""` stores an empty one). The log is `GET /rest/resource/{location}`: served whole, gzip, no length, no
  Range, so it is streamed and only its end kept. A file that is not there is a 404 "Undefined resource
  path." (2.27 and 2.28.5). GeoServer Cloud writes no log file: each service logs to its standard output,
  and the log GET is that 404 on its pgconfig backend and a 0-byte file on its datadir one (measured on
  Cloud 2.28.5.1 in the review of 2026-09-29), so the dialog says so for both. Nothing tells beforehand
  without reading the file: `?operation=metadata` is a 500 on 2.27, and the library's
  `get_resource_directory()` gets an HTML page on 2.28.5 and 3.0 (the API reads `format=json`, not the
  `Accept` header it sends). `POST /rest/reload` and `/rest/reset` answer 200 at once on
  the sandbox. The web admin pages are `/web/wicket/bookmarkable/{class}` (a wrong class is a 404).
- **Non-administrator accounts** (measured on 2.28.5 with restricted accounts). REST lists and GETs only what the account administers, and a resource it cannot see is a
  404 ("No such workspace"), not a 403; `workspaces/default.json` is a 404 when the default workspace is
  one of those. A workspace administrator can still `POST /rest/workspaces`
  (201), and the new workspace is hidden from it, so *Add a Workspace* reads it back and says so. Global
  styles and layer groups are readable, and writing one is a 405 "Cannot edit global resource , full
  admin credentials required". GeoWebCache's REST applies no catalog filter: it lists every cached layer,
  and a workspace administrator's DELETE of another workspace's cached layer answered 200.
  `/rest/security/acl/catalog.json` is a 403 "Administrative privileges required". For whoever sets up
  test accounts: `rest.properties` is first-match in file order, and rules POSTed through REST are
  appended after `/**`, so they can never narrow it. `DELETE /rest/security/acl/rest/{rule}` cannot
  address a rule that starts with `/` (a 404 with the slash stripped, a 400 for `%2F`, and Spring's
  firewall rejects `%25`). A rule change through REST took effect only much later, and a deleted
  account's cached login still passed for about 7 minutes.
- **Library models that lose data** (row 62, measured on 2.28.5): `rest_service.get_layer()` keeps a single
  other style as the bare `{"name", "href"}` object GeoServer writes, and `Layer.asdict()` reads its keys as two
  styles named "name" and "href" (11 demo layers); `get_wms_store()`'s model drops `user`, `password`,
  `maxConnections`, `readTimeout` and `connectTimeout`. Both are read raw. So is a cascaded WMS layer (row 65):
  GeoServer sends one with an international title as `internationalTitle` alone, no `title`, and
  `WmsLayer` reads the plain title only, so `get_wms_layer()` returned none. A style is created in **one** `POST`
  to the collection with `?name=` and the body's content type: creating the definition first left an empty style
  behind when the body was refused (a retry then "already exists"). A GeoPackage upload whose name matches a
  layer in another store is published as `name1`, and a Replace upload onto a store of another type makes
  GeoServer import into that store (a PostGIS database), so `_refuse_layer_clash` checks both first.
- **Seeding** (row 59, measured on 2.28.5): `POST /gwc/rest/seed/{layer}.json` with a `seedRequest` (name,
  gridSetId, format, type seed/reseed/truncate, zoomStart/Stop, threadCount, optional `bounds.coords.double`
  and `parameters.entry[].string[key, value]`) answers 200 and starts `threadCount` tasks; an unknown gridset is
  a 500 naming it, a zoom beyond the published range is accepted. `GET` of the same path lists the tasks as
  `[tiles done, tiles total, seconds left, task id, state]` (state -1 aborted, 0 pending, 1 running, 2 done; -1
  for a count not made yet). A form `POST` of `kill_all=all` to `/seed/{layer}` stops them and answers GWC's HTML
  seed page. A gridSubset's `zoomStart`/`zoomStop` (published levels, either one alone too) and
  `min`/`maxCachedLevel`, and `parameterFilters` of any kind, survive an XML `PUT`; a misspelt filter element
  is a bare 500 naming it.
- **Other style formats, rename and usage** (row 58, measured on 2.28.5): CSS, YSLD and MBStyle each need their
  extension; without it GeoServer answers 500 "No such style handler". Their bodies read and write at
  `{style}.css` / `.ysld` / `.mbstyle` with their own content types, and a new one is created by a `POST` to the
  collection with `?name=` (a `PUT` is refused, 400). `GET {style}.sld` returns any format **converted to SLD**,
  which is how *Apply to a QGIS layer* reads a CSS or YSLD style. A bad body gets a 400 whose Tomcat page
  carries the parser's reason in its "Message" line, which `summarise_body` keeps; a well-formed but
  meaningless SLD is accepted, even with `validate=true`. A `PUT` of `name` renames a style, and the layers and
  groups using it follow (they link by id; a layer names a workspace style `ws:name`). There is no endpoint
  listing a style's users, so *Used by* reads every layer and group. **An SLD body is read as UTF-8**
  whatever its XML declaration names: an ISO-8859-1 SLD 1.0 `POST`ed or `PUT` without a `charset` is
  stored with replacement characters (GeoServer re-serialises it as UTF-8), and an SLD 1.1 one is
  stored byte for byte but read, rendered and served as `.sld` with them. With `; charset=ISO-8859-1`,
  or sent as UTF-8, the accents are kept; `toolbelt/sld.utf8_sld()` sends every SLD as UTF-8 under a
  declaration rewritten to say so. A **`.zip`** (an SLD and its images) is created by the same
  `POST ?name=` with `application/zip`: the images land beside the style, the SLD inside is renamed
  after `name`, and a zip without an SLD is a 403 "No sld file provided" that leaves nothing (the
  library's `create_style_from_file()` creates the definition first). A relative `OnlineResource` href
  is resolved against the style's own folder: a workspace's zip style drew its icon in the legend, and
  its SLD posted as a global style drew none, which is why *Copy* names those files. A
  `DELETE ?purge=true` does not remove the style's file: it renames it `<file>.bak` (then `.bak.1`,
  and so on) in the data directory. A save of a style deleted meanwhile is a 400 "Invalid style: … info is
  null" (a body), a 500 NullPointerException (a rename) or a 500 "Error processing the style" (a zip), where
  a GET says 404, so a failed save reads the style again before it reports.
- **Workspace WMS settings** (rows 25–26 of #1): `WmsSettings` models none of the service metadata
  (`title`, `abstrct`, `keywords`, `srs`, …) and there is no delete, so `tab_workspaces.py` GETs, PUTs and
  DELETEs the settings path itself. GeoServer facts behind that code: the abstract's JSON key is **`abstrct`**;
  a partial PUT **merges**, so sending only the form's fields is what keeps the watermark and metadata links
  intact (a full template would overwrite them); the settings are **created with PUT** (POST answers 405) and
  removed with DELETE; and `defaultLocale` must be `""` when empty: `null` makes GeoServer's `LocaleConverter`
  throw an NPE (500). The library's `unset_default_locale_for_service()` silently does nothing at all.
- **Coverages** (rows 21–24 of #1): there is no `get_coverage_stores(ws)` at all; `get_coverages` hardcodes
  `list=all`, so "what is published" needs its own call (`list=configured`; `list=available` answers the
  same). The two do not compare as they are: `list=all` answers the store's native names, `list=configured`
  the published ones, and `sfdem` published as `elev` keeps `nativeName` `sfdem` (an uploaded raster also
  writes it as `nativeCoverageName`, and a rename keeps both). GeoServer accepts a second publish of the same
  native coverage (201, a duplicate layer), so the *Publish* action offers `list=all` minus each published
  coverage's `nativeCoverageName`, else its `nativeName`, read with `get_coverage()`;
  `CoverageStore` drops the store's description and its `put_payload()` raises
  `NotImplementedError` (no store edit anywhere); `Coverage.asdict()` drops the bounding boxes and keywords.
  Two GeoServer facts the tab depends on: a grid range's `high` is the **exclusive** bound (size = high − low,
  checked against gdalinfo), and store metadata GeoServer does not understand (`CogSettings.Key` without the
  COG extension) is dropped silently, so the create warns when it comes back missing.
- **Cascaded WMS / WMTS stores** (rows 33–38 of #1): the library creates, gets and deletes a
  WMS store and its layers, and creates and deletes a WMTS store, but **lists nothing**: no store
  listing per workspace, no cascaded-layer listing (`get_wms_layers()` is this GeoServer's own
  capabilities), no WMTS getter or layer delete. So `tab_cascaded.py` GETs the collections itself.
  GeoServer facts behind it, measured on 2.28.5: the collections are `wmsStores.wmsStore` and
  `wmtsStores.wmtsStore` (WMTS layers live under `.../wmtsstores/{s}/layers`, not `wmtslayers`);
  `?list=available` on a layer collection answers `{"list": {"string": [...]}}` with the remote's own
  layer names, a single entry written as a bare string; a POST of just `name` + `nativeName` publishes
  a cascaded layer, GeoServer filling title, abstract, SRS and bounds from the capabilities. This
  is why the WMTS publish does not use `create_wmts_layer()` (it fetches the remote capabilities from
  the *plugin's* machine, forces EPSG:4326 and deletes an existing layer first); a cascaded layer
  DELETE needs `recurse=true` or GeoServer answers 403 "wms layer referenced by layer(s)"; a store
  DELETE with `recurse=true` takes its layers along. Cascaded layers also appear in the Layers tab (it
  reads `/rest/layers`), which reaches this tab's detail and delete helpers for them. The tab's own
  *Cascaded layers* dialog is a viewer, deleting is the Layers tab's action. Names go into the library's
  path builders **pre-quoted** (`_q`, `quote(name, safe="")`): `RestEndpoints` interpolates them raw and
  `requests` sends `stores/a#b.json` as `stores/a`; `tab_styles.py` (`_style_path`) and `tab_gwc.py`
  (`_gwc_layer_path`, `safe=":"` for `ws:layer`) do the same, all to drop once the library quotes.
- **Tile cache (GeoWebCache)** (rows 42–47 of #1): GeoServer caches every layer and layer group
  by itself, so `GET /gwc/rest/layers.json` (a bare JSON array of names, `ws:name`, a global group bare)
  lists about everything published, and *Add a Layer to the Cache* only ever offers what was removed.
  GWC's REST is XML-first, and on 2.28.5 its **JSON writes are broken**: a PUT of the very document a GET
  returned fails with "Duplicate field mimeFormats" (any array) or "defaultValue" (the STYLES parameter
  filter loses its class); the one JSON shape it accepts is the library's `publish_gwc_layer()` template,
  which comes back as a degraded configuration: no formats, 0×0 meta-tiles, one gridset, no STYLES
  filter. So `tab_gwc.py` reads JSON and writes XML: `GET .xml` → `PUT .xml` round-trips byte for byte
  (200 "layer saved"), and a new layer's document is the one GeoServer writes itself, the id left to the
  server. Truncate is `POST /gwc/rest/masstruncate` with `<truncateLayer><layerName>…` sent as **`text/xml`**
  (200, empty body); `application/xml` there is a 400 "Format extension unknown", while the layer PUTs
  take `application/xml`; the seed endpoint wants one request per gridset × format. `DELETE /gwc/rest/layers/{name}.json` drops
  the tiles and the configuration and leaves the layer published. `get_gwc_layer()` / `delete_gwc_layer()`
  take a workspace and a layer, so a global layer group (cached under its bare name) goes raw, and
  `GwcEndpoints.layers(ws)` ignores its argument. A GET of a layer GWC does not cache is a 404 "Unknown
  layer" on 2.28.5 but a 500 on 2.27 (GWC 1.27) and 3.0 (GWC 2.0), and so is one of a gridset that does
  not exist; `get_gwc_layer()` raises on the 500, so whether a layer is cached is read from
  `layers.json`. Gridsets: the list is a JSON array of names; a JSON PUT
  fails the same way ("Duplicate field coords"), an XML PUT creates one (201), DELETE removes it, and
  deleting a gridset in use answers 500 with an empty body.
- **Layer-group modes** are shown as GeoServer's web admin names them (`tab_layergroups._mode_label`: Single,
  Opaque Container, Named Tree, Container Tree, Earth Observation Tree) and mapped back to the enum for the
  payload; `MODES` still holds the enum, which `test_tab_layergroups` checks against the library's model.
- **Layer groups** are the biggest library gap so far (rows 16–19 of #1): every layer-group call requires a
  `workspace_name`, so the *global* groups are unreachable; `create_layer_group` re-qualifies every layer with
  the group's own workspace (no cross-workspace and no nested group), always sends a world bbox from a
  three-entry `EPSG_BBOX` table (GeoServer computes the real union when `bounds` is omitted), and writes the
  abstract as `abstract`, which GeoServer drops (its key is `abstractTxt`, which the model also fails to read).
  Hence `tab_layergroups.py` builds its own payload and GETs the group itself; it still uses the facade for the
  per-workspace listing and delete.
- **Editing a layer group** (row 57, measured on 2.28.5): a partial `PUT` merges; on a group deleted meanwhile
  it is a 500 NullPointerException, and a `DELETE` a 500 with no body, where a GET says 404, so a failed save
  or delete reads the group again before it reports. A new `publishables` list
  needs a `styles` list of the same length (`""` for a layer's default), or it is refused; a group holding a
  nested group needs `styles` even on a create (HTTP 500 without). GeoServer **never recomputes the bounds on a
  PUT**: a new layer list keeps the old box, and `"bounds": null` stores a zero one, so the plugin sends the
  union of the members' lon/lat boxes (`_group_bounds`), and of an Earth Observation group's root layer,
  which GeoServer's own box for a new EO group holds too, whenever the layers or that root change. A name GeoServer does not know is **dropped with a
  200**, so every line is checked first. A rename is forbidden (403). An EO group needs `rootLayer` and
  `rootLayerStyle`, and cannot leave EO mode: JSON null, `""`, `{}` and an empty XML element are all refused.
  A workspace group holds that workspace's layers, groups and styles, and a global group only when
  everything in it is in that workspace too: GeoServer follows its nested groups, their styles and an EO
  root layer. A global group of the workspace's own layers is created, 201, and stored bare; one holding
  `topp:states`, even two levels down, or an EO root of `topp`, is a 500 "Layer group within a workspace
  (ws) can not contain resources from other workspace: topp", and a style of another workspace one that
  "can not contain styles from other workspace". A layer of another workspace in the group itself, or
  as its EO root, gets the same 500. The form refuses those layers, and such a global group
  (`_foreign_member`, one GET per global group reached). In a workspace group a bare `layerGroup` name is the global group, even when the workspace has a
  group of that name (`ws:name`). A group may share a layer's qualified name.
- The bundled wheel is the upstream 0.8.5 with `geoserver_acceptance_tests/` removed (15 MB of fixtures):
  16 MB → 49 KB. On a version bump, strip the new wheel the same way. The procedure is in
  `toolbelt/dependencies.py`; see [packaging and release](packaging.md). `GSC_REQUIRED` pins the version;
  `ensure_dependencies()` logs which copy was imported and from where, and pushes a warning when it is not
  the pin. An install in the QGIS profile still wins over the bundled wheel, the warning is how you notice.
  `tests/qgis/test_library_contract.py` asserts the pin equals the shipped wheel.
- Extracted library source, when you need to read it: unzip the wheel into a scratch dir; the plugin
  only uses `geoservercloud/geoservercloud.py`, `services/restclient.py`, `services/restservice.py`,
  `models/datastore.py`, `models/workspace.py`, `models/featuretype.py` (a table publish) and
  `models/layer.py` (a layer's styles).
