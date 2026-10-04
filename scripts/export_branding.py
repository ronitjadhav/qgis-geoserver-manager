"""Export the branding with Qt, the same SVG renderer used by the plugin.

Run with PyQt5 and Pillow installed:
    QT_QPA_PLATFORM=offscreen python3 scripts/export_branding.py

Edit resources/images/geoserver_manager.svg first. No network is used.
"""

import os
import sys
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QGuiApplication, QImage, QPainter
from PyQt5.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "geoserver_manager/resources/images"
OUTPUT = ROOT / "docs/static/branding"
GREEN, BLUE = "#589632", "#0099c0"
INK, PAPER = "#172f36", "#f6f7f3"


def document(width, height, body, title="GeoServer Manager"):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="title">\n'
        f'<title id="title">{title}</title>\n{body}\n</svg>\n'
    )


def mark(paths, x, y, size):
    return f'<g transform="translate({x} {y}) scale({size / 64})">{paths}</g>'


def label(x, y, value, size=16, color=INK, weight=400):
    return (
        f'<text x="{x}" y="{y}" fill="{color}" '
        f'font-family="DejaVu Sans, sans-serif" font-size="{size}" '
        f'font-weight="{weight}">{value}</text>'
    )


def rectangle(x, y, width, height, fill, radius=0):
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'rx="{radius}" fill="{fill}"/>'
    )


def render(svg, destination, size=None):
    renderer = QSvgRenderer(svg.encode("utf-8"))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG for {destination}")
    dimensions = renderer.defaultSize()
    width, height = size or (dimensions.width(), dimensions.height())
    image = QImage(width * 4, height * 4, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    image = image.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if not image.save(str(destination)):
        raise OSError(f"Could not save {destination}")


def export(name, svg):
    (OUTPUT / f"{name}.svg").write_text(svg, encoding="utf-8")
    render(svg, OUTPUT / f"{name}.png")


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source = (IMAGES / "geoserver_manager.svg").read_text(encoding="utf-8")
    root = ElementTree.fromstring(source)
    paths = "\n".join(
        "<path "
        + " ".join(f'{key}="{value}"' for key, value in path.attrib.items())
        + "/>"
        for path in root.findall("{http://www.w3.org/2000/svg}path")
    )
    mono, white = paths, paths
    for color in (GREEN, BLUE):
        mono = mono.replace(color, INK)
        white = white.replace(color, "#ffffff")
    for name, shapes in (("mark", paths), ("mark-mono", mono), ("mark-white", white)):
        export(name, document(64, 64, shapes))

    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        render(source, OUTPUT / f"icon-{size}.png", (size, size))
    # Retain the existing PNG metadata path for plugin-repository compatibility.
    render(source, IMAGES / "default_icon.png", (256, 256))
    with Image.open(OUTPUT / "icon-256.png") as icon:
        icon.save(
            OUTPUT / "favicon.ico",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )

    for name, background in (("avatar", PAPER), ("avatar-dark", INK)):
        export(
            name,
            document(
                1024,
                1024,
                rectangle(0, 0, 1024, 1024, background) + mark(paths, 176, 176, 672),
            ),
        )

    for name, color in (("wordmark", INK), ("wordmark-white", "#ffffff")):
        export(
            name,
            document(
                620,
                96,
                mark(paths, 0, 0, 96)
                + label(116, 61, "GeoServer Manager", 42, color, 600),
            ),
        )

    export(
        "social-card",
        document(
            1200,
            630,
            rectangle(0, 0, 1200, 630, PAPER)
            + mark(paths, 94, 155, 320)
            + label(478, 270, "GeoServer", 72, weight=600)
            + label(478, 350, "Manager", 72, weight=600)
            + label(482, 421, "GeoServer, inside QGIS.", 28, "#52686d"),
        ),
    )

    preview = (
        rectangle(0, 0, 1440, 960, PAPER)
        + label(64, 65, "GEOSERVER MANAGER", 18, weight=600)
        + label(64, 117, "The Ribbon G.", 36, weight=600)
        + rectangle(64, 161, 632, 475, "#ffffff", 24)
        + mark(paths, 232, 216, 296)
        + label(104, 585, "GeoServer, inside QGIS.", 23)
        + rectangle(720, 161, 656, 213, "#ffffff", 24)
        + mark(paths, 758, 212, 98)
        + label(882, 254, "GeoServer", 32, weight=600)
        + label(882, 293, "Manager", 32, weight=600)
        + rectangle(720, 398, 312, 238, INK, 24)
        + mark(paths, 800, 436, 152)
        + rectangle(1056, 398, 320, 238, "#e6ebe7", 24)
        + '<circle cx="1216" cy="516" r="86" fill="#ffffff"/>'
        + mark(paths, 1152, 452, 128)
        + label(64, 702, "AT HOME IN THE TOOLBAR", 15, weight=600)
        + label(720, 702, "ONE COLOUR", 15, weight=600)
    )
    for x, size in ((72, 16), (142, 24), (220, 32), (306, 48), (408, 64)):
        preview += mark(paths, x, 765 - size / 2, size)
        preview += label(x, 829, str(size), 14, "#52686d")
    preview += (
        mark(mono, 724, 730, 88)
        + rectangle(844, 723, 104, 104, INK, 16)
        + mark(white, 852, 731, 88)
        + label(1092, 752, "SVG + PNG", 19, weight=600)
        + label(1092, 790, "Light and dark", 17, "#52686d")
        + label(64, 907, "PLUGIN  /  WEBSITE  /  SOCIAL", 14, "#52686d")
    )
    export(
        "preview", document(1440, 960, preview, "GeoServer Manager identity preview")
    )
    with ZipFile(OUTPUT / "brand-kit.zip", "w", compression=ZIP_DEFLATED) as archive:
        for asset in sorted(OUTPUT.iterdir()):
            if asset.suffix in {".svg", ".png", ".ico"}:
                archive.write(asset, asset.name)
        guide = (ROOT / "docs/branding.md").read_text(encoding="utf-8")
        guide = guide.replace("static/branding/", "")
        guide = guide.replace("[Download the complete brand kit](brand-kit.zip).", "")
        archive.writestr("BRANDING.md", guide)
    print(f"Branding exported to {OUTPUT.relative_to(ROOT)}")
    app.quit()


if __name__ == "__main__":
    main()
