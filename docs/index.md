# GeoServer Manager

**GeoServer, inside QGIS.**

Manage a GeoServer through its REST API without leaving QGIS. Workspaces,
datastores, coverage stores, cascaded WMS and WMTS stores, layers, layer
groups, styles, the tile cache and the server-wide settings. Publish a table
or a layer of the open project. Bring what the server has back into QGIS as
WMS, WFS or WMTS.

```{image} static/screenshot-layers.png
:alt: The Layers tab, listing a server's layers with their workspace, type, store and default style
```

::::{grid} 1 2 2 4
:gutter: 3

:::{grid-item-card} {octicon}`download;1.5em` Install
:link: usage/installation
:link-type: doc

Install the plugin from the plugin manager of QGIS.
:::

:::{grid-item-card} {octicon}`rocket;1.5em` Quick start
:link: usage/quickstart
:link-type: doc

Connect to a server and publish your first layer in 5 minutes.
:::

:::{grid-item-card} {octicon}`book;1.5em` User guide
:link: usage/guide
:link-type: doc

Every tab, every button and every form, with screenshots.
:::

:::{grid-item-card} {octicon}`git-pull-request;1.5em` Contribute
:link: development/contribute
:link-type: doc

Set up an environment, run the tests, and find the work that is waiting.
:::

::::

## What it does

| Tab | In one line |
| :-- | :---------- |
| Workspaces | create, rename, isolate, set the default and the namespace URI, edit the WMS, WFS, WCS and WMTS settings |
| Datastores | PostGIS, shapefiles, GeoPackage, PMTiles and cascaded WFS, created and edited across every workspace |
| Coverage stores | GeoTIFF, COG and ImageMosaic, or a raster of the open project uploaded and published in one request |
| Cascaded stores | the WMS and WMTS stores that proxy another server, with the layers they advertise |
| Layers | every layer whatever its type, published, restyled, previewed and added back to QGIS |
| Layer groups | global and per workspace, built from ordered layers with their styles |
| Styles | pasted, uploaded or made from the symbology of a QGIS layer, edited beside the legend GeoServer renders |
| Tile cache | what GeoWebCache holds, its gridsets and formats, seeded, truncated or reconfigured |
| Server | contact details, global settings, the settings of each service, logging and the log, catalog reload and reset |

Every list loads in the background, and is searchable, sortable and paginated.
The user name and password can live encrypted in the QGIS authentication
database. Then they never reach a project file.
The plugin uses
[python-geoservercloud](https://github.com/camptocamp/python-geoservercloud),
with documented workarounds for the library operations it lacks.

:::{note}
Install the plugin from the plugin manager of QGIS. See
[installation](usage/installation.md).
:::

## At a glance

| | |
| :-- | :-- |
| Latest released version | {{ release_version }} |
| Development version | {{ version }} |
| QGIS | {{ qgis_version_min }} to 4.x, Qt5 and Qt6, Python 3.10 or newer |
| GeoServer | 2.28 and 3.0, tested with 2.28.5 and 3.0.1; [other versions](usage/guide.md#supported-geoserver-versions) |
| Author | {{ author }} |
| Source code | {{ repo_url }} |
| Licence | GPLv2+ |
| This page built | {{ date_update }} |

```{toctree}
---
caption: Use it
maxdepth: 1
hidden:
---
Installation <usage/installation>
Quick start <usage/quickstart>
User guide <usage/guide>
```

```{toctree}
---
caption: Develop it
maxdepth: 1
hidden:
---
Contributing <development/contribute>
Environment <development/environment>
Architecture <development/architecture>
Invariants <development/invariants>
Conventions <development/conventions>
GeoServer and library notes <development/geoserver-notes>
Testing <development/testing>
Translations <development/translation>
Documentation <development/documentation>
Packaging and release <development/packaging>
Changelog <development/history>
```

```{toctree}
---
caption: Project
maxdepth: 1
hidden:
---
Icon style guide <development/icon-style-guide>
Code of conduct <development/code_of_conduct>
```
