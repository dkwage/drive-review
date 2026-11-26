import csv
import errno
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from drive_review.drives import identify_drive
from drive_review.flags import FLAG_COLUMNS, Flag, FlagStore
from drive_review.quarantine import (
    LOG, QUARANTINE, LogEntry, delete_quarantine, quarantine_inventory, quarantine_items, read_log,
    undo_quarantine, write_log,
)
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

    def test_permanent_delete_rechecks_metadata_and_only_deletes_confirmed_files(self):
        (self.root / "nested" / "empty").mkdir(parents=True)
        remove = self.root / "nested" / "empty" / "remove.txt"
        changed = self.root / "changed.txt"
        later = self.root / "later.txt"
        for path in (remove, changed, later):
            path.write_bytes(b"data")
        items = {item.name: item for item in scan(self.root).items}
        self.assertEqual(quarantine_items(self.root, [items[remove.name], items[changed.name]]).moved, 2)
        inventory = quarantine_inventory(self.root)
        self.assertEqual((inventory.count, inventory.size), (2, 8))
        self.assertEqual(len(inventory.candidates), 2)
        holding = self.root / QUARANTINE
        changed_path = holding / changed.name
        timestamp = changed_path.stat().st_mtime_ns
        os.utime(changed_path, ns=(timestamp, timestamp + 2_000_000_000))
        quarantine_items(self.root, [items[later.name]])
        untracked = holding / "untracked.txt"
        untracked.write_bytes(b"untracked")

        result = delete_quarantine(self.root, inventory.candidates)
        self.assertEqual((result.moved, result.size), (1, 4))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("changed after review", result.errors[0])
        self.assertEqual({row.rel_path: row.state for row in read_log(self.root / LOG)}, {
            "nested/empty/remove.txt": "deleted", "changed.txt": "quarantined",
            "later.txt": "quarantined",
        })
        self.assertFalse((holding / "nested").exists())
        self.assertTrue(changed_path.is_file())
        self.assertTrue((holding / later.name).is_file())
        self.assertTrue(untracked.is_file())
        self.assertFalse(remove.exists())
        result = undo_quarantine(self.root)
        self.assertEqual(result.moved, 1)
        self.assertTrue(later.is_file())
        self.assertFalse(remove.exists())

    def test_permanent_delete_writes_intent_before_unlink_and_recovers_after_failed_commit(self):
        (self.root / "file.txt").write_bytes(b"data")
        quarantine_items(self.root, scan(self.root).items)
        holding = self.root / QUARANTINE / "file.txt"
        log = self.root / LOG
        candidates = quarantine_inventory(self.root).candidates
        with patch("drive_review.quarantine.write_log", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                delete_quarantine(self.root, candidates)
        self.assertTrue(holding.is_file())
        self.assertEqual(read_log(log)[0].state, "quarantined")

        def fail_commit(path, rows):
            if rows[0].state == "deleted":
                raise OSError("commit failed")
            self.assertTrue(holding.exists())
            write_log(path, rows)

        with patch("drive_review.quarantine.write_log", side_effect=fail_commit):
            with self.assertRaises(OSError):
                delete_quarantine(self.root, candidates)
        self.assertFalse(holding.exists())
        self.assertEqual(read_log(log)[0].state, "deleting")
        self.assertEqual(undo_quarantine(self.root).moved, 0)
        self.assertEqual(read_log(log)[0].state, "deleted")

    def test_permanent_delete_rejects_untrusted_log_paths(self):
        (self.root / "keep.txt").write_bytes(b"keep")
        holding = self.root / QUARANTINE
        holding.mkdir(parents=True)
        info = (self.root / "keep.txt").stat()
        log = self.root / LOG
        write_log(log, [LogEntry("../../keep.txt", info.st_size, info.st_mtime_ns, "quarantined")])
        inventory = quarantine_inventory(self.root)
        self.assertEqual(inventory.candidates, [])
        self.assertIn("Unsafe relative path", inventory.errors[0])
        self.assertTrue((self.root / "keep.txt").is_file())
