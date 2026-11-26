from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
import zipfile

from drive_review.items import category_for
from drive_review.scan import scan
from drive_review.zipsource import open_item


class ScanTests(unittest.TestCase):
    def test_system_item_filter_and_categories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["projects/frame.CR3", "projects/poster.psd", "projects/cut.prproj", "clip.mov",
                     "System Volume Information/a", "$RECYCLE.BIN/b", ".Trashes/c", "projects/._a",
                     "projects/.DS_Store", ".Spotlight-V100/d", ".fseventsd/e", "desktop.ini",
                     "Thumbs.db", "autorun.inf", "Seagate Toolkit Installer.exe", "_drive_review/flags.csv"]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"file")
            self.assertEqual({item.rel_path for item in scan(root).items}, set(names[:4]))
            found = scan(root, show_hidden=True)
            self.assertEqual({item.rel_path for item in found.items}, set(names[:-1]))
            self.assertEqual(category_for(names[0]), "Image")
            self.assertEqual(category_for(names[1]), "Design")
            self.assertEqual(category_for(names[2]), "Design")
            self.assertEqual(category_for(names[3]), "Video")

    def test_zip64_directories_and_single_member_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"zip64 payload" * 12
            with patch.object(zipfile, "ZIP64_LIMIT", 32):
                with zipfile.ZipFile(root / "archive.zip", "w", allowZip64=True) as archive:
                    with archive.open("folder/image.jpg", "w", force_zip64=True) as output:
                        output.write(data)
                    archive.writestr("folder/._image.jpg", b"junk")
                    archive.writestr("empty/", b"")
            self.assertIn(b"PK\x06\x06", (root / "archive.zip").read_bytes())
            result = scan(root)
            self.assertEqual(result.errors, [])
            self.assertEqual(result.archives, {"archive.zip"})
            self.assertIn("archive.zip/folder", result.directories)
            self.assertIn("archive.zip/empty", result.directories)
            self.assertEqual([item.identity for item in result.items],
                             ["archive.zip", "archive.zip::folder/image.jpg"])
            with open_item(root, result.items[1]) as stream:
                self.assertEqual(stream.read(), data)
            self.assertFalse((root / "folder").exists())

    def test_corrupt_encrypted_and_unsafe_archives_are_plain_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.zip").write_bytes(b"broken")
            with zipfile.ZipFile(root / "unsafe.zip", "w") as archive:
                archive.writestr("../outside.txt", b"content")
            encrypted = root / "encrypted.zip"
            with zipfile.ZipFile(encrypted, "w") as archive:
                archive.writestr("secret.txt", b"content")
            data = bytearray(encrypted.read_bytes())
            central_offset = data.index(b"PK\x01\x02")
            struct.pack_into("<H", data, central_offset + 8, 1)
            encrypted.write_bytes(data)
            result = scan(root)
            self.assertEqual(result.archives, set())
            self.assertEqual(len(result.errors), 3)
            self.assertTrue(all(item.warning and not item.zip_entry for item in result.items))

    def test_cancel_and_disconnected_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stop = Event()
            stop.set()
            with self.assertRaises(InterruptedError):
                scan(root, stop=stop)
            with self.assertRaises(OSError):
                scan(root / "unplugged")
