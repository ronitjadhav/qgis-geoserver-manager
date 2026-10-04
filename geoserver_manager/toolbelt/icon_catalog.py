#! python3  # noqa: E265

"""The icon inventory, usable by the plugin and documentation without QGIS."""

import json
from functools import lru_cache
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "resources"
CATALOG_PATH = RESOURCES / "icons" / "catalog.json"
SOURCE_COLOURS = ("#172f36", "#0099c0", "#589632", "#b3261e")


@lru_cache(maxsize=1)
def load_catalog():
    """Read the single registry of custom artwork and declared fallback icons."""
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def icon_spec(name):
    """Find a registered icon; an unknown name is a development error."""
    try:
        return load_catalog()["icons"][name]
    except KeyError:
        raise ValueError(
            f"Unregistered icon {name!r}. Add it to resources/icons/catalog.json."
        ) from None
