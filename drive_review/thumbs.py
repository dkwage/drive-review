"""Local thumbnail cache and read-only thumbnail providers."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageOps

from .dupes import CHUNK, check_cancel
from .paths import safe_path
from .thumbs_windows import shell_thumbnail
from .zipsource import open_item

MAX_AUTO_MEMBER = 64 * 1024 * 1024


def copy_for_view(root, item, directory, stop=None):
    # A viewer can only access a temporary copy, even if it supports editing.
    output = safe_path(directory, item.name)
    with open_item(root, item) as source, output.open("xb") as target:
        while True:
            check_cancel(stop)
            data = source.read(CHUNK)
            if not data:
                break
            target.write(data)
    return output


def _pillow(path, size):
    try:
        with Image.open(path) as original:
            original.draft("RGB", (size, size))
            image = ImageOps.exif_transpose(original)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            return image.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


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


def _thumbnail(path, item, size):
    image = shell_thumbnail(path, size)
    if image is None and item.category in {"Image", "Design"}:
        image = _pillow(path, size)
    if image is None and (item.category in {"Image", "Design", "Video"} or path.suffix.lower() == ".pdf"):
        image = _quicklook(path, size)
    if image is not None:
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


class ThumbnailCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def key(drive_id, item, size):
        signature = repr((drive_id, item.key, item.stamp, size)).encode("utf-8", errors="surrogatepass")
        return hashlib.blake2b(signature, digest_size=24).hexdigest()

    def get(self, drive, item, size, *, explicit=False, stop=None):
        if item.zip_entry:
            if item.category == "Video" and not explicit:
                return None
            if item.category not in {"Image", "Video"} and not item.name.lower().endswith(".pdf"):
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
                image = _thumbnail(path, item, size)
        else:
            # Check the scan fingerprint before giving an external read-only provider the path.
            with open_item(drive.root, item):
                image = _thumbnail(safe_path(drive.root, item.rel_path), item, size)

        check_cancel(stop)
        if image is None:
            return None
        from io import BytesIO
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
