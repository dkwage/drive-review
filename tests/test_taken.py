from datetime import datetime
from io import BytesIO
from pathlib import Path
from contextlib import closing
from sqlite3 import connect
from struct import pack
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, features
from pillow_heif import from_pillow

from drive_review.capture import CaptureDates, read_item_taken
from drive_review.drives import identify_drive
from drive_review.index import Index
from drive_review.scan import scan
from drive_review.taken import CANON_UUID, read_taken
from drive_review.zipsource import ItemReader


def photo(format="JPEG", *, taken="2021:11:14 10:13:12", offset="-05:30", camera=False):
    image = Image.new("RGB", (20, 20))
    exif = image.getexif()
    exif[0x0132] = "2024:01:02 03:04:05"
    exif[0x8769] = {0x9003: taken, 0x9004: "2022:01:01 00:00:00", 0x9011: offset}
    if camera:
        exif[0x010F], exif[0x0110] = "Canon", "EOS R6"
        exif[0x8769][0x9291] = "032"
    output = BytesIO()
    if format == "HEIF":
        from_pillow(image).save(output, exif=exif.tobytes())
    else:
        image.save(output, format=format, exif=exif)
    return output.getvalue()


def tiff(order, tags, next_ifd=0, magic=42):
    start = 8
    values = bytearray()
    entries = bytearray()
    for tag, value in tags.items():
        text = value.encode("ascii") + b"\0"
        entries += pack(order + "HHI", tag, 2, len(text))
        if len(text) <= 4:
            entries += text.ljust(4, b"\0")
        else:
            entries += pack(order + "I", start + 2 + 12 * len(tags) + 4 + len(values))
            values += text
    return (pack(order + "2sHIH", b"II" if order == "<" else b"MM", magic, start, len(tags))
            + entries + pack(order + "I", next_ifd) + values)


def box(kind, body):
    return pack(">I4s", len(body) + 8, kind) + body


def mvhd(version, date):
    seconds = int((date - datetime(1904, 1, 1)).total_seconds())
    return box(b"mvhd", pack(">B3x" + ("Q" if version else "I"), version, seconds))


class ReadTakenTests(unittest.TestCase):
    def test_reads_pair_metadata_from_exif_and_raw(self):
        fields = {0x9003: "2021:11:14 10:13:12", 0x9011: "-05:30",
                  0x9291: "032", 0x010F: "Canon", 0x0110: "EOS R6"}
        jpeg = read_taken(BytesIO(photo(camera=True)), "photo.jpg")
        self.assertEqual((jpeg.subsec, jpeg.make, jpeg.model), ("032", "Canon", "EOS R6"))
        self.assertEqual(read_taken(BytesIO(tiff(">", fields)), "photo.CR2"), jpeg)

    def test_pillow_jpeg_prefers_original_and_uses_offset_for_sort(self):
        shifted = read_taken(BytesIO(photo()), "FAMILY.JPG")
        local = read_taken(BytesIO(photo(offset="")), "family.jpg")
        self.assertEqual(shifted.text, "2021-11-14 10:13:12")
        self.assertEqual(shifted.offset, "-05:30")
        self.assertEqual(shifted.label, "2021-11-14 10:13:12 -05:30")
        self.assertIsNone(local.offset)
        self.assertEqual(local.label, shifted.text)
        self.assertEqual(shifted.sort_key - local.sort_key, 5 * 3600 + 30 * 60)

    def test_tiff_endianness_and_tag_fallbacks(self):
        fields = {0x9003: "0000:00:00 00:00:00", 0x9004: "2002:03:04 05:06:07",
                  0x0132: "2003:03:04 05:06:07", 0x9011: "+03:00", 0x9012: "+02:30", 0x9010: "+02:00"}
        for order in ("<", ">"):
            with self.subTest(order=order):
                date = read_taken(BytesIO(tiff(order, fields)), "photo.tiff")
                self.assertEqual(date.label, "2002-03-04 05:06:07 +02:30")
        fallback = read_taken(BytesIO(tiff(">", {0x0132: fields[0x0132], 0x9010: fields[0x9010]})), "photo.sr2")
        self.assertEqual(fallback.label, "2003-03-04 05:06:07 +02:00")
        self.assertIsNone(read_taken(BytesIO(tiff("<", {0x9003: "   "})), "empty.nef"))
        for name, magic in (("camera.orf", 0x4F52), ("camera.rw2", 0x55)):
            date = read_taken(BytesIO(tiff("<", {0x9004: fields[0x9004]}, magic=magic)), name)
            self.assertEqual(date.text, "2002-03-04 05:06:07")

    def test_malformed_ifds_and_garbage(self):
        loop = tiff("<", {0x9003: "2002:03:04 05:06:07"}, next_ifd=8)
        broken = bytearray(tiff(">", {0x9003: "2002:03:04 05:06:07"}))
        broken[18:22] = pack(">I", 0xFFFF_FFFF)
        for name, data in (("loop.tif", loop), ("corrupted.pef", broken), ("garbage.jpg", b"not a photo"),
                           ("garbage.mov", pack(">I4s", 4, b"moov"))):
            with self.subTest(name=name):
                self.assertIsNone(read_taken(BytesIO(data), name))

    def test_image_containers(self):
        formats = [("PNG", "png"), ("HEIF", "heic")]
        formats += [(name.upper(), name) for name in ("webp", "avif") if features.check(name)]
        for format, ext in formats:
            with self.subTest(ext=ext):
                self.assertEqual(read_taken(BytesIO(photo(format)), "photo." + ext).label,
                                 "2021-11-14 10:13:12 -05:30")

    def test_heif_item_spans_multiple_idat_extents(self):
        exif = pack(">I", 6) + b"Exif\0\0" + tiff("<", {0x9003: "2020:01:01 08:09:10"})
        first, second = exif[:19], exif[19:]
        info = box(b"iinf", bytes(4) + pack(">H", 1) + box(b"infe", pack(">B3xHH4s", 2, 7, 0, b"Exif")))
        locations = box(b"iloc", pack(">B3x6H4I", 1, 0x4400, 1, 7, 1, 0, 2, 0, len(first),
                                      len(first) + 5, len(second)))
        data = box(b"idat", first + b"other" + second)
        heif = box(b"meta", bytes(4) + info + locations + data)
        self.assertEqual(read_taken(BytesIO(heif), "image.heif").label, "2020-01-01 08:09:10")

    def test_canon_boxes_and_fuji_preview(self):
        first = box(b"CMT1", tiff("<", {0x0132: "2003:03:04 05:06:07"}))
        second = box(b"CMT2", tiff(">", {0x9003: "2002:03:04 05:06:07", 0x9011: "+00:30"}))
        cr3 = box(b"ftyp", b"crx \0\0\0\0") + box(b"moov", box(b"uuid", CANON_UUID + first + second))
        self.assertEqual(read_taken(BytesIO(cr3), "photo.cr3").label, "2002-03-04 05:06:07 +00:30")
        preview = photo()
        raf = b"FUJIFILMCCD-RAW ".ljust(84, b"\0") + pack(">II", 92, len(preview)) + preview
        self.assertEqual(read_taken(BytesIO(raf), "photo.raf").label, "2021-11-14 10:13:12 -05:30")

    def test_mp4_movie_headers_v0_and_v1(self):
        for version, year in ((0, 2020), (1, 2055)):
            with self.subTest(version=version):
                movie = box(b"moov", mvhd(version, datetime(year, 9, 8, 7, 6, 5)))
                date = read_taken(BytesIO(box(b"ftyp", b"isom\0\0\0\0") + movie), "movie.mp4")
                self.assertEqual(date.label, f"{year}-09-08 07:06:05 +00:00")
                self.assertIsNone(read_taken(BytesIO(movie), "movie.mkv"))
        for date in (datetime(1904, 1, 1), datetime(1904, 1, 2)):
            self.assertIsNone(read_taken(BytesIO(box(b"moov", mvhd(0, date))), "epoch.mov"))

    def test_quicktime_creationdate_preferred_over_movie_header(self):
        keys = box(b"keys", pack(">II", 0, 2) + box(b"mdta", b"unrelated")
                   + box(b"mdta", b"com.apple.quicktime.creationdate"))
        entry = box(b"data", pack(">II", 1, 0) + b"2023-09-01T22:30:32.125+0800")
        ilst = box(b"ilst", box(pack(">I", 2), entry))
        header = mvhd(0, datetime(2022, 1, 1))
        for prefix in (b"", bytes(4)):
            with self.subTest(meta_full_box=bool(prefix)):
                movie = box(b"moov", header + box(b"udta", box(b"meta", prefix + keys + ilst)))
                self.assertEqual(read_taken(BytesIO(movie), "movie.mov").label, "2023-09-01 22:30:32 +08:00")

    def test_zip_entry_uses_item_reader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with ZipFile(root / "album.zip", "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("folder/photo.jpg", photo())
            item = next(item for item in scan(root).items if item.zip_entry)
            with ItemReader() as reader, reader.open(root, item) as source:
                self.assertEqual(read_taken(source, item.name).label, "2021-11-14 10:13:12 -05:30")

    def test_seeks_past_media_and_caps_reads_from_sequential_streams(self):
        class CountingStream(BytesIO):
            bytes_read = 0

            def read(self, count):
                data = super().read(count)
                self.bytes_read += len(data)
                return data

        class SequentialStream(CountingStream):
            def seekable(self):
                return False

            def seek(self, *_args):
                raise AssertionError("A sequential stream must not be seeked")

        movie = box(b"mdat", bytes(5000)) + box(b"moov", mvhd(0, datetime(2020, 1, 1)))
        random = CountingStream(movie)
        self.assertEqual(read_taken(random, "movie.mp4").text, "2020-01-01 00:00:00")
        self.assertLess(random.bytes_read, 80)
        with patch("drive_review.taken.READ_LIMIT", 80):
            sequential = SequentialStream(movie)
            self.assertIsNone(read_taken(sequential, "movie.mp4"))
            self.assertLessEqual(sequential.bytes_read, 80)
            repeated = CountingStream(box(b"free", b"") * 50)
            self.assertIsNone(read_taken(repeated, "movie.mp4"))
            self.assertEqual(repeated.bytes_read, 80)
        self.assertEqual(read_taken(SequentialStream(photo()), "photo.jpg").offset, "-05:30")


class CaptureCacheTests(unittest.TestCase):
    def test_legacy_capture_rows_are_refreshed_with_camera_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "old.sqlite"
            (root / "photo.jpg").write_bytes(photo(camera=True))
            drive = identify_drive(root)
            items = scan(root).items
            with connect(path) as old:
                old.execute("""CREATE TABLE capture_dates (
                    drive_id TEXT, rel_path TEXT, zip_entry TEXT, stamp TEXT, text TEXT,
                    utc_offset TEXT, sort_key INTEGER, PRIMARY KEY (drive_id, rel_path, zip_entry, stamp))""")
                old.execute("INSERT INTO capture_dates VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (drive.id, *items[0].key, items[0].stamp, "2021-11-14 10:13:12", "-05:30", 1))
            capture = CaptureDates(Index(path), drive, items)
            self.assertEqual(capture.cached, {})
            capture.read_all(items, Event(), lambda *_: None)
            cached = CaptureDates(Index(path), drive, items).cached[items[0].key]
            self.assertEqual((cached.subsec, cached.make, cached.model), ("032", "Canon", "EOS R6"))

    def test_cache_migrates_existing_index_and_reuses_positive_and_negative_results(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            path = base / "existing.sqlite"
            with closing(connect(path)) as old, old:
                old.execute("CREATE TABLE drives (id TEXT PRIMARY KEY, label TEXT NOT NULL, serial TEXT NOT NULL, root TEXT NOT NULL)")
                old.execute("INSERT INTO drives VALUES ('old', 'Old drive', '', '/old')")
            (root / "a.jpg").write_bytes(photo())
            (root / "b.jpg").write_bytes(b"No metadata")
            drive = identify_drive(root)
            items = scan(root).items
            index = Index(path)
            with index.connect() as db:
                self.assertEqual(db.execute("SELECT label FROM drives WHERE id='old'").fetchone(), ("Old drive",))
            capture = CaptureDates(index, drive, items)
            progress = []
            capture.read_all(items, Event(), lambda *values: progress.append(values))
            self.assertEqual(progress[-1][1:], (2, 2))
            expected = progress[-1][0]
            self.assertEqual(expected[("a.jpg", "")].text, "2021-11-14 10:13:12")
            self.assertIsNone(expected[("b.jpg", "")])
            self.assertEqual(index.cached_dates("different drive", items), {})

            capture = CaptureDates(Index(path), drive, items)
            with ItemReader() as reader, patch("drive_review.capture.read_taken", side_effect=AssertionError("cache miss")):
                self.assertEqual({item.key: capture.read(item, reader, None) for item in items}, expected)
            (root / "b.jpg").write_bytes(photo(taken="2020:01:02 12:00:00"))
            changed = scan(root).items
            self.assertEqual(index.cached_dates(drive.id, changed), {("a.jpg", ""): expected[("a.jpg", "")]})
            capture = CaptureDates(index, drive, changed)
            self.assertEqual(capture.cached, {("a.jpg", ""): expected[("a.jpg", "")]})
            capture.read_all(changed, Event(), lambda *_: None)
            self.assertEqual(index.cached_dates(drive.id, changed)[("b.jpg", "")].text, "2020-01-02 12:00:00")

    def test_cancel_keeps_completed_dates_for_next_pass(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            for name in ("a.jpg", "b.jpg"):
                (root / name).write_bytes(photo())
            drive = identify_drive(root)
            items = sorted(scan(root).items, key=lambda item: item.name)
            index = Index(base / "index.sqlite")
            stop = Event()
            capture = CaptureDates(index, drive, items)

            def cancel_after_read(*args):
                date = read_item_taken(*args)
                stop.set()
                return date

            with patch("drive_review.capture.read_item_taken", side_effect=cancel_after_read) as read:
                with self.assertRaises(InterruptedError):
                    capture.read_all(items, stop, lambda *_: None)
                self.assertEqual(read.call_count, 1)
            resumed = CaptureDates(index, drive, items)
            self.assertEqual(list(resumed.cached), [("a.jpg", "")])
            resumed.read_all(items, Event(), lambda *_: None)
            self.assertEqual(len(index.cached_dates(drive.id, items)), 2)
