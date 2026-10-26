#! python3  # noqa E265

"""GeoServer's collection shapes, tolerated in one place (toolbelt/payload.py)."""

import unittest

from geoserver_manager.toolbelt.payload import (
    DEFAULT_VALIDATOR,
    VIRTUAL_TABLE,
    as_int,
    as_list,
    bbox_text,
    crs_text,
    dimension_attributes,
    dimension_form_values,
    name_of,
    time_resolution,
    unwrap,
    view_form_values,
    view_of,
    virtual_table,
    with_dimensions,
    with_view,
)


class TestUnwrap(unittest.TestCase):
    def test_a_list_a_bare_object_a_bare_string_and_nothing(self):
        self.assertEqual(
            unwrap({"layers": {"layer": [{"name": "a"}]}}, "layers", "layer"),
            [{"name": "a"}],
        )
        self.assertEqual(
            unwrap({"layers": {"layer": {"name": "a"}}}, "layers", "layer"),
            [{"name": "a"}],
        )
        # a one-entry list=available collection is written as a bare string
        self.assertEqual(
            unwrap({"list": {"string": "states"}}, "list", "string"), ["states"]
        )
        self.assertEqual(unwrap({"layers": ""}, "layers", "layer"), [])
        self.assertEqual(unwrap({}, "layers", "layer"), [])
        self.assertEqual(unwrap("not json", "layers", "layer"), [])


class TestSmallOnes(unittest.TestCase):
    def test_as_list(self):
        self.assertEqual(as_list(""), [])
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_list("one"), ["one"])
        self.assertEqual(as_list({"name": "x"}), [{"name": "x"}])
        self.assertEqual(as_list(("a", "b")), ["a", "b"])

    def test_as_int(self):
        self.assertEqual(as_int("4"), 4)
        self.assertEqual(as_int(4.0), 4)
        self.assertEqual(as_int("four"), 0)
        self.assertEqual(as_int(None), 0)
        self.assertEqual(as_int("", 10), 10)

    def test_name_of(self):
        self.assertEqual(name_of({"name": "topp"}), "topp")
        self.assertEqual(name_of("topp"), "topp")
        self.assertEqual(name_of({"href": "x"}), "{'href': 'x'}")

    def test_crs_text(self):
        self.assertEqual(crs_text("EPSG:4326"), "EPSG:4326")
        self.assertEqual(
            crs_text({"@class": "projected", "$": "EPSG:3857"}), "EPSG:3857"
        )
        self.assertEqual(crs_text(None), "")

    def test_bbox_text(self):
        box = {"minx": 1, "miny": 2, "maxx": 3, "maxy": 4, "crs": "EPSG:4326"}
        self.assertEqual(bbox_text(box), "1, 2 → 3, 4  (EPSG:4326)")
        self.assertEqual(
            bbox_text({"minx": 1, "miny": 2, "maxx": 3, "maxy": 4}), "1, 2 → 3, 4"
        )
        self.assertEqual(bbox_text({"minx": 1}), "")
        self.assertEqual(bbox_text(None), "")


class TestKeywordsAndText(unittest.TestCase):
    """One parser for every tab: the three copies it replaced disagreed."""

    def test_every_keyword_shape_becomes_a_list(self):
        from geoserver_manager.toolbelt.payload import keyword_list

        self.assertEqual(keyword_list(["a", "b"]), ["a", "b"])  # the library
        self.assertEqual(keyword_list({"string": ["a", "b"]}), ["a", "b"])  # REST
        self.assertEqual(keyword_list({"string": "a"}), ["a"])  # one, bare
        self.assertEqual(keyword_list(None), [])
        self.assertEqual(keyword_list(""), [])

    def test_text_is_one_line_with_languages_in_order(self):
        from geoserver_manager.toolbelt.payload import text_of

        self.assertEqual(text_of("Roads"), "Roads")
        self.assertEqual(
            text_of({"fr": "Routes", "en": "Roads"}), "en: Roads; fr: Routes"
        )
        self.assertEqual(text_of(None), "")


class TestChanged(unittest.TestCase):
    def test_only_fields_that_differ_come_back_under_their_rest_names(self):
        from geoserver_manager.toolbelt.payload import changed

        before = {"url": "a", "enabled": True, "description": "x"}
        after = {"url": "b", "enabled": True, "description": ""}
        self.assertEqual(
            changed(
                before,
                after,
                (
                    ("url", "url"),
                    ("enabled", "enabled"),
                    ("description", "description"),
                ),
            ),
            {"url": "b", "description": ""},
        )
        self.assertEqual(changed(before, dict(before), (("url", "url"),)), {})


if __name__ == "__main__":
    unittest.main()


# A SQL view as GeoServer 2.28.5 answered it: the SQL with an added newline,
# a single key and parameter written bare, a numeric default as a number.
STORED_VIEW = {
    "name": "pts",
    "sql": "SELECT g AS id, geom FROM t WHERE g < %n%\n",
    "escapeSql": False,
    "keyColumn": "id",
    "geometry": {"name": "geom", "type": "Point", "srid": 4326},
    "parameter": {"name": "n", "defaultValue": 5, "regexpValidator": "^[\\d]+$"},
}


class TestSqlViews(unittest.TestCase):
    def test_a_view_is_found_among_other_metadata_and_a_table_has_none(self):
        resource = {
            "metadata": {
                "entry": [
                    {"@key": "cachingEnabled", "$": "true"},
                    {"@key": VIRTUAL_TABLE, "virtualTable": STORED_VIEW},
                ]
            }
        }
        self.assertEqual(virtual_table(resource), STORED_VIEW)
        self.assertIsNone(virtual_table({"metadata": {"entry": {"@key": "x"}}}))
        self.assertIsNone(virtual_table({}))

    def test_the_form_values_of_a_stored_view(self):
        self.assertEqual(
            view_form_values(STORED_VIEW),
            {
                "sql": "SELECT g AS id, geom FROM t WHERE g < %n%",
                "geometry_column": "geom",
                "geometry_type": "Point",
                "key_columns": ["id"],
                "parameters": [["n", "5", "^[\\d]+$"]],
            },
        )

    def test_the_form_values_give_the_view_back(self):
        values = view_form_values(STORED_VIEW)
        view = view_of("pts", values, 4326)
        self.assertEqual(view["sql"], "SELECT g AS id, geom FROM t WHERE g < %n%")
        self.assertEqual(view["keyColumn"], "id")
        self.assertEqual(view["geometry"], STORED_VIEW["geometry"])
        self.assertEqual(
            view["parameter"],
            [{"name": "n", "defaultValue": "5", "regexpValidator": "^[\\d]+$"}],
        )

    def test_a_parameter_without_a_validator_gets_the_default_one(self):
        # Without one, GeoServer passes any viewparams value into the SQL.
        view = view_of(
            "v", {"sql": "x", "parameters": [["n", "1", ""], ["", "", ""]]}, 4326
        )
        self.assertEqual(
            view["parameter"],
            [{"name": "n", "defaultValue": "1", "regexpValidator": DEFAULT_VALIDATOR}],
        )

    def test_a_parameter_stored_without_a_validator_keeps_none(self):
        # A date needs none: an SQL edit added the default one, and clients
        # sending "lbl:2026-01-01" were refused (2.28.5).
        old = dict(
            STORED_VIEW,
            parameter=[STORED_VIEW["parameter"], {"name": "lbl", "defaultValue": "p"}],
        )
        values = dict(
            view_form_values(old),
            parameters=[["n", "5", "^[\\d]+$"], ["lbl", "p", ""], ["new", "1", ""]],
        )
        self.assertEqual(
            view_of("pts", values, 4326, old=old)["parameter"],
            [
                {"name": "n", "defaultValue": "5", "regexpValidator": "^[\\d]+$"},
                {"name": "lbl", "defaultValue": "p"},
                {
                    "name": "new",
                    "defaultValue": "1",
                    "regexpValidator": DEFAULT_VALIDATOR,
                },
            ],
        )

    def test_a_second_geometry_column_is_kept(self):
        # The form read a list of them as one, and failed on it.
        second = {"name": "geom2", "type": "Polygon", "srid": 4326}
        old = dict(STORED_VIEW, geometry=[STORED_VIEW["geometry"], second])
        values = view_form_values(old)
        self.assertEqual(values["geometry_column"], "geom")
        self.assertEqual(
            view_of("pts", values, 4326, old=old)["geometry"],
            [STORED_VIEW["geometry"], second],
        )

    def test_no_geometry_column_sends_no_geometry_and_two_keys_a_list(self):
        view = view_of("v", {"sql": "x", "key_columns": ["a", " b "]}, 4326)
        self.assertNotIn("geometry", view)
        self.assertEqual(view["keyColumn"], ["a", "b"])

    def test_the_view_entry_is_replaced_and_the_others_kept(self):
        entries = [
            {"@key": "cachingEnabled", "$": "true"},
            {"@key": VIRTUAL_TABLE, "virtualTable": STORED_VIEW},
        ]
        self.assertEqual(
            with_view(entries, {"name": "new"}),
            [
                {"@key": "cachingEnabled", "$": "true"},
                {"@key": VIRTUAL_TABLE, "virtualTable": {"name": "new"}},
            ],
        )


# A feature type with both dimensions, as GeoServer 2.28.5 stored them.
DIMENSIONED = {
    "attributes": {
        "attribute": [
            {"name": "t", "binding": "java.sql.Timestamp"},
            {"name": "t_end", "binding": "java.sql.Timestamp"},
            {"name": "z", "binding": "java.math.BigDecimal"},
            {"name": "label", "binding": "java.lang.String"},
        ]
    },
    "metadata": {
        "entry": [
            {"@key": VIRTUAL_TABLE, "virtualTable": {"name": "d"}},
            {
                "@key": "time",
                "dimensionInfo": {
                    "enabled": True,
                    "attribute": "t",
                    "endAttribute": "t_end",
                    "presentation": "DISCRETE_INTERVAL",
                    "resolution": 86400000,
                    "units": "ISO8601",
                    "defaultValue": {"strategy": "MAXIMUM"},
                    "nearestMatchEnabled": True,
                },
            },
        ]
    },
}


class TestDimensions(unittest.TestCase):
    def test_each_dimension_is_offered_the_attributes_it_can_use(self):
        # Another attribute is stored, and the layer then breaks.
        self.assertEqual(dimension_attributes(DIMENSIONED, "time"), ["t", "t_end"])
        self.assertEqual(dimension_attributes(DIMENSIONED, "elevation"), ["z"])

    def test_the_form_values_of_stored_dimensions(self):
        values = dimension_form_values(DIMENSIONED)
        self.assertTrue(values["time_enabled"])
        self.assertEqual(values["time_attribute"], "t")
        self.assertEqual(values["time_end_attribute"], "t_end")
        self.assertEqual(
            (values["time_resolution"], values["time_resolution_unit"]), (1, "days")
        )
        self.assertEqual(values["time_default"], "MAXIMUM")
        self.assertFalse(values["elevation_enabled"])
        self.assertEqual(values["elevation_units"], "EPSG:5030")

    def test_a_time_resolution_takes_the_largest_unit_it_fills(self):
        self.assertEqual(time_resolution(2 * 604_800_000), (2, "weeks"))
        self.assertEqual(time_resolution(36 * 3_600_000), (36, "hours"))
        self.assertEqual(time_resolution(1500), (1500, "milliseconds"))
        self.assertEqual(time_resolution(None), (1, "days"))
        self.assertEqual(time_resolution(0), (1, "days"))

    def test_untouched_values_give_the_stored_dimension_back(self):
        entries = DIMENSIONED["metadata"]["entry"]
        written = with_dimensions(entries, dimension_form_values(DIMENSIONED))
        self.assertEqual(written[0], entries[0])  # the SQL view is kept
        self.assertEqual(written[1], entries[1])  # what the form hides too
        self.assertEqual(len(written), 2)  # no elevation written

    def test_elevation_on_and_time_off_keeping_its_settings(self):
        values = dict(
            dimension_form_values(DIMENSIONED),
            time_enabled=False,
            elevation_enabled=True,
            elevation_attribute="z",
            elevation_presentation="DISCRETE_INTERVAL",
            elevation_resolution="100",
            elevation_default="FIXED",
            elevation_reference="200",
        )
        written = {
            e["@key"]: e
            for e in with_dimensions(DIMENSIONED["metadata"]["entry"], values)
        }
        self.assertFalse(written["time"]["dimensionInfo"]["enabled"])
        self.assertEqual(written["time"]["dimensionInfo"]["attribute"], "t")
        elevation = written["elevation"]["dimensionInfo"]
        self.assertEqual(elevation["resolution"], 100.0)
        self.assertEqual(elevation["units"], "EPSG:5030")
        self.assertEqual(
            elevation["defaultValue"], {"strategy": "FIXED", "referenceValue": "200"}
        )
        self.assertNotIn("endAttribute", elevation)
