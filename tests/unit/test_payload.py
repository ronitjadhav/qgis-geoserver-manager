#! python3  # noqa E265

"""GeoServer's collection shapes, tolerated in one place (toolbelt/payload.py)."""

import unittest

from geoserver_manager.toolbelt.payload import (
    as_list,
    bbox_text,
    crs_text,
    name_of,
    unwrap,
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
