"""Local thumbnail cache and read-only thumbnail providers."""

import base64
import binascii
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from PIL import Image, ImageOps

from .dupes import CHUNK, check_cancel
from .paths import display_path, safe_path
from .thumbs_windows import shell_thumbnail
from .zipsource import open_item

MAX_AUTO_MEMBER = 64 * 1024 * 1024
XMP_WINDOW = 8 * 1024 * 1024
_XMP_TAG = re.compile(rb"<(/?)xmpGImg:image(?=[\s>])[^>]{0,1024}>")
_XMP_TAG_OVERLAP = 1040
_XMP_SPACE = re.compile(rb"\s+|&#(?:x0*[aAdD]|0*10|0*13);")


def copy_for_view(root, item, directory, stop=None, progress=lambda count, total: None):
    # A viewer can only access a temporary copy, even if it supports editing.
    name = item.name
    if sys.platform == "win32" and len(str(Path(directory) / name)) > 245:
        name = "preview" + Path(name).suffix[:24]
    output = safe_path(directory, name)
    copied = 0
    progress(copied, item.size)
    with open_item(root, item) as source, output.open("xb") as target:
        while True:
            check_cancel(stop)
            data = source.read(CHUNK)
            if not data:
                break
            target.write(data)
            copied += len(data)
            if copied == item.size or copied % (CHUNK * 16) == 0:
                progress(copied, item.size)
    return Path(display_path(output))


def _pillow(source, size, *, formats=None):
    try:
        with Image.open(source, formats=formats) as original:
            original.draft("RGB", (size, size))
            image = ImageOps.exif_transpose(original)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            return image.convert("RGBA")
    except (OSError, ValueError, SyntaxError, struct.error, Image.DecompressionBombError):
        return None


def _xmp_payloads(source, count, stop):
    tail = b""
    payload = None
    while count:
        check_cancel(stop)
        block = source.read(min(CHUNK, count))
        count = count - len(block) if block else 0
        data = tail + block
        # Keep enough lookahead for a tag that straddles two blocks.
        cutoff = max(0, len(data) - _XMP_TAG_OVERLAP) if count else len(data)
        start = 0
        for match in _XMP_TAG.finditer(data):
            if match.start() >= cutoff:
                break
            if match[1]:
                if payload is not None:
                    payload.extend(data[start:match.start()])
                    if len(payload) <= XMP_WINDOW:
                        yield payload
                payload = None
            else:
                payload = bytearray()
            start = match.end()
        cutoff = max(cutoff, start)
        if payload is not None:
            payload.extend(data[start:cutoff])
            if len(payload) > XMP_WINDOW:
                payload = None
        tail = data[cutoff:]


def _decode_xmp(source, count, size, stop):
    for payload in _xmp_payloads(source, count, stop):
        check_cancel(stop)
        try:
            jpeg = base64.b64decode(_XMP_SPACE.sub(b"", payload), validate=True)
        except binascii.Error:
            continue
        if image := _pillow(BytesIO(jpeg), size, formats=("JPEG",)):
            return image
    return None


def xmp_thumbnail(source, size, stop=None):
    """Decode XMP's embedded JPEG from the first and last bounded file regions."""
    length = source.seek(0, os.SEEK_END)
    # Merge overlapping regions to handle tags straddling the window boundary.
    regions = ((0, length),) if length <= 2 * XMP_WINDOW else (
        (0, XMP_WINDOW), (length - XMP_WINDOW, XMP_WINDOW))
    for offset, count in regions:
        source.seek(offset)
        if image := _decode_xmp(source, count, size, stop):
            return image
    return None


def _idml_thumbnail(source, size, stop):
    # Cap both the central directory read and the total decoded metadata.
    if source.seek(0, os.SEEK_END) > 2 * XMP_WINDOW:
        return None
    remaining = XMP_WINDOW
    try:
        with ZipFile(source) as package:
            for name in ("META-INF/metadata.xml", "designmap.xml"):
                try:
                    entry = package.getinfo(name)
                except KeyError:
                    continue
                count = min(entry.file_size, remaining)
                with package.open(entry) as metadata:
                    if image := _decode_xmp(metadata, count, size, stop):
                        return image
                remaining -= count
                if not remaining:
                    break
    except InterruptedError:
        raise
    except (OSError, BadZipFile, RuntimeError, CompressionError):
        pass
    return None


def _eps_thumbnail(source, size):
    source.seek(0)
    header = source.read(30)
    if len(header) != 30 or header[:4] != b"\xc5\xd0\xd3\xc6":
        return None
    offset, length = struct.unpack_from("<II", header, 20)
    if offset < len(header) or not 0 < length <= XMP_WINDOW:
        return None
    source.seek(offset)
    return _pillow(BytesIO(source.read(length)), size, formats=("TIFF",))


def _pdf_thumbnail(path, size):
    # The document is loaded, rendered and closed on the same worker.
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtPdf import QPdfDocument

    document = QPdfDocument()
    try:
        document.load(os.fspath(path))
        if document.status() != QPdfDocument.Status.Ready or not document.pageCount():
            return None
        points = document.pagePointSize(0)
        if points.isEmpty():
            return None
        scale = size / max(points.width(), points.height())
        dimensions = QSize(max(1, round(points.width() * scale)), max(1, round(points.height() * scale)))
        page = document.render(0, dimensions).convertToFormat(QImage.Format.Format_RGBA8888)
        if not page.isNull():
            return Image.frombytes("RGBA", (page.width(), page.height()), bytes(page.constBits()))
    finally:
        document.close()
    return None


def design_thumbnail(path, size, stop=None, *, prefer_composite=False):
    extension = path.suffix.lower()
    photoshop = extension in {".psd", ".psb", ".psdt"}
    composite_tried = photoshop and prefer_composite and path.stat().st_size <= MAX_AUTO_MEMBER
    if composite_tried and (image := _pillow(path, size)):
        return image
    with path.open("rb") as source:
        if image := xmp_thumbnail(source, size, stop):
            return image
        if extension == ".eps" and (image := _eps_thumbnail(source, size)):
            return image
        if extension == ".idml" and (image := _idml_thumbnail(source, size, stop)):
            return image
        source.seek(0)
        pdf = extension in {".ai", ".ait"} and b"%PDF-" in source.read(1024)
    check_cancel(stop)
    if photoshop and not composite_tried and (image := _pillow(path, size)):
        return image
    if pdf and (image := _pdf_thumbnail(path, size)):
        return image
    return shell_thumbnail(path, size) or _quicklook(path, size)


def _quicklook(path, size):
    if sys.platform != "darwin":
        return None
    with tempfile.TemporaryDirectory(prefix="drive-review-ql-") as directory:
        try:
            subprocess.run(["/usr/bin/qlmanage", "-t", "-s", str(size), "-o", directory, os.fspath(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        output = next(Path(directory).glob("*.png"), None)
        return _pillow(output, size) if output else None


def _thumbnail(path, item, size, stop=None):
    if item.category == "Design":
        image = design_thumbnail(path, size, stop)
    else:
        image = shell_thumbnail(path, size)
    if image is None and item.category == "Image":
        image = _pillow(path, size)
    if image is None and (item.category in {"Image", "Video"} or path.suffix.lower() == ".pdf"):
        image = _quicklook(path, size)
    if image is not None:
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


def can_thumbnail(item):
    if item.zip_entry:
        return item.category in {"Image", "Design", "Video"} or item.name.lower().endswith(".pdf")
    return (sys.platform == "win32" or item.category in {"Image", "Design"}
            or (sys.platform == "darwin" and (item.category == "Video" or item.name.lower().endswith(".pdf"))))


class ThumbnailCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def key(drive_id, item, size):
        signature = (drive_id, item.key, item.stamp, size)
        if item.category == "Design":
            signature += ("design-xmp",)
        signature = repr(signature).encode("utf-8", errors="surrogatepass")
        return hashlib.blake2b(signature, digest_size=24).hexdigest()

    def get(self, drive, item, size, *, explicit=False, stop=None):
        if not can_thumbnail(item):
            return None
        if item.zip_entry:
            if item.category == "Video" and not explicit:
                return None
            if item.size > MAX_AUTO_MEMBER and not explicit:
                return None
        check_cancel(stop)
        key = self.key(drive.id, item, size)
        cached = self.directory / key[:2] / f"{key}.png"
        try:
            return cached.read_bytes()
        except OSError:
            pass
        if item.zip_entry:
            with tempfile.TemporaryDirectory(prefix="drive-review-thumbnail-") as directory:
                path = copy_for_view(drive.root, item, directory, stop)
                image = _thumbnail(path, item, size, stop)
        else:
            # Check the scan fingerprint before giving an external read-only provider the path.
            with open_item(drive.root, item):
                image = _thumbnail(safe_path(drive.root, item.rel_path), item, size, stop)

        check_cancel(stop)
        if image is None:
            return None
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        pending = None
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as temp:
                pending = Path(temp.name)
                temp.write(data)
            os.replace(pending, cached)
        except OSError:
            # A full cache volume must not prevent previews of source files.
            pass
        finally:
            if pending:
                pending.unlink(missing_ok=True)
        return data
