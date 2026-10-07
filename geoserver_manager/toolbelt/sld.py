#! python3  # noqa: E265

"""
SLD helpers shared by the Styles and Layers tabs: what version a document is,
which content type and encoding GeoServer wants for it, and how to move a
style between a QGIS layer and an SLD string.

Nothing here imports `qgis` at module level, so `sld_version` and
`sld_content_type` (the part with the rules worth pinning) are testable in an
interpreter without QGIS, like the CI unit job. The functions that do touch a
QGIS layer import it when called, and must run on the GUI thread: they read and
write live layer objects (see invariant 9 in docs/development/invariants.md).
`icon_package` imports it only to word a refusal.
"""

import html
import io
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

# GeoServer chooses its SLD parser from the request's content type, not from the
# document. Send the wrong one and it stores the body under the wrong
# languageVersion: accepted, rendered, and mislabelled.
SLD_1_0 = "application/vnd.ogc.sld+xml"
SLD_1_1 = "application/vnd.ogc.se+xml"

_VERSION = re.compile(
    r"StyledLayerDescriptor[^>]*\bversion\s*=\s*[\"']([\d.]+)[\"']", re.IGNORECASE
)
# QGIS writes Symbology Encoding elements (se:PolygonSymbolizer, …) for SLD 1.1.
_SE_NAMESPACE = re.compile(r"xmlns:se\s*=|<\s*se:", re.IGNORECASE)
# An XML declaration up to its encoding's name, which a rewrite replaces.
_DECLARATION = re.compile(
    r"^(\ufeff?\s*<\?xml\b[^>]*?\bencoding\s*=\s*[\"'])([\w.:-]+)"
)
_RASTER = re.compile(r"<\s*(?:\w+:)?RasterSymbolizer\b")
# An OnlineResource href as three groups: up to its value, the quote, the value.
_HREF = re.compile(
    r"(<\s*(?:\w+:)?OnlineResource\b[^>]*?\b(?:\w+:)?href\s*=\s*)([\"'])(.*?)\2",
    re.DOTALL,
)
# A path from the root, or a URI of any scheme but "file:" without a slash.
_ABSOLUTE = re.compile(r"^(?:[/\\]|file:/|(?!file:)[A-Za-z][\w+.-]*:)", re.IGNORECASE)
# A URI scheme; two letters at least, so that C:/ stays a Windows path.
_SCHEME = re.compile(r"^[A-Za-z][\w+.-]+:")
_ROOTED = re.compile(r"^(?:[/\\]|[A-Za-z]:)")
_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_FONT_HREF = re.compile(r"(\bhref\s*=\s*[\"']ttf://)([^\"']*)")
# The images GeoServer unpacks from a style zip, lower-cased (2.28.5).
_ZIP_IMAGES = (".svg", ".png", ".jpg", ".bmp", ".gif")
# A Size that holds only an ogc:Literal, which QGIS reads as 0.
_LITERAL_SIZE = re.compile(
    r"<((?:\w+:)?Size)>\s*<((?:\w+:)?Literal)>\s*([^<]*?)\s*</\2>\s*</\1>"
)


def sld_version(sld):
    """The SLD version of a document: "1.1.0" or "1.0.0".

    Reads the version attribute, and falls back to the Symbology Encoding
    namespace for documents that leave it out: an SE document is 1.1 whatever
    the root element says.
    """
    match = _VERSION.search(sld or "")
    if match:
        return "1.1.0" if match.group(1).startswith("1.1") else "1.0.0"
    return "1.1.0" if _SE_NAMESPACE.search(sld or "") else "1.0.0"


def sld_content_type(sld):
    """The content type GeoServer needs in order to parse this document."""
    return SLD_1_1 if sld_version(sld) == "1.1.0" else SLD_1_0


def style_text(data):
    """A style body as text: UTF-8, else the encoding its XML declaration names.

    GeoServer reads a style as UTF-8, whatever its declaration says. A file
    put in the data directory by hand is served byte for byte, and a Latin-1
    one is no valid UTF-8: it is decoded as it declares.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    match = _DECLARATION.match(data[:200].decode("latin-1"))
    try:
        return data.decode(match.group(2) if match else "utf-8", errors="replace")
    except LookupError:  # an encoding Python does not know, or not a text one
        return data.decode("utf-8", errors="replace")


def utf8_sld(sld):
    """An SLD, text or bytes, as UTF-8 bytes under a declaration that says so.

    GeoServer reads an SLD body as UTF-8 whatever its declaration names
    (measured on 2.28.5): an ISO-8859-1 SLD 1.0 was stored with replacement
    characters, and an SLD 1.1 one, stored as sent, was read with them.
    """
    text = style_text(sld) if isinstance(sld, bytes) else sld
    match = _DECLARATION.match(text)
    if match and match.group(2).upper() not in ("UTF-8", "UTF8"):
        text = match.group(1) + "UTF-8" + text[match.end() :]
    return text.encode("utf-8")


def has_raster_symbolizer(sld):
    """Whether an SLD draws a raster: GetLegendGraphic then needs a raster layer."""
    return _RASTER.search(sld or "") is not None


def relative_hrefs(sld):
    """The files an SLD points to beside itself, once each, in order.

    GeoServer resolves a relative OnlineResource href (an ExternalGraphic's
    icon, a fill image) against the style's own folder on the server.
    """
    found = [match.group(3) for match in _HREF.finditer(sld or "")]
    found = [href for href in found if href and not _ABSOLUTE.match(href)]
    return list(dict.fromkeys(found))


def _href_path(href):
    """An href's path as text: its query dropped, XML entities decoded."""
    return html.unescape(href.partition("?")[0])


def _local_file(href):
    """The file on this machine an absolute href names, or None."""
    text = _href_path(href)
    if text[:5].lower() == "file:":
        text = url2pathname(urlparse(text).path)
    elif _SCHEME.match(text):
        return None
    path = Path(text)
    try:
        return path if path.is_absolute() and path.is_file() else None
    except OSError:  # a name too long for the file system is no file either
        return None


def local_icons(sld):
    """{href: file} of the icons an SLD draws from files on this machine.

    QGIS writes an SVG or image marker as its file's path, which GeoServer
    looks for in its own data directory (measured on 2.28.5: it drew the
    fallback square), and adds a fallback relative to QGIS's SVG folders,
    "gpsicons/plane.svg": the end of that same path.
    """
    hrefs = [match.group(3) for match in _HREF.finditer(sld or "")]
    found = {href: _local_file(href) for href in hrefs}
    found = {href: path for href, path in found.items() if path is not None}
    files = list(found.values())
    for href in hrefs:
        tail = _href_path(href).replace("\\", "/")
        if href in found or not tail or _SCHEME.match(tail) or _ROOTED.match(tail):
            continue
        for path in files:
            if path.as_posix().endswith(f"/{tail}"):
                found[href] = path
                break
    return found


def icon_package(sld, style_name):
    """(zip, SLD): the SLD zipped with the files its icons are drawn from on
    this machine, and the SLD in it as UTF-8 bytes; None when it names none.

    GeoServer unpacks the zip beside the style. Each href becomes its file's
    name there, after the style's ("roads_plane.svg"), so that two styles'
    icons of one name do not replace each other in the folder they share. A
    parametric SVG keeps its "?fill=…". GeoServer unpacks SVG, PNG, JPG, BMP
    and GIF files only (2.28.5), so a .jpeg goes as .jpg, and any other
    image is refused (RuntimeError) before a request. Measured on 2.28.5: a
    zip POST keeps an SLD 1.1 document as sent, recorded as 1.1.0; a zip PUT
    keeps the style's recorded format and version, so a replace sends the
    SLD again.
    """
    icons = local_icons(sld)
    if not icons:
        return None
    names, refused = {}, []
    for path in dict.fromkeys(icons.values()):
        suffix = path.suffix.lower()
        suffix = ".jpg" if suffix == ".jpeg" else suffix
        if suffix not in _ZIP_IMAGES:
            refused.append(str(path))
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", f"{style_name}_{path.stem}")
        name, count = f"{stem}{suffix}", 1
        while name in names.values():
            count += 1
            name = f"{stem}_{count}{suffix}"
        names[path] = name
    if refused:
        from qgis.PyQt.QtCore import QCoreApplication

        raise RuntimeError(
            QCoreApplication.translate(
                "Sld",
                "GeoServer takes only SVG, PNG, JPEG, BMP and GIF icons with a "
                "style. Convert these to one of them: {}",
            ).format(", ".join(refused))
        )

    def packaged(match):
        href = match.group(3)
        if href not in icons:
            return match.group(0)
        _path, mark, query = href.partition("?")
        name = names[icons[href]]
        return f"{match.group(1)}{match.group(2)}{name}{mark}{query}{match.group(2)}"

    document = utf8_sld(_HREF.sub(packaged, sld))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("style.sld", document)
        for path, name in names.items():
            package.write(path, name)
    return buffer.getvalue(), document


def icons_for_qgis(sld, base_url, workspace_name=None):
    """The SLD with each icon kept beside the style made a URL QGIS fetches.

    QGIS keeps a relative href relative and draws a "?" for it, even beside
    the SLD it read (measured on 3.44). GeoServer serves a style's folder
    without a login at {base}/styles, or {base}/styles/{workspace}, and its
    SLD 1.0 rendition of a stored 1.1 style names the same files as
    file:{data directory}/…/styles/…, a path on the server.
    """
    styles = f"{base_url}/styles"
    folder = f"{styles}/{quote(workspace_name, safe='')}" if workspace_name else styles

    def fetchable(match):
        path, mark, query = match.group(3).partition("?")
        if path[:5].lower() == "file:":
            head, found, rest = urlparse(path).path.rpartition("/styles/")
            if not found:
                return match.group(0)
            owner = re.search(r"/workspaces/([^/]+)$", head)
            url = f"{styles}/{owner.group(1)}/{rest}" if owner else f"{styles}/{rest}"
        elif path and not _SCHEME.match(path) and not _ROOTED.match(path):
            url = f"{folder}/{path}"
        else:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{url}{mark}{query}{match.group(2)}"

    return _HREF.sub(fetchable, sld or "")


def unresolved_icons(sld):
    """The icons of an SLD that QGIS cannot open, once each: a relative path,
    or a file that is not on this machine. A URL is QGIS's to fetch."""
    missing = []
    for match in _HREF.finditer(sld or ""):
        href = match.group(3)
        path = _href_path(href)
        if not path or (_SCHEME.match(path) and path[:5].lower() != "file:"):
            continue
        if _local_file(href) is None:
            missing.append(path)
    return list(dict.fromkeys(missing))


def styleable_project_layers(rasters=True):
    """The project's layers that can carry an SLD: its vector and raster ones.

    QGIS writes SLD for both, but reads it into a vector layer only: a raster
    answers "Layer type 1 not supported" (3.40 and 3.44), so an Apply asks
    for rasters=False. A mesh or point-cloud layer would just fail later with
    a worse message. The forms' layer pickers filter the same kinds.
    """
    from qgis.core import QgsMapLayer, QgsProject

    kinds = (QgsMapLayer.LayerType.VectorLayer,) + (
        (QgsMapLayer.LayerType.RasterLayer,) if rasters else ()
    )
    return [
        layer
        for layer in QgsProject.instance().mapLayers().values()
        if layer.type() in kinds
    ]


def _answer(result):
    """(ok, message) of what saveSldStyle or loadSldStyle returned.

    Both return (str, bool) on 3.40 and 3.44; order and arity have moved
    between QGIS releases, so either way round is accepted.
    """
    ok, message = True, ""
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, bool):
                ok = item
            elif isinstance(item, str):
                message = item
    elif isinstance(result, bool):
        ok = result
    return ok, message


def layer_to_sld(layer):
    """One QGIS layer's symbology as an SLD string. GUI thread only.

    Raises RuntimeError with QGIS's own reason when it cannot write it all:
    3.44 refuses the export ("Cannot export label expression … to SLD"),
    3.40 writes what it cannot as a "… not implemented yet" comment and
    reports a success, which drew nothing, or "Placeholder" for each label.
    4.2 writes a label expression as SLD functions under QGIS's own names.
    """
    from qgis.PyQt.QtCore import QCoreApplication

    path = Path(tempfile.mkdtemp(prefix="gsm_sld_")) / "style.sld"
    try:
        ok, message = _answer(layer.saveSldStyle(str(path)))
        sld = path.read_text(encoding="utf-8") if path.exists() else ""
    finally:
        if path.exists():
            path.unlink()
        path.parent.rmdir()
    if not ok and message:
        raise RuntimeError(message)
    skipped = [text.strip() for text in _COMMENT.findall(sld)]
    skipped = [text for text in skipped if "not implemented yet" in text]
    if skipped:
        raise RuntimeError(
            QCoreApplication.translate(
                "Sld", "QGIS could not write all of it as SLD: {}."
            ).format("; ".join(dict.fromkeys(skipped)))
        )
    if not ok or not sld.strip():
        raise RuntimeError(
            QCoreApplication.translate(
                "Sld",
                "QGIS exported no SLD for '{}'. Its symbology probably has no SLD "
                "equivalent.",
            ).format(layer.name())
        )
    # GeoServer reads the href as a URI and refuses "ttf://DejaVu Sans" (a
    # 500); QGIS reads the encoded name back as it was.
    return _FONT_HREF.sub(
        lambda match: match.group(1) + match.group(2).replace(" ", "%20"), sld
    )


def apply_sld_to_layer(layer, sld):
    """Load an SLD string into a QGIS layer. Returns (ok, message).

    GUI thread only. QGIS's SLD *reader* covers less than its writer, so a
    server style can come back "not applied"; the message is QGIS's own and
    worth showing. A vector layer only: QGIS reads no SLD into a raster.
    QGIS reads a Size given as an ogc:Literal as 0 and draws nothing (the
    demo style burg, measured on 3.44), so such a Size goes as its number.
    """
    path = Path(tempfile.mkdtemp(prefix="gsm_sld_")) / "style.sld"
    try:
        # QGIS reads the file in the encoding its declaration names.
        path.write_bytes(utf8_sld(_LITERAL_SIZE.sub(r"<\1>\3</\1>", sld)))
        ok, message = _answer(layer.loadSldStyle(str(path)))
    finally:
        if path.exists():
            path.unlink()
        path.parent.rmdir()
    if ok:
        layer.triggerRepaint()
    # QGIS names the temporary file, which is gone by now.
    return ok, message.replace(str(path), path.name)
