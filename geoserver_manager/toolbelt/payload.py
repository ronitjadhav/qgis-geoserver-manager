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


def as_int(value, default=0):
    """An integer as GeoServer wrote it, or the default when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


# -- Dimensions ---------------------------------------------------------------
# A layer's time and elevation, as metadata entries holding a dimensionInfo.
DIMENSIONS = ("time", "elevation")
# A time resolution is stored in milliseconds; the units the form offers.
TIME_UNITS = (
    ("weeks", 604_800_000),
    ("days", 86_400_000),
    ("hours", 3_600_000),
    ("minutes", 60_000),
    ("seconds", 1000),
    ("milliseconds", 1),
)
# The attribute bindings each dimension can use. Another one is stored and
# then breaks the layer (measured on 2.28.5), so the form offers only these.
_TIME_BINDINGS = ("Date", "Timestamp", "Time")
_ELEVATION_BINDINGS = ("Short", "Integer", "Long", "Float", "Double", "BigDecimal")


def attribute_bindings(resource):
    """A feature type's attributes as (name, binding's short name) pairs."""
    attributes = (resource or {}).get("attributes") or []
    if isinstance(attributes, dict):
        attributes = attributes.get("attribute") or []
    if isinstance(attributes, dict):
        attributes = [attributes]
    return [
        (str(a.get("name")), str(a.get("binding") or "").rsplit(".", 1)[-1])
        for a in attributes
        if isinstance(a, dict) and a.get("name")
    ]


def dimension_attributes(resource, dimension):
    """The attributes a dimension can use: dates for time, numbers for elevation."""
    kinds = _TIME_BINDINGS if dimension == "time" else _ELEVATION_BINDINGS
    return [name for name, binding in attribute_bindings(resource) if binding in kinds]


def dimension_form_values(resource):
    """Each dimension's settings as the form shows them, prefixed time_ or
    elevation_. A resolution is split into a number and, for time, a unit."""
    stored = {
        entry.get("@key"): entry.get("dimensionInfo") or {}
        for entry in metadata_entries(resource)
        if isinstance(entry, dict) and entry.get("@key") in DIMENSIONS
    }
    values = {}
    for dimension in DIMENSIONS:
        info = stored.get(dimension, {})
        default = info.get("defaultValue") or {}
        reference = default.get("referenceValue")
        values.update(
            {
                f"{dimension}_enabled": bool(info.get("enabled")),
                f"{dimension}_attribute": str(info.get("attribute") or ""),
                f"{dimension}_end_attribute": str(info.get("endAttribute") or ""),
                f"{dimension}_presentation": info.get("presentation") or "LIST",
                f"{dimension}_default": default.get("strategy") or "MINIMUM",
                f"{dimension}_reference": "" if reference is None else str(reference),
            }
        )
        resolution = info.get("resolution")
        if dimension == "time":
            count, unit = time_resolution(resolution)
            values.update(time_resolution=count, time_resolution_unit=unit)
        else:
            values.update(
                elevation_resolution="" if resolution is None else str(resolution),
                elevation_units=str(info.get("units") or "EPSG:5030"),
                elevation_unit_symbol=str(info.get("unitSymbol") or "m"),
            )
    return values


def time_resolution(milliseconds):
    """(count, unit) for a stored time resolution: the largest unit it fills
    exactly, down to milliseconds, so a saved value comes back as it was;
    (1, "days") when there is none."""
    try:
        milliseconds = int(milliseconds)
    except (TypeError, ValueError):
        return 1, "days"
    if milliseconds <= 0:
        return 1, "days"
    for unit, size in TIME_UNITS:  # the last unit, milliseconds, always fits
        if milliseconds % size == 0:
            return milliseconds // size, unit


def dimension_info(dimension, values, stored=None):
    """The dimensionInfo GeoServer stores, from the form's values, over what
    was stored (a field the form does not show is kept)."""
    info = dict(stored or {})
    info.update(
        enabled=True,
        attribute=values[f"{dimension}_attribute"],
        presentation=values[f"{dimension}_presentation"],
    )
    end = values.get(f"{dimension}_end_attribute") or ""
    if end:
        info["endAttribute"] = end
    else:
        info.pop("endAttribute", None)
    info.pop("resolution", None)
    if values[f"{dimension}_presentation"] == "DISCRETE_INTERVAL":
        if dimension == "time":
            size = dict(TIME_UNITS)[values["time_resolution_unit"]]
            info["resolution"] = int(values["time_resolution"]) * size
        else:
            info["resolution"] = float(values["elevation_resolution"])
    if dimension == "time":
        info.setdefault("units", "ISO8601")
    else:
        info["units"] = values["elevation_units"]
        info["unitSymbol"] = values["elevation_unit_symbol"]
    default = {"strategy": values[f"{dimension}_default"]}
    if default["strategy"] in ("NEAREST", "FIXED"):
        default["referenceValue"] = values[f"{dimension}_reference"]
    info["defaultValue"] = default
    return info


def with_dimensions(entries, values):
    """The metadata entries with each dimension as the form left it, the
    others kept: a metadata PUT replaces the whole map. A dimension turned
    off keeps its settings, with enabled false."""
    by_key = {e.get("@key"): e for e in entries if isinstance(e, dict)}
    kept = [
        e for e in entries if not (isinstance(e, dict) and e.get("@key") in DIMENSIONS)
    ]
    for dimension in DIMENSIONS:
        stored = (by_key.get(dimension) or {}).get("dimensionInfo")
        if values.get(f"{dimension}_enabled"):
            info = dimension_info(dimension, values, stored)
        elif stored is not None:
            info = dict(stored, enabled=False)
        else:
            continue
        kept.append({"@key": dimension, "dimensionInfo": info})
    return kept
