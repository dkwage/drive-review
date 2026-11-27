import base64
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from drive_review.drives import identify_drive
from drive_review.scan import scan
from drive_review.thumbs import CHUNK, MAX_AUTO_MEMBER, XMP_WINDOW, ThumbnailCache, design_thumbnail, xmp_thumbnail


def xmp_packet():
    buffer = BytesIO()
    Image.new("RGB", (30, 12), "blue").save(buffer, format="JPEG")
    jpeg = base64.b64encode(buffer.getvalue())
    return (b"<xmpGImg:image>" + b"&#xA;\r\n".join(jpeg[i:i + 70] for i in range(0, len(jpeg), 70))
            + b"</xmpGImg:image>")


class DesignThumbnailTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_extracts_xmp_from_ai_and_indesign_like_files(self):
        for extension in ("ai", "indd", "psb"):
            with self.subTest(extension=extension):
                path = self.root / f"sample.{extension}"
                path.write_bytes(b"binary design data\0" + xmp_packet())
                image = design_thumbnail(path, 60)
                self.assertEqual(image.size, (30, 12))
                self.assertGreater(image.getpixel((0, 0))[2], 240)

    def test_extracts_xmp_from_psd_image_resource(self):
        path = self.root / "sample.psd"
        header = struct.pack(">4sH6xHIIHH", b"8BPS", 1, 3, 1, 1, 8, 3)
        xmp = xmp_packet()
        xmp_resource = b"8BIM" + struct.pack(">H2xI", 1060, len(xmp)) + xmp + b"\x00" * (len(xmp) % 2)
        path.write_bytes(header + struct.pack(">II", 0, len(xmp_resource)) + xmp_resource
                         + struct.pack(">IH3B", 0, 0, 255, 0, 0))
        with Image.open(path) as original:
            self.assertEqual(original.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(design_thumbnail(path, 60).size, (30, 12))

    def test_bounded_search_at_tail_and_across_read_blocks(self):
        class CountingStream:
            def __init__(self, source):
                self.source = source
                self.total = 0

            def seek(self, *args):
                return self.source.seek(*args)

            def read(self, count):
                self.assert_read_size(count)
                result = self.source.read(count)
                self.total += len(result)
                return result

            @staticmethod
            def assert_read_size(count):
                if not 0 <= count <= CHUNK:
                    raise AssertionError(f"Unexpected read size {count}")

        path = self.root / "sparse.ai"
        with path.open("w+b") as source:
            length = 5 * XMP_WINDOW
            source.seek(length - 1)
            source.write(b"\x00")
            # Put the opening tag across two adjacent reads in the trailing window.
            source.seek(length - XMP_WINDOW + CHUNK - 5)
            source.write(xmp_packet())
            stream = CountingStream(source)
            self.assertEqual(xmp_thumbnail(stream, 60).size, (30, 12))
            self.assertLessEqual(stream.total, 2 * XMP_WINDOW)
            self.assertLess(stream.total, length)

    def test_eps_tiff_preview(self):
        path = self.root / "sample.eps"
        buffer = BytesIO()
        Image.new("RGB", (30, 12), "green").save(buffer, format="TIFF")
        tiff = buffer.getvalue()
        path.write_bytes(struct.pack("<4sIIIIIIH", b"\xc5\xd0\xd3\xc6", 0, 0, 0, 0, 30, len(tiff), 0)
                         + tiff)
        self.assertEqual(design_thumbnail(path, 60).size, (30, 12))

    def test_compressed_idml_metadata_thumbnail(self):
        path = self.root / "sample.idml"
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("META-INF/metadata.xml", xmp_packet())
        self.assertEqual(design_thumbnail(path, 60).size, (30, 12))

    def test_zip_design_thumbnails_respect_auto_size_limit(self):
        with ZipFile(self.root / "archive.zip", "w") as archive:
            archive.writestr("file.indd", xmp_packet())
        item = next(item for item in scan(self.root).items if item.zip_entry)
        drive = identify_drive(self.root)
        cache = ThumbnailCache(self.root / "cache")
        data = cache.get(drive, item, 60)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (30, 12))
        self.assertIsNone(cache.get(drive, replace(item, size=MAX_AUTO_MEMBER + 1), 60))
