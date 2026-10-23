# Security policy

## Supported versions

Only the [latest release](https://github.com/ronitjadhav/qgis-geoserver-manager/releases/latest)
gets security fixes. Update the plugin before you report.

## Report a vulnerability

Report it privately, never in a public issue:

- On GitHub: open the [*Security* tab](https://github.com/ronitjadhav/qgis-geoserver-manager/security),
  then *Report a vulnerability*.
- Or by email to `dev@ronit.io`.

Say what an attacker can do, and how to reproduce it. You get an answer
within a week. The fix and the date of its release are agreed with you, and
the release notes credit you unless you ask otherwise.

## Scope

The plugin's own code is in scope, and so are the libraries it bundles in
`geoserver_manager/extras/`. A vulnerability of GeoServer or QGIS goes to
[GeoServer](https://github.com/geoserver/geoserver/security) or to
[QGIS](https://github.com/qgis/QGIS/security).

By default, the plugin keeps the connection password as plain text in the
QGIS settings. That is a documented choice, not a vulnerability: a connection
can keep its password in QGIS's encrypted authentication database instead
([Good to know](https://geoserver-manager.ronit.io/usage/guide.html#good-to-know)).
