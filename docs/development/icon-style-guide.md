# Icon style guide

`geoserver_manager/resources/icons/catalog.json` is the single inventory of
the plugin's icons. Reuse an existing ID when the meaning matches. The SVGs
live beside it. Qt's own checkboxes, message symbols and window controls are
outside this registry. The plugin's logo,
`resources/images/geoserver_manager.svg`, keeps its original proportions.

## Drawing rules

- Use a 24 × 24 canvas, **1.2-unit strokes**, round caps and round joins.
  The dialog shows icons at 20 px, which gives a one-pixel stroke.
- Keep path centres mostly within 2 to 22. Leave about two units between
  unrelated strokes. Use one recognisable object and one action marker.
- Keep brushes broad at the tip, and chain links large enough to balance
  folders and databases. Simplify crowded geometry and keep the strokes thin.
- Use an up arrow for sending to GeoServer and a down arrow for bringing
  content back. Keep the style-transfer pair's upright brush identical, and
  reverse only its separate, full-height arrow on the right.
- Use a plus for adding, a minus for stopping caching, and a bin for deleting
  a resource. The cache eraser has a flat contact edge and a short baseline.
- Use plain paths and shapes, with no backgrounds, fonts, raster images,
  gradients or shadows. Use `fill="none"` except for small meaningful dots.

Use these exact lowercase colour tokens. The shared renderer replaces them
with the widget's palette and the light or dark accents; the selected and
disabled states become monochrome. The shape must carry the meaning without
colour.

| Token | Role |
| :---- | :--- |
| `#172f36` | Neutral outline |
| `#0099c0` | Server, connection or transfer accent |
| `#589632` | Layer, map or styling accent |
| `#b3261e` | Destructive action |

Start from a related SVG. This is the shared structure:

```xml
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <g fill="none" stroke="#172f36" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
    <!-- Add the object's geometry and one action marker here. -->
  </g>
</svg>
```

## Register and use

Add a stable, meaningful ID to `catalog.json` with `label`, `category`,
`purpose`, `status: "custom"` and `asset: "icons/<id>.svg"`. If the artwork
is pending, omit `asset` and register the fallback at once:

```json
"new-feature": {
  "label": "New feature",
  "category": "Utilities",
  "purpose": "Describe what the action does.",
  "status": "needs-custom",
  "fallback": "mActionHelpContents.svg",
  "notes": "Describe the custom symbol still needed."
}
```

When the artwork is ready, replace `fallback` and `notes` with the SVG's
`asset` and set `status` to `custom`. Use the ID in `TABS`, `_row_actions` or
`icon("new-feature", button.palette())` from `gui/icons.py`. Do not call
`QIcon`, `getThemeIcon` or `iconPath` elsewhere. A menu-only action uses
`for_menu=True` for the highlighted colours; a shared toolbar action leaves it
off. Give an icon-only control a tooltip and an accessible name. A row button
shows a 20 px icon inside a target of at least 30 px. Use the shared
row-action builder for the spacing, the keyboard focus, the selection colours
and the labelled secondary actions. Do not make a destructive action a quick
button.

## Review

```sh
python scripts/build_icon_catalog.py --check  # validate, write nothing
python scripts/build_icon_catalog.py          # optional local gallery
```

Open `build/icon-catalog.html` to compare the 16, 20 and 24 px previews in
the light, dark, selected and disabled states. Git ignores the file, and you
can delete it at any time. Also inspect the real QGIS control at normal and
2× scaling. The unit tests check the registration, the fallbacks, missing
SVGs and the stroke consistency; no generated file needs to be committed.
When a visible icon changes, update the usage guide, the changelog and the
screenshot of the real dialog.
