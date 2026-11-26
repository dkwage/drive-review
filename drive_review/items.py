"""Review items and extension categories."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

CATEGORIES = {
    "Video": "mp4 mkv avi mov wmv mpeg mpg webm m4v 3gp 3g2 asf flv mts m2ts mxf vob ogv divx",
    "Image": ("jpg jpeg jpe jif jfif png apng gif bmp webp heic heif avif tif tiff ico svg "
              "raw arw srf sr2 cr2 cr3 crw nef nrw dng raf rw2 orf pef ptx srw x3f 3fr iiq mos rwl dcr kdc"),
    "Docs": ("pdf doc docx docm dot dotx xls xlsx xlsm xlsb ppt pptx pptm pps ppsx hwp hwpx hwt "
             "txt csv tsv odt ods odp rtf pages numbers key md epub"),
    "Music": "mp3 wav flac aac m4a ogg wma aiff aif alac ape opus mid midi amr mka dsf dff",
    "Compressed": "zip rar 7z tar gz bz2 xz tgz tbz2 txz zst cab iso alz egg",
    "Design": ("psd psb psdt ai ait eps indd idml indt incx aep aepx aet prproj prprojx mogrt sesx "
               "fla xd lrcat"),
    "Etc": "",
}
EXTENSIONS = {ext: category for category, text in CATEGORIES.items() for ext in text.split()}
CATEGORY_LABELS = dict(zip(CATEGORIES, ("영상", "사진", "문서", "음악", "압축", "디자인·편집", "기타")))


def category_for(name):
    return EXTENSIONS.get(PurePosixPath(name).suffix.lower().lstrip("."), "Etc")


def iso_mtime(mtime_ns):
    seconds, nanos = divmod(mtime_ns, 10**9)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(
        microsecond=nanos // 1000
    ).isoformat(timespec="microseconds")


def bytes_label(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{size:,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024


@dataclass(frozen=True, slots=True)
class Item:
    rel_path: str
    size: int
    mtime_ns: int
    zip_entry: str = ""
    crc: int | None = None
    archive_stamp: str = ""
    warning: str = ""

    @property
    def key(self):
        return self.rel_path, self.zip_entry

    @property
    def identity(self):
        return f"{self.rel_path}::{self.zip_entry}" if self.zip_entry else self.rel_path

    @property
    def name(self):
        return (self.zip_entry or self.rel_path).rsplit("/", 1)[-1]

    @property
    def category(self):
        return category_for(self.name)

    @property
    def folder(self):
        parent = (self.zip_entry or self.rel_path).rpartition("/")[0]
        if self.zip_entry:
            return self.rel_path + ("/" + parent if parent else "")
        return parent

    @property
    def stamp(self):
        return f"{self.size}:{self.mtime_ns}:{self.crc}:{self.archive_stamp}"
