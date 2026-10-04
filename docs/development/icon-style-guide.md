# Icon style guide

`geoserver_manager/resources/icons/catalog.json` is the single inventory of
plugin icons. Reuse an existing ID when the meaning matches. SVGs live beside
it; Qt's own checkboxes, message symbols and window controls are outside this
registry. The [brand mark](../branding.md) keeps its original proportions.

## Drawing rules

- Use a 24 × 24 canvas, **1.2-unit strokes**, round caps and round joins.
  The dialog displays icons at 20 px, giving a one-pixel stroke.
- Keep path centres mostly within 2–22. Leave about two units between
  unrelated strokes. Use one recognisable object and one action marker.
- Keep brushes broad at the tip and chain links large enough to balance
  folders and databases. Simplify crowded geometry while keeping thin strokes.
- Use an up arrow for sending to GeoServer and a down arrow for bringing
  content back. Keep the style-transfer pair's upright brush identical and
  reverse only its separate, full-height arrow on the right.
- Use a plus for adding, a minus for stopping caching and a bin for deleting
  resources. The cache eraser has a flat contact edge and short baseline.
- Use plain paths and shapes, with no backgrounds, fonts, raster images,
  gradients or shadows. Use `fill="none"` except for small meaningful dots.

Use these exact lowercase colour tokens. The shared renderer replaces them
with the widget's palette and light/dark accents; selected and disabled states
become monochrome. Shape must carry the meaning without colour.

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
`purpose`, `status: "custom"` and `asset: "icons/<id>.svg"`.
If artwork is pending, omit `asset` and register the fallback immediately:

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

When ready, replace `fallback` and `notes` with the SVG's `asset` and set
`status` to `custom`. Use the ID in `TABS`, `_row_actions` or
`icon("new-feature", button.palette())` from `gui/icons.py`. Do not call
`QIcon`, `getThemeIcon` or `iconPath` elsewhere. Menu-only actions use
`for_menu=True` for highlighted colours; shared toolbar actions leave it off.
Give icon-only controls a tooltip and accessible name. Row buttons show 20 px
icons inside targets of at least 30 px. Use the shared row-action builder for
spacing, keyboard focus, selection colours and labelled secondary actions.
Do not make destructive actions quick buttons.

## Review

```sh
python scripts/build_icon_catalog.py --check  # validate, write nothing
python scripts/build_icon_catalog.py          # optional local gallery
```

Open `build/icon-catalog.html` to compare 16, 20 and 24 px previews in light,
dark, selected and disabled states. The file is ignored by Git and can be
deleted freely. Inspect the actual QGIS control at normal and 2× scaling too.
Unit tests check registration, fallbacks, missing SVGs and stroke consistency;
no generated files need to be committed. Update the usage guide, changelog and
real-dialog screenshot when a visible icon changes.
