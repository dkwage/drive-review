import csv
import errno
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from drive_review.drives import identify_drive
from drive_review.flags import FLAG_COLUMNS, Flag, FlagStore
from drive_review.quarantine import LOG, quarantine_items, undo_quarantine
from drive_review.scan import flagged_items, scan


class FlagAndQuarantineTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.base = Path(area.name).resolve()
        self.root = self.base / "drive"
        self.root.mkdir()
        self.store = FlagStore(identify_drive(self.root), self.base / "local")

    def test_flags_unicode_zip_roundtrip_and_failed_rename_preserves_last_save(self):
        (self.root / "사진, 1.png").write_bytes(b"image")
        with ZipFile(self.root / "archive.zip", "w") as archive:
            archive.writestr("하위/문서.txt", b"document")
        items = {item.identity: item for item in scan(self.root).items}
        loose = Flag.for_item(items["사진, 1.png"], "green", "manual", "검토자", "content-hash")
        member = Flag.for_item(items["archive.zip::하위/문서.txt"], "red", "duplicate", "검토자")
        flags = {flag.key: flag for flag in (loose, member)}
        path, local = self.store.save(flags)
        self.assertFalse(local)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with path.open(encoding="utf-8-sig", newline="") as source:
            self.assertEqual(tuple(next(csv.reader(source))), FLAG_COLUMNS)
        self.assertEqual(self.store.load(), (flags, False))
        contents = path.read_bytes()
        with patch("drive_review.flags.os.replace", side_effect=RuntimeError("failed rename")):
            with self.assertRaises(RuntimeError):
                self.store.save({})
        self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.store.save({})
        self.assertEqual(self.store.load(), ({}, False))

    def test_read_only_fallback_and_later_portable_save(self):
        path = self.root / "a.txt"
        path.write_text("a")
        item = scan(self.root).items[0]
        flag = Flag.for_item(item, "red", "manual", "")
        original = {item.key: flag}
        from drive_review.flags import atomic_csv

        def read_only(destination, *args):
            if Path(destination).name == "flags.csv":
                raise OSError(errno.EROFS, "read-only test")
            return atomic_csv(destination, *args)

        with patch("drive_review.flags.atomic_csv", side_effect=read_only):
            result, fallback = self.store.save(original)
        self.assertTrue(fallback)
        self.assertEqual(result, self.store.local_path)
        self.assertEqual(self.store.load(), (original, True))
        _path, fallback = self.store.save(original)
        self.assertFalse(fallback)
        self.assertFalse(result.exists())

    def test_quarantine_and_collision_safe_undo(self):
        parent = self.root / "nested"
        parent.mkdir()
        path = parent / "photo.jpg"
        path.write_bytes(b"original photo")
        with ZipFile(self.root / "album.zip", "w") as archive:
            archive.writestr("do-not-move.jpg", b"zip")
        found = scan(self.root)
        original = next(item for item in found.items if item.rel_path == "nested/photo.jpg")
        entry = next(item for item in found.items if item.zip_entry)
        result = quarantine_items(self.root, [original, entry])
        self.assertEqual((result.moved, result.size, result.errors), (1, original.size, []))
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "album.zip").exists())
        holding = self.root / "_drive_review" / "quarantine" / "nested" / "photo.jpg"
        self.assertEqual(holding.read_bytes(), b"original photo")
        with (self.root / LOG).open(encoding="utf-8-sig", newline="") as source:
            self.assertEqual(next(csv.DictReader(source))["state"], "quarantined")

        path.write_bytes(b"replacement")
        self.assertEqual(undo_quarantine(self.root).moved, 0)
        self.assertEqual(path.read_bytes(), b"replacement")
        self.assertTrue(holding.exists())
        path.unlink()

        self.assertEqual(undo_quarantine(self.root).moved, 1)
        self.assertEqual(path.read_bytes(), b"original photo")
        self.assertFalse(holding.exists())
        self.assertEqual(undo_quarantine(self.root).moved, 0)

    def test_owner_can_resolve_hidden_flags_and_skip_changed_files(self):
        (self.root / "desktop.ini").write_bytes(b"hidden")
        stale = self.root / "stale.txt"
        stale.write_bytes(b"initial")
        items = scan(self.root, show_hidden=True).items
        flags = {item.key: Flag.for_item(item, "red", "manual", "Owner") for item in items}
        stale.write_bytes(b"changed bytes")
        selected, errors = flagged_items(self.root, flags, "red", include_entries=False)
        self.assertEqual([item.rel_path for item in selected], ["desktop.ini"])
        self.assertEqual(len(errors), 1)
