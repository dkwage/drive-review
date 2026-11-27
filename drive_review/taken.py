"""Read capture timestamps without decoding image pixels or video samples."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
import re
from struct import Struct, unpack
from zipfile import ZipExtFile

READ_LIMIT = 4 * 1024 * 1024
MOVIE_EPOCH = datetime(1904, 1, 1)
VIDEOS = {"mp4", "mov", "m4v", "3gp"}
HEIF = {"heic", "heif", "avif"}
TIFF = set("tif tiff cr2 nef nrw arw sr2 dng orf rw2 pef srw".split())
JPEG = {"jpg", "jpeg", "jpe", "jif", "jfif"}
SUPPORTED = JPEG | TIFF | HEIF | VIDEOS | {"cr3", "raf", "png", "apng", "webp"}
CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
EXIF_PREFIX = b"Exif\0\0"
DATE_PATTERN = re.compile(
    r"([0-9]{4})([-:])([0-9]{2})\2([0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:[.,][0-9]+)?\s*(Z|[+-][0-9]{2}:?[0-9]{2})?"
)


@dataclass(frozen=True, slots=True)
class Taken:
    text: str
    offset: str | None
    sort_key: int

    @property
    def label(self):
        return f"{self.text} {self.offset}" if self.offset else self.text


def _date(value, offset=None):
    match = DATE_PATTERN.fullmatch(value.strip("\0 \t\r\n"))
    if not match:
        return None
    year, _, month, day, hour, minute, second, embedded = match.groups()
    try:
        wall = datetime(*map(int, (year, month, day, hour, minute, second)))
    except ValueError:
        return None
    return _timestamp(wall, embedded or offset)


def _timestamp(wall, offset):
    # Use a common numeric scale without assigning a time zone to wall-clock values.
    seconds = wall.toordinal() * 86400 + wall.hour * 3600 + wall.minute * 60 + wall.second
    normalized = None
    if offset == "Z":
        normalized = "+00:00"
    elif offset and re.fullmatch(r"[+-][0-9]{2}:?[0-9]{2}", offset):
        hours, minutes = int(offset[1:3]), int(offset[-2:])
        if hours < 24 and minutes < 60:
            normalized = f"{offset[0]}{hours:02}:{minutes:02}"
            seconds -= (1 if offset[0] == "+" else -1) * (hours * 3600 + minutes * 60)
    return Taken(wall.isoformat(sep=" "), normalized, seconds)


class _Invalid(ValueError):
    pass


def _check(valid):
    if not valid:
        raise _Invalid("Invalid or out-of-bounds capture metadata")


class _Reader:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = READ_LIMIT
        self.prefix = None
        self.size = None
        # ZipExtFile.seek() can inflate every intervening byte.
        if isinstance(stream, ZipExtFile) or not getattr(stream, "seekable", lambda: False)():
            self.prefix = bytearray()
        else:
            self.size = stream.seek(0, 2)

    def at(self, offset, count, *, short=False):
        _check(offset >= 0 and count >= 0)
        if self.prefix is not None:
            _check(offset + count <= READ_LIMIT)
            while len(self.prefix) < offset + count and self.size is None:
                block = self.stream.read(min(65536, offset + count - len(self.prefix)))
                if not block:
                    self.size = len(self.prefix)
                    break
                self.prefix.extend(block)
            data = bytes(self.prefix[offset:offset + count])
        else:
            if self.size is not None:
                _check(short or offset + count <= self.size)
                count = min(count, max(0, self.size - offset))
            _check(count <= self.remaining)
            self.remaining -= count
            self.stream.seek(offset)
            data = self.stream.read(count)
        _check(short or len(data) == count)
        return data

    def section(self, start, end):
        return _Section(self, start, end)


class _Section:
    """A bounded view with offsets relative to the start of a metadata block."""

    def __init__(self, reader, start, end):
        _check(0 <= start <= end and (reader.size is None or end <= reader.size))
        self.reader = reader
        self.start = start
        self.size = end - start

    def at(self, offset, count):
        _check(0 <= offset <= offset + count <= self.size)
        return self.reader.at(self.start + offset, count)


class _Extents:
    """A HEIF item can store one logical TIFF stream across several spans."""

    def __init__(self, reader, spans):
        self.reader = reader
        self.spans = spans
        self.ends = [0]
        for start, size in spans:
            _check(start >= 0 and (reader.size is None or start + size <= reader.size))
            self.ends.append(self.ends[-1] + size)
        self.size = self.ends[-1]

    def at(self, offset, count):
        _check(0 <= offset <= offset + count <= self.size)
        chunks = []
        while count:
            index = bisect_right(self.ends, offset) - 1
            start, _ = self.spans[index]
            length = min(count, self.ends[index + 1] - offset)
            chunks.append(self.reader.at(start + offset - self.ends[index], length))
            offset += length
            count -= length
        return b"".join(chunks)


def _uint(data):
    return int.from_bytes(data, "big")


DATE_TAGS = ((0x9003, (0x9011, 0x9010)), (0x9004, (0x9012, 0x9010)), (0x0132, (0x9010,)))
TEXT_TAGS = {tag for date, offsets in DATE_TAGS for tag in (date, *offsets)}


def _tag_date(tags):
    for tag, offsets in DATE_TAGS:
        for value in tags.get(tag, ()):
            taken = _date(value)
            if taken is None:
                continue
            if taken.offset is None:
                for offset_tag in offsets:
                    for offset in tags.get(offset_tag, ()):
                        if (with_offset := _date(value, offset)).offset is not None:
                            return with_offset
            return taken
    return None


def _tiff(view):
    signature = view.at(0, 4)
    order = {b"II": "<", b"MM": ">"}.get(signature[:2])
    _check(order is not None)
    magic = unpack(order + "H", signature[2:])[0]
    _check(magic in (42, 43, 0x4F52, 0x5352, 0x55))  # TIFF, BigTIFF, ORF, RW2
    big = magic == 43
    header_size = 16 if big else 8
    width = 8 if big else 4
    offset_struct = Struct(order + ("Q" if big else "I"))
    count_struct = Struct(order + ("Q" if big else "H"))
    entry_struct = Struct(order + ("HHQ8s" if big else "HHI4s"))
    if big:
        _check(unpack(order + "HH", view.at(4, 4)) == (8, 0))
    first = offset_struct.unpack(view.at(header_size - width, width))[0]
    tags = {}
    visited = set()
    active = set()

    def visit(offset, depth):
        if offset == 0:
            return
        _check(offset not in active and depth < 64 and offset >= header_size)
        if offset in visited:
            return
        _check(len(visited) < 256)
        visited.add(offset)
        active.add(offset)
        count = count_struct.unpack(view.at(offset, count_struct.size))[0]
        _check(count <= 4096)
        start = offset + count_struct.size
        entries = view.at(start, count * entry_struct.size)
        next_offset = offset_struct.unpack(view.at(start + len(entries), width))[0]
        children = []
        for tag, kind, length, field in entry_struct.iter_unpack(entries):
            if tag in TEXT_TAGS and kind == 2 and 0 < length <= 128:
                raw = field[:length] if length <= width else view.at(offset_struct.unpack(field)[0], length)
                try:
                    value = raw.split(b"\0", 1)[0].decode("ascii").strip()
                except UnicodeError:
                    continue
                tags.setdefault(tag, []).append(value)
            elif tag in (0x8769, 0x014A) and kind in (4, 13, 16, 18):  # ExifIFD, SubIFDs
                pointer = Struct(order + ("I" if kind in (4, 13) else "Q"))
                _check(length <= 256)
                size = length * pointer.size
                raw = field[:size] if size <= width else view.at(offset_struct.unpack(field)[0], size)
                links = [value for (value,) in pointer.iter_unpack(raw)]
                if tag == 0x8769:
                    children[0:0] = links
                else:
                    children.extend(links)
        for child in children:
            visit(child, depth + 1)
        visit(next_offset, depth + 1)
        active.remove(offset)

    visit(first, 0)
    return tags


def _exif(reader, start, end):
    if end - start >= len(EXIF_PREFIX) and reader.at(start, len(EXIF_PREFIX)) == EXIF_PREFIX:
        start += len(EXIF_PREFIX)
    return _tag_date(_tiff(reader.section(start, end)))


def _jpeg(reader, start, end):
    _check(reader.at(start, 2) == b"\xff\xd8")
    pos = start + 2
    while end is None or pos + 2 <= end:
        marker = reader.at(pos, 2)
        _check(marker[0] == 0xFF and marker[1] != 0)
        if marker[1] == 0xFF:
            pos += 1
            continue
        if marker[1] in (0xDA, 0xD9):  # scan data or end of image
            return None
        pos += 2
        if marker[1] == 1 or 0xD0 <= marker[1] <= 0xD7:
            continue
        _check(end is None or pos + 2 <= end)
        length = _uint(reader.at(pos, 2))
        _check(length >= 2 and (end is None or pos + length <= end))
        if marker[1] == 0xE1 and length >= 8 and reader.at(pos + 2, 6) == EXIF_PREFIX:
            return _exif(reader, pos + 8, pos + length)
        pos += length
    return None


def _png(reader):
    _check(reader.at(0, 8) == b"\x89PNG\r\n\x1a\n")
    pos = 8
    while True:
        length, kind = unpack(">I4s", reader.at(pos, 8))
        start = pos + 8
        end = start + length
        _check(reader.size is None or end + 4 <= reader.size)
        if kind == b"eXIf":
            return _exif(reader, start, end)
        if kind == b"IEND":
            return None
        pos = end + 4  # CRC


def _webp(reader):
    riff, size, webp = unpack("<4sI4s", reader.at(0, 12))
    end = size + 8
    _check(riff == b"RIFF" and webp == b"WEBP" and end >= 12)
    _check(reader.size is None or end <= reader.size)
    pos = 12
    while pos + 8 <= end:
        kind, length = unpack("<4sI", reader.at(pos, 8))
        start = pos + 8
        pos = start + length + (length & 1)
        _check(pos <= end)
        if kind == b"EXIF":
            return _exif(reader, start, start + length)
    return None


def _boxes(reader, start, end):
    while end is None or start < end:
        _check(end is None or start + 8 <= end)
        header = reader.at(start, 8, short=end is None)
        if not header and end is None:
            return
        _check(len(header) == 8)
        size, kind = unpack(">I4s", header)
        payload = start + 8
        if size == 1:
            _check(end is None or payload + 8 <= end)
            size = _uint(reader.at(payload, 8))
            payload += 8
        elif size == 0:
            if end is None:
                return  # an unknown-length stream cannot locate later sibling boxes
            size = end - start
        tail = start + size
        _check(payload <= tail and (end is None or tail <= end))
        yield kind, payload, tail
        start = tail


def _box_sections(reader, start, end, types):
    return {kind: (head, tail) for kind, head, tail in _boxes(reader, start, end) if kind in types}


def _movie_header(reader, start, end):
    _check(start + 4 <= end)
    version = reader.at(start, 4)[0]
    if version not in (0, 1):
        return None
    width = 8 if version else 4
    _check(start + 4 + width <= end)
    seconds = _uint(reader.at(start + 4, width))
    wall = MOVIE_EPOCH + timedelta(seconds=seconds)
    return _timestamp(wall, "Z") if wall.year != 1904 else None


def _quicktime_metadata(reader, start, end):
    _check(start + 8 <= end)
    if reader.at(start, 4) == b"\0\0\0\0":
        start += 4  # ISO full box. Older QuickTime meta starts directly with a child atom.
    children = _box_sections(reader, start, end, (b"keys", b"ilst"))
    if b"keys" not in children or b"ilst" not in children:
        return None
    head, tail = children[b"keys"]
    _check(head + 8 <= tail)
    count = _uint(reader.at(head + 4, 4))
    target = b"com.apple.quicktime.creationdate"
    start = head + 8
    indexes = set()
    _check(count <= (tail - start) // 8)
    for index in range(1, count + 1):
        _check(start + 8 <= tail)
        size, namespace = unpack(">I4s", reader.at(start, 8))
        _check(8 <= size <= tail - start)
        if (namespace == b"mdta" and size == len(target) + 8
                and reader.at(start + 8, len(target)) == target):
            indexes.add(index)
        start += size
    if not indexes:
        return None
    for kind, head, tail in _boxes(reader, *children[b"ilst"]):
        if _uint(kind) not in indexes:
            continue
        for kind, start, end in _boxes(reader, head, tail):
            if kind == b"data" and 8 < end - start <= 256:
                body = reader.at(start, end - start)
                encoding = {0: "utf-8", 1: "utf-8", 2: "utf-16-be"}.get(_uint(body[:4]))
                if encoding:
                    taken = _date(body[8:].decode(encoding).lstrip("\ufeff"))
                    if taken and taken.text[:4] != "1904":
                        return taken
    return None


def _movie(reader, start, end):
    fallback = None
    for kind, head, tail in _boxes(reader, start, end):
        if kind == b"mvhd":
            fallback = head, tail
        elif kind == b"meta":
            if taken := _quicktime_metadata(reader, head, tail):
                return taken
        elif kind == b"udta":
            for child, first, last in _boxes(reader, head, tail):
                if child == b"meta" and (taken := _quicktime_metadata(reader, first, last)):
                    return taken
    return _movie_header(reader, *fallback) if fallback else None


def _canon(reader, start, end, nested=False):
    tags = {}
    for kind, head, tail in _boxes(reader, start, end):
        if kind in (b"CMT1", b"CMT2"):
            found = _tiff(reader.section(head, tail))
        elif kind == b"uuid" and not nested and tail - head >= 16 and reader.at(head, 16) == CANON_UUID:
            found = _canon(reader, head + 16, tail, nested=True)
        else:
            continue
        for tag, values in found.items():
            tags.setdefault(tag, []).extend(values)
    return tags


def _heif_exif_ids(reader, start, end):
    _check(start + 6 <= end)
    version = reader.at(start, 4)[0]
    _check(version in (0, 1))
    head = start + (8 if version else 6)
    ids = set()
    for kind, pos, tail in _boxes(reader, head, end):
        if kind != b"infe":
            continue
        _check(pos + 4 <= tail)
        version = reader.at(pos, 4)[0]
        if version in (2, 3):
            width = 2 if version == 2 else 4
            _check(pos + 10 + width <= tail)
            data = reader.at(pos + 4, width + 6)
            if data[width + 2:] == b"Exif":
                ids.add(_uint(data[:width]))
    return ids


def _heif_locations(reader, start, end, exif_ids, idat):
    pos = start

    def number(width):
        nonlocal pos
        _check(pos + width <= end)
        if not width:
            return 0
        value = _uint(reader.at(pos, width))
        pos += width
        return value

    version = number(4) >> 24
    _check(version in (0, 1, 2))
    widths = number(2)
    offset, length, base, index = (widths >> shift & 15 for shift in (12, 8, 4, 0))
    _check(max(offset, length, base, index) <= 8)
    if version == 0:
        index = 0
    id_width = 4 if version == 2 else 2
    count = number(id_width)
    _check(count <= (end - pos) // (id_width + 4))
    for _ in range(count):
        item_id = number(id_width)
        method = (number(2) & 15) if version else 0
        reference = number(2)
        base_offset = number(base)
        extent_count = number(2)
        extent_width = index + offset + length
        _check(pos + extent_count * extent_width <= end)
        if item_id not in exif_ids or reference or method not in (0, 1) or (method == 1 and idat is None):
            pos += extent_count * extent_width
            continue
        _check(extent_count <= 1024)
        origin, bound = idat if method == 1 else (0, reader.size)
        spans = []
        for _ in range(extent_count):
            number(index)
            start = origin + base_offset + number(offset)
            size = number(length)
            if size == 0:
                _check(bound is not None and start <= bound)
                size = bound - start
            _check(bound is None or start + size <= bound)
            spans.append((start, size))
        if spans:
            yield _Extents(reader, spans)


def _heif(reader, start, end):
    _check(start + 4 <= end)
    children = _box_sections(reader, start + 4, end, (b"iinf", b"iloc", b"idat"))
    if b"iinf" not in children or b"iloc" not in children:
        return None
    ids = _heif_exif_ids(reader, *children[b"iinf"])
    if ids:
        for item in _heif_locations(reader, *children[b"iloc"], ids, children.get(b"idat")):
            offset = 4 + _uint(item.at(0, 4))
            if taken := _tag_date(_tiff(_Section(item, offset, item.size))):
                return taken
    return None


def _extension(name):
    return PurePosixPath(name).suffix.lower().lstrip(".")


def supports_taken(name):
    return _extension(name) in SUPPORTED


def read_taken(stream, name) -> Taken | None:
    """Return a capture timestamp using at most 4 MiB of reads from the stream."""
    try:
        ext = _extension(name)
        if ext not in SUPPORTED:
            return None
        reader = _Reader(stream)
        if ext in JPEG:
            return _jpeg(reader, 0, reader.size)
        if ext in TIFF:
            return _tag_date(_tiff(reader if reader.size is None else reader.section(0, reader.size)))
        if ext in ("png", "apng"):
            return _png(reader)
        if ext == "webp":
            return _webp(reader)
        if ext == "raf":
            _check(reader.at(0, 16) == b"FUJIFILMCCD-RAW ")
            offset, size = unpack(">II", reader.at(84, 8))
            _check(offset >= 92 and size >= 2 and (reader.size is None or offset + size <= reader.size))
            return _jpeg(reader, offset, offset + size)
        for kind, start, end in _boxes(reader, 0, reader.size):
            if ext in VIDEOS and kind == b"moov":
                return _movie(reader, start, end)
            if ext == "cr3" and kind == b"moov":
                return _tag_date(_canon(reader, start, end))
            if ext in HEIF and kind == b"meta":
                return _heif(reader, start, end)
    except Exception:
        return None
    return None
