from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from drive_review.drives import Drive, identify_drive
from drive_review.dupes import EDGE, find_duplicates, keeper_order
from drive_review.flags import Flag
from drive_review.index import Index, Record
from drive_review.items import Item
from drive_review.scan import scan


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.base = Path(area.name).resolve()
        self.index = Index(self.base / "local" / "index.sqlite")

    def root(self, name):
        root = self.base / name
        root.mkdir()
        return root

    def analyze(self, drive, flags=None, **options):
        result = scan(drive.root, show_hidden=True)
        self.index.sync_scan(drive, result, True)
        self.index.replace_flags(drive, flags or {})
        return find_duplicates(drive, result.items, flags or {}, self.index, **options)

    def test_size_edges_and_full_hash_zip_loose(self):
        root = self.root("original")
        (root / "short.txt").write_bytes(b"same!")
        (root / "renamed.txt").write_bytes(b"same!")
        (root / "other.txt").write_bytes(b"other")
        first = b"A" * EDGE + b"one" + b"B" * EDGE
        second = b"A" * EDGE + b"two" + b"B" * EDGE
        (root / "one.bin").write_bytes(first)
        (root / "two.bin").write_bytes(second)
        with ZipFile(root / "album.zip", "w") as archive:
            archive.writestr("folder/copy.txt", b"same!")
            archive.writestr("folder/different.txt", b"opp!!")
        drive = identify_drive(root)
        result = self.analyze(drive, verify=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual({record.item.identity for record in group.records},
                         {"short.txt", "renamed.txt", "album.zip::folder/copy.txt"})
        self.assertFalse(group.keeper.item.zip_entry)
        self.assertIn(("album.zip", "folder/copy.txt"), group.suggestions)
        self.assertNotEqual(result.hashes[("one.bin", "")], result.hashes[("two.bin", "")])
        cached = self.index.cached(drive.id)
        self.assertEqual(cached[("one.bin", "")][1], cached[("two.bin", "")][1])
        self.assertEqual(result.hashes[("short.txt", "")], result.hashes[("album.zip", "folder/copy.txt")])

    def test_keeper_priority_and_manual_override(self):
        root = self.root("keepers")
        drive = identify_drive(root)
        records = [Record(item, drive.id, drive.label, root) for item in (
            Item("$RECYCLE.BIN/a", 1, 1), Item("x.zip", 1, 1, "a"), Item("child/a", 1, 1),
            Item("new", 1, 3), Item("older-long-name", 1, 2), Item("old", 1, 2))]
        self.assertEqual([record.item.identity for record in sorted(records, key=keeper_order)],
                         ["old", "older-long-name", "new", "child/a", "x.zip::a", "$RECYCLE.BIN/a"])

        for filename in ("a", "longer", "longest"):
            (root / filename).write_bytes(b"x")
        items = {item.name: item for item in scan(root).items}
        flags = {items["a"].key: Flag.for_item(items["a"], "red", "manual", "Reviewer"),
                 items["longer"].key: Flag.for_item(items["longer"], "green", "manual", "Reviewer")}
        group = self.analyze(drive, flags).groups[0]
        self.assertEqual(group.keeper.item.name, "longer")
        self.assertEqual(group.suggestions, [("longest", "")])

    def test_cross_drive_requires_recorded_kept_copy_and_respects_rescan(self):
        a = self.root("a-drive")
        b = self.root("b-drive")
        drive_a = Drive("drive-a", "Drive A", "serial-a", a)
        drive_b = Drive("drive-b", "Drive B", "serial-b", b)
        (a / "a").write_bytes(b"same")
        (b / "some-longer-name").write_bytes(b"same")
        item_a = scan(a).items[0]
        import os
        os.utime(a / "a", ns=(item_a.mtime_ns - 4_000_000_000, item_a.mtime_ns - 4_000_000_000))
        item_a = scan(a).items[0]
        self.assertEqual(self.analyze(drive_a).groups, [])
        key_b = ("some-longer-name", "")
        reopened = Index(self.index.path)
        seen = find_duplicates(drive_b, scan(b).items, {}, reopened)
        self.assertEqual(seen.groups[0].suggestions, [])
        self.assertEqual({record.label for record in seen.groups[0].records}, {"Drive A", "Drive B"})

        red = Flag.for_item(item_a, "red", "manual", "Reviewer")
        self.index.replace_flags(drive_a, {red.key: red})
        self.assertEqual(self.analyze(drive_b).groups[0].suggestions, [])

        green = Flag.for_item(item_a, "green", "manual", "Reviewer")
        self.index.replace_flags(drive_a, {green.key: green})
        matched = self.analyze(drive_b).groups[0]
        self.assertEqual(matched.keeper.label, "Drive A")
        self.assertEqual(matched.suggestions, [key_b])

        (a / "a").unlink()
        offline = self.analyze(drive_b, verify=True).groups[0]
        self.assertEqual(offline.unverified, {drive_a.id})
        self.assertEqual(offline.suggestions, [])
        self.analyze(drive_a)
        self.assertEqual(self.analyze(drive_b).groups, [])

    def test_overlapping_folders_do_not_match_the_same_file(self):
        root = self.root("original")
        subfolder = root / "child"
        subfolder.mkdir()
        (subfolder / "same.txt").write_text("same")
        outer = identify_drive(root)
        inner = identify_drive(subfolder)
        item = scan(root).items[0]
        self.analyze(outer, {item.key: Flag.for_item(item, "green", "manual", "Reviewer")})
        result = self.analyze(inner)
        self.assertEqual(result.groups, [])

    def test_full_hash_cache_survives_cancellation(self):
        root = self.root("resume")
        for number in range(24):
            (root / f"{number}.txt").write_bytes(str(number).encode())
        stop = Event()

        def cancel(count, total, phase):
            if phase == "전체 해시":
                stop.set()

        drive = identify_drive(root)
        with self.assertRaises(InterruptedError):
            self.analyze(drive, stop=stop, progress=cancel)
        cached = self.index.cached(drive.id)
        self.assertEqual(sum(bool(entry[2]) for entry in cached.values()), 16)
        with patch("drive_review.dupes.partial_hash", side_effect=AssertionError("partial hash already cached")):
            result = self.analyze(drive)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.hashes), 24)
