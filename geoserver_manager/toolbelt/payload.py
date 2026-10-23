#! python3  # noqa: E265

"""
GeoServer's JSON payload shapes, tolerated in one place.

A collection comes back as `{"layers": {"layer": [...]}}`, but an empty one
is `{"layers": ""}`, a one-entry one may wrap a bare object, or, for the
`list=available` and GeoWebCache answers, a bare string, instead of a
one-item list. Every tab used to carry its own copy of these rules.

No QGIS import: the unit suite runs this on a plain Python.
"""


def unwrap(payload, list_key, item_key):
    """Entries of a collection payload, always as a list."""
    container = payload.get(list_key) if isinstance(payload, dict) else None
    if not isinstance(container, dict):
        return []
    items = container.get(item_key) or []
    if isinstance(items, (dict, str)):
        return [items]
    return list(items)


def as_list(value):
    """A GeoWebCache collection: "" when empty, bare when it has one entry."""
    if value in (None, ""):
        return []
    # Any scalar is one entry: a single SRS comes back as the number 4326.
    return list(value) if isinstance(value, (list, tuple)) else [value]


def keyword_list(value):
    """A resource's keywords as a list of strings.

    The library normalises them to a list, raw REST wraps them as
    {"string": [...]}, and a single keyword comes back bare: a live server
    showed all three.
    """
    if isinstance(value, dict):
        value = value.get("string")
    return [str(keyword) for keyword in as_list(value)]


def words(value):
    """A form's list field (keywords, an SRS list) as clean strings: each
    trimmed, blanks dropped. None or "" is no word at all."""
    return [str(item).strip() for item in (value or ()) if str(item).strip()]


def text_of(value):
    """A title or an abstract as one line: a string, or {language: text}."""
    if isinstance(value, dict):
        return "; ".join(f"{lang}: {text}" for lang, text in sorted(value.items()))
    return "" if value is None else str(value)


def name_of(item):
    """Name of a list entry: geoservercloud returns dicts, tolerate strings."""
    return item.get("name", str(item)) if isinstance(item, dict) else str(item)


def crs_text(value):
    """A CRS, which GeoServer gives either as a string or as {"$": …}."""
    if isinstance(value, dict):
        return str(value.get("$", ""))
    return "" if value is None else str(value)


def bbox_text(box):
    """One line for a bounding box, or "" when it is not one.

    `minx, miny → maxx, maxy  (crs)`, the same words on every tab.
    """
    if not isinstance(box, dict) or not {"minx", "miny", "maxx", "maxy"} <= set(box):
        return ""
    text = "{minx}, {miny} → {maxx}, {maxy}".format(
        **{key: box[key] for key in ("minx", "miny", "maxx", "maxy")}
    )
    crs = crs_text(box.get("crs"))
    return f"{text}  ({crs})" if crs else text


def changed(before, after, mapping):
    """{REST key: new value} for the form fields that differ, else {}.

    `mapping` is (form key, REST key) pairs. An edit sends only these, as a
    partial PUT that GeoServer merges, so nothing the form does not show is
    touched.
    """
    return {
        rest_key: after.get(key)
        for key, rest_key in mapping
        if after.get(key) != before.get(key)
    }


# -- SQL views ----------------------------------------------------------------
# A SQL view is a feature type whose metadata holds this entry.
VIRTUAL_TABLE = "JDBC_VIRTUAL_TABLE"
# What GeoServer's web interface fills in for a new parameter. A parameter
# saved without a validator is not checked at all, which lets a client inject
# SQL through `viewparams`.
DEFAULT_VALIDATOR = r"^[\w\d\s]+$"
# The form fields of a SQL view, beside the layer's own.
VIEW_FIELDS = ("sql", "geometry_column", "geometry_type", "key_columns", "parameters")
GEOMETRY_TYPES = (
    "Geometry",
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
    "GeometryCollection",
)


def metadata_entries(resource):
    """A resource's metadata entries, always as a list."""
    entries = ((resource or {}).get("metadata") or {}).get("entry") or []
    return [entries] if isinstance(entries, dict) else list(entries)


def virtual_table(resource):
    """The SQL view of a feature type, or None for a table."""
    for entry in metadata_entries(resource):
        if isinstance(entry, dict) and entry.get("@key") == VIRTUAL_TABLE:
            return entry.get("virtualTable") or {}
    return None


def view_form_values(view):
    """A SQL view as the form shows it. GeoServer adds a newline to the SQL
    and writes a numeric default as a number."""
    geometry = view.get("geometry") or {}
    return {
        "sql": str(view.get("sql") or "").strip(),
        "geometry_column": str(geometry.get("name") or ""),
        "geometry_type": str(geometry.get("type") or "Geometry"),
        "key_columns": [str(key) for key in as_list(view.get("keyColumn"))],
        "parameters": [
            [
                str(p.get("name") or ""),
                "" if p.get("defaultValue") is None else str(p["defaultValue"]),
                str(p.get("regexpValidator") or ""),
            ]
            for p in as_list(view.get("parameter"))
            if isinstance(p, dict)
        ],
    }


def view_of(name, values, srid, escape_sql=False):
    """The virtualTable GeoServer stores, from the form's values. A parameter
    without a validator gets DEFAULT_VALIDATOR."""
    view = {"name": name, "sql": values["sql"].strip(), "escapeSql": escape_sql}
    keys = [key.strip() for key in values.get("key_columns") or [] if key.strip()]
    if keys:
        view["keyColumn"] = keys if len(keys) > 1 else keys[0]
    column = (values.get("geometry_column") or "").strip()
    if column:
        view["geometry"] = {
            "name": column,
            "type": values.get("geometry_type") or "Geometry",
            "srid": srid,
        }
    parameters = [
        {
            "name": name.strip(),
            "defaultValue": default or "",
            "regexpValidator": validator or DEFAULT_VALIDATOR,
        }
        for name, default, validator in values.get("parameters") or []
        if name.strip()
    ]
    if parameters:
        view["parameter"] = parameters
    return view


def with_view(entries, view):
    """The metadata entries with the SQL view's replaced, the others kept: a
    metadata PUT replaces the whole map."""
    kept = [
        e
        for e in entries
        if not (isinstance(e, dict) and e.get("@key") == VIRTUAL_TABLE)
    ]
    return kept + [{"@key": VIRTUAL_TABLE, "virtualTable": view}]
