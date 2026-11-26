"""List archive members by reading only the ZIP central directory."""

from collections import OrderedDict
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
import stat
from zipfile import ZipFile

from .items import Item
from .paths import is_link, relative_parts, safe_path


def file_stamp(info):
    return f"{info.st_size}:{info.st_mtime_ns}"


def list_zip(path, archive, stop=None):
    items = []
    directories = set()
    names = set()
    with ZipFile(path, allowZip64=True) as source:
        for entry in source.infolist():
            if stop is not None and stop.is_set():
                raise InterruptedError("스캔이 취소되었습니다.")
            if entry.flag_bits & 1:
                raise ValueError("암호화된 ZIP입니다.")
            name = entry.filename.removesuffix("/")
            relative_parts(name)
            if name in names:
                raise ValueError(f"이름이 중복된 ZIP 항목: {name}")
            names.add(name)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"ZIP에 심볼릭 링크가 있습니다: {name}")
            if entry.is_dir():
                directories.add(name)
                continue
            try:
                mtime = int(datetime(*entry.date_time, tzinfo=timezone.utc).timestamp()) * 10**9
            except (ValueError, OverflowError):
                mtime = archive.mtime_ns
            items.append(Item(archive.rel_path, entry.file_size, mtime, name, entry.CRC,
                              f"{archive.size}:{archive.mtime_ns}"))
    return items, directories


def validate_item(root, item):
    path = safe_path(root, item.rel_path)
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"일반 파일이 아닙니다: {item.rel_path}")
    expected = item.archive_stamp if item.zip_entry else f"{item.size}:{item.mtime_ns}"
    if file_stamp(info) != expected:
        raise OSError(f"스캔 후 파일이 변경되었습니다: {item.rel_path}")
    return path, info


class ItemReader(AbstractContextManager):
    """Reuse a few central directories during a bulk operation."""

    def __init__(self):
        self.archives = OrderedDict()

    def __exit__(self, *_error):
        for archive in self.archives.values():
            archive.close()
        self.archives.clear()

    def archive(self, path, info):
        key = (path, info.st_dev, info.st_ino, info.st_ctime_ns)
        if key not in self.archives:
            self.archives[key] = ZipFile(path, allowZip64=True)
            if len(self.archives) > 4:
                self.archives.popitem(last=False)[1].close()
        self.archives.move_to_end(key)
        return self.archives[key]

    @contextmanager
    def open(self, root, item):
        path, info = validate_item(root, item)
        if item.zip_entry:
            archive = self.archive(path, info)
            member = archive.getinfo(item.zip_entry)
            if member.CRC != item.crc or member.file_size != item.size:
                raise OSError(f"스캔 후 ZIP 항목이 변경되었습니다: {item.identity}")
            with archive.open(member) as stream:
                yield stream
        else:
            with path.open("rb") as stream:
                yield stream
        final = path.lstat()
        if (file_stamp(final), final.st_dev, final.st_ino, final.st_ctime_ns) != (
                file_stamp(info), info.st_dev, info.st_ino, info.st_ctime_ns):
            raise OSError(f"읽는 동안 파일이 변경되었습니다: {item.rel_path}")


@contextmanager
def open_item(root, item):
    with ItemReader() as reader, reader.open(root, item) as stream:
        yield stream
