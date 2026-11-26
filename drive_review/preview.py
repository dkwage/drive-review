"""Dispatch preview types and prepare their content on a worker thread."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import sys
from tempfile import TemporaryDirectory
from uuid import UUID

from PIL import Image, ImageOps
from PIL.ImageQt import ImageQt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtPdf import QPdfDocument

from .dupes import check_cancel
from .items import category_for
from .paths import display_path
from .preview_text import OFFICE_EXTRACTORS, office_text, plain_text
from .preview_windows import handler_for
from .thumbs import copy_for_view
from .thumbs_windows import shell_thumbnail
from .zipsource import open_item, validate_item

try:
    from pillow_heif import register_heif_opener
except (ImportError, OSError):
    register_heif_opener = None
else:
    register_heif_opener()


class PreviewKind(Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    OFFICE = "office"
    TEXT = "text"
    OTHER = "other"


TEXT_EXTENSIONS = frozenset("""
txt text csv tsv json jsonl ndjson md markdown rst log yaml yml toml ini cfg conf xml html htm xhtml
css scss sass less js jsx ts tsx mjs cjs py pyw ipynb rb php pl pm lua r sql sh bash zsh fish ps1
bat cmd c h cc cpp cxx hpp cs java kt kts scala groovy swift m mm go rs dart vue svelte asm s tex
bib properties env gitignore gitattributes editorconfig diff patch srt vtt nfo vb vbs fs fsx clj
""".split())
TEXT_NAMES = {"readme", "license", "copying", "changelog", "makefile", "dockerfile", "gemfile", "rakefile"}
TEXT_FILENAMES = TEXT_NAMES | {".gitignore", ".gitattributes", ".editorconfig", ".env"}
OFFICE_EXTENSIONS = frozenset("""
doc dot xls xlsb xlt ppt pps pot rtf odt ods odp hwp hwpx hwt pages numbers key
""".split())


def preview_kind(name):
    filename = PurePosixPath(name.lower()).name
    extension = PurePosixPath(filename).suffix
    if extension == ".pdf":
        return PreviewKind.PDF
    if extension in OFFICE_EXTRACTORS or extension[1:] in OFFICE_EXTENSIONS:
        return PreviewKind.OFFICE
    if extension[1:] in TEXT_EXTENSIONS or filename in TEXT_FILENAMES:
        return PreviewKind.TEXT
    category = category_for(filename)
    return {"Image": PreviewKind.IMAGE, "Video": PreviewKind.VIDEO, "Music": PreviewKind.AUDIO}.get(
        category, PreviewKind.OTHER
    )


@dataclass(slots=True)
class PreviewData:
    kind: PreviewKind
    image: QImage | None = None
    text: str | None = None
    path: Path | None = None
    pdf: QPdfDocument | None = None
    handler: UUID | None = None
    directory: TemporaryDirectory | None = None

    def release(self):
        if self.pdf is not None:
            self.pdf.close()
            self.pdf.deleteLater()
            self.pdf = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


def stage_file(root, item, stop, progress):
    directory = TemporaryDirectory(prefix="drive-review-preview-", ignore_cleanup_errors=True)
    try:
        path = copy_for_view(root, item, directory.name, stop, progress)
    except BaseException:
        directory.cleanup()
        raise
    return path, directory


def source_image(root, item, stop):
    with open_item(root, item) as source:
        try:
            with Image.open(source) as original:
                check_cancel(stop)
                ImageOps.exif_transpose(original, in_place=True)
                check_cancel(stop)
                image = original if original.mode in {"RGB", "RGBA"} else original.convert("RGBA")
                return ImageQt(image)
        except Image.DecompressionBombError:
            raise ValueError("The image is too large to decode safely.") from None
        except (OSError, ValueError, SyntaxError):
            return None


def load_preview(root, item, gui_thread, stop, progress, *, use_handler=True):
    check_cancel(stop)
    kind = preview_kind(item.name)
    extension = PurePosixPath(item.name).suffix.lower()
    result = PreviewData(kind)
    path, _ = validate_item(root, item)

    def path_for_view(*, copy=False):
        if item.zip_entry or copy:
            if result.directory is None:
                result.path, result.directory = stage_file(root, item, stop, progress)
            return result.path
        return path

    try:
        if kind in {PreviewKind.OFFICE, PreviewKind.OTHER} and use_handler:
            if clsid := handler_for(extension):
                try:
                    path_for_view(copy=True)
                except InterruptedError:
                    raise
                except (OSError, ValueError):
                    pass
                else:
                    result.handler = clsid
                    return result

        if kind == PreviewKind.TEXT:
            with open_item(root, item) as source:
                result.text = plain_text(source)
        elif kind == PreviewKind.OFFICE and extension in OFFICE_EXTRACTORS:
            source_file = path_for_view().open("rb") if item.zip_entry else open_item(root, item)
            with source_file as source:
                result.text = office_text(source, item.name, stop)
        elif kind in {PreviewKind.VIDEO, PreviewKind.AUDIO}:
            long_windows_path = sys.platform == "win32" and len(display_path(path)) >= 248
            result.path = path_for_view(copy=long_windows_path)
        elif kind == PreviewKind.PDF:
            document = QPdfDocument()
            result.pdf = document
            error = document.load(str(path_for_view()))
            if document.status() != QPdfDocument.Status.Ready:
                raise ValueError(f"The PDF could not be opened ({error.name}).")
            check_cancel(stop)
            document.moveToThread(gui_thread)
        elif kind == PreviewKind.IMAGE:
            result.image = source_image(root, item, stop)
            if result.image is None:
                image_path = path_for_view()
                reader = QImageReader(str(image_path))
                reader.setAutoTransform(True)
                image = reader.read()
                if image.isNull():
                    thumbnail = shell_thumbnail(image_path, 2048)
                    image = ImageQt(thumbnail) if thumbnail is not None else None
                result.image = image
        elif sys.platform == "win32":
            thumbnail = shell_thumbnail(path_for_view(), 2048)
            if thumbnail is not None:
                result.image = ImageQt(thumbnail)

        if result.pdf is None:
            check_cancel(stop)
        if kind not in {PreviewKind.VIDEO, PreviewKind.AUDIO, PreviewKind.PDF}:
            result.release()
    except BaseException:
        if result.pdf is not None:
            result.pdf.close()
            result.pdf = None
        result.release()
        raise
    return result
