from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from drive_review.flags import Flag
from drive_review.organize import create_folder, move_items, move_path, remap_flags
from drive_review.paths import move_no_replace
from drive_review.scan import scan


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "target").mkdir()

    def write(self, path, content=b"data"):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return file

    def items(self):
        return {item.key: item for item in scan(self.root, show_hidden=True).items}

    def test_file_moves_flat_and_uses_the_next_free_suffix(self):
        self.write("source/photo.jpg", b"photo")
        self.write("target/photo.jpg", b"existing")
        self.write("target/photo (2).jpg", b"also existing")
        record = self.items()["source/photo.jpg", ""]
        result = move_items(self.root, [record], "target")
        self.assertEqual((result.moved, result.size, result.errors), (1, 5, []))
        self.assertEqual(result.mapping, {"source/photo.jpg": "target/photo (3).jpg"})
        moved = self.root / result.mapping[record.rel_path]
        self.assertEqual(moved.read_bytes(), b"photo")
        self.assertEqual(moved.stat().st_mtime_ns, record.mtime_ns)
        self.assertEqual((self.root / "target/photo.jpg").read_bytes(), b"existing")

    def test_folder_move_collision_and_rename(self):
        self.write("album/sub/photo.jpg")
        self.write("target/album/existing.txt")
        result = move_path(self.root, "album", "target")
        self.assertEqual((result.moved, result.size, result.errors), (1, 4, []))
        self.assertEqual(result.mapping, {"album": "target/album (2)"})
        renamed = move_path(self.root, "target/album (2)", "target", name="Album")
        target = self.root / renamed.mapping["target/album (2)"]
        self.assertEqual((target / "sub/photo.jpg").read_bytes(), b"data")
        self.assertTrue((self.root / "target/album/existing.txt").exists())

    def test_case_only_rename_keeps_requested_spelling(self):
        self.write("album/photo.jpg")
        renamed = move_path(self.root, "album", "", name="Album")
        self.assertEqual(renamed.mapping, {"album": "Album"})
        self.assertIn("Album", os.listdir(self.root))

    def test_rejects_same_folder_self_and_descendants(self):
        source = self.write("album/sub/photo.jpg")
        item = self.items()["album/sub/photo.jpg", ""]
        self.assertIn("already", move_path(self.root, "album", "").errors[0])
        self.assertIn("already", move_items(self.root, [item], "album/sub").errors[0])
        for destination in ("album", "album/sub"):
            with self.subTest(destination=destination):
                result = move_path(self.root, "album", destination)
                self.assertIn("itself or a descendant", result.errors[0])
                self.assertFalse(result.mapping)
                self.assertTrue(source.is_file())

    def test_rejects_management_paths_and_destinations_outside_source(self):
        self.write("_DRIVE_REVIEW/flag.csv")
        self.write("source/item.txt")
        self.write("source/nested/_drive_review/hidden")
        item = self.items()["source/item.txt", ""]
        for destination in ("_DRIVE_REVIEW", "../elsewhere", str(self.root.parent)):
            with self.subTest(destination=destination):
                result = move_items(self.root, [item], destination)
                self.assertTrue(result.errors)
                self.assertFalse(result.mapping)
        managed = move_path(self.root, "_DRIVE_REVIEW", "target")
        folder = move_path(self.root, "source", "target")
        self.assertIn("management", managed.errors[0])
        self.assertIn("management", folder.errors[0])
        self.assertTrue((self.root / "_DRIVE_REVIEW/flag.csv").is_file())

    def test_rejects_link_paths_and_folders_containing_links(self):
        self.write("album/photo.jpg")
        self.write("outside/file.txt")
        item = self.items()["album/photo.jpg", ""]
        alias = self.root / "alias"
        link = self.root / "album/link"
        try:
            alias.symlink_to(self.root / "outside", target_is_directory=True)
            link.symlink_to(self.root / "outside/file.txt")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Cannot create a symlink: {error}")
        self.assertIn("Links", move_items(self.root, [item], "alias").errors[0])
        self.assertIn("Links", move_items(self.root, [replace(item, rel_path="album/link")], "target").errors[0])
        self.assertIn("contains a link", move_path(self.root, "album", "target").errors[0])
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.root / "outside/photo.jpg").exists())

    def test_skips_zip_entries_and_rejects_changed_files(self):
        changed = self.write("album/changed.txt")
        self.write("album/same-size.txt")
        with ZipFile(self.root / "album/archive.zip", "w") as archive:
            archive.writestr("entry.txt", b"entry")
        items = self.items()
        changed.write_bytes(b"new data")
        stamp = items["album/same-size.txt", ""].mtime_ns + 2_000_000_000
        os.utime(self.root / "album/same-size.txt", ns=(stamp, stamp))
        result = move_items(self.root, items.values(), "target")
        self.assertEqual(result.mapping, {"album/archive.zip": "target/archive.zip"})
        self.assertEqual(len(result.errors), 3)
        self.assertTrue(any("ZIP entry skipped" in error for error in result.errors))
        self.assertEqual(sum("changed after the scan" in error for error in result.errors), 2)
        self.assertTrue(changed.is_file())

    def test_rejects_folder_move_if_a_scanned_file_inside_changed(self):
        child = self.write("album/photo.jpg")
        items = self.items()
        child.write_bytes(b"changed")
        result = move_path(self.root, "album", "target", scanned=items.values())
        self.assertIn("changed after the scan", result.errors[0])
        self.assertFalse(result.mapping)
        self.assertTrue(child.exists())

    def test_remaps_folder_prefix_and_zip_flags_and_preserves_other_fields(self):
        self.write("album/sub/photo.jpg")
        self.write("album-other/photo.jpg")
        with ZipFile(self.root / "album/archive.zip", "w") as archive:
            archive.writestr("sub/file.txt", b"entry")
        original = self.items()
        flags = {key: Flag.for_item(item, "green", "manual", "reviewer", "hash", pair_choice="jpg")
                 for key, item in original.items()}
        moved = move_path(self.root, "album", "target", scanned=original.values())
        archive = self.items()["target/album/archive.zip", ""]
        renamed = move_path(self.root, archive.rel_path, "target/album", name="new.zip", item=archive)
        remapped = remap_flags(remap_flags(flags, moved.mapping), renamed.mapping)
        current = self.items()
        self.assertEqual(remapped.keys(), current.keys())
        self.assertTrue(all(flag.matches(current[key]) for key, flag in remapped.items()))
        self.assertIs(remapped["album-other/photo.jpg", ""], flags["album-other/photo.jpg", ""])
        self.assertEqual(remapped["target/album/new.zip", "sub/file.txt"],
                         replace(flags["album/archive.zip", "sub/file.txt"], rel_path="target/album/new.zip"))

    def test_pair_has_shared_suffix_and_rolls_back_on_failure(self):
        self.write("scene.CR2", b"raw")
        self.write("scene.JPG", b"jpg")
        self.write("target/scene.JPG", b"existing")
        pair = ("scene.CR2", ""), ("scene.JPG", "")
        items = self.items()
        files = [items[key] for key in pair]

        def fail_jpg(source, target):
            if source.name == "scene.JPG":
                raise PermissionError("JPG is locked")
            move_no_replace(source, target)

        with patch("drive_review.organize.move_no_replace", side_effect=fail_jpg):
            result = move_items(self.root, files, "target", pairs=[pair])
        self.assertEqual(result.mapping, {})
        self.assertIn("JPG is locked", result.errors[0])
        self.assertTrue(all((self.root / item.name).exists() for item in files))
        result = move_items(self.root, files, "target", pairs=[pair])
        self.assertEqual(result.mapping, {"scene.CR2": "target/scene (2).CR2", "scene.JPG": "target/scene (2).JPG"})

    def test_create_folder_validates_windows_names_and_destination(self):
        self.assertEqual(create_folder(self.root, "target", "A valid folder"), "target/A valid folder")
        self.assertTrue((self.root / "target/A valid folder").is_dir())
        with self.assertRaises(FileExistsError):
            create_folder(self.root, "target", "A valid folder")
        for name in ("", ".", "..", "a/b", "a\\b", "_DRIVE_REVIEW", "CON.txt", "COM¹", "LPT9",
                     "trailing.", "trailing ", "question?", "bad\x00name", "D:"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                create_folder(self.root, "", name)
        with self.assertRaises(ValueError):
            create_folder(self.root, "../outside", "folder")
