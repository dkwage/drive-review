import csv
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from drive_review.drives import identify_drive
from drive_review.flags import FLAG_COLUMNS, Flag, FlagStore, atomic_csv, read_flags
from drive_review.items import RAW_EXTENSIONS, Item
from drive_review.pairs import PhotoPair, cycle_choice, pair_candidates, restore_choice
from drive_review.taken import Taken

CAPTURE = Taken("2024-04-05 19:38:52", None, 100, "032", "Canon", "EOS R6")


class PairTests(unittest.TestCase):
    def test_name_and_capture_are_both_required(self):
        raw = Item("folder/IMG_0177.CR2", 20, 1)
        jpg = Item("folder/img_0177.JpEg", 10, 2)
        pair = PhotoPair(raw, jpg)
        self.assertEqual(pair_candidates([raw, jpg]), {raw.key: pair, jpg.key: pair})
        self.assertEqual(pair_candidates([raw, replace(jpg, rel_path="folder/IMG_0178.JPG")]), {})
        self.assertTrue(pair.matches({raw.key: CAPTURE, jpg.key: CAPTURE}))
        for tags in ({"text": "2024-04-05 19:38:53", "sort_key": 101}, {"subsec": "033"},
                     {"make": "Nikon"}, {"model": "EOS R5"}):
            with self.subTest(tags=tags):
                self.assertFalse(pair.matches({raw.key: CAPTURE, jpg.key: replace(CAPTURE, **tags)}))
        for key in (raw.key, jpg.key):
            dates = {raw.key: CAPTURE, jpg.key: CAPTURE}
            del dates[key]
            self.assertFalse(pair.matches(dates))
            dates[key] = None
            self.assertFalse(pair.matches(dates))
        self.assertTrue(pair.matches({raw.key: CAPTURE, jpg.key: replace(CAPTURE, subsec=None, model=None)}))

    def test_capture_seconds_use_offsets_when_both_are_known(self):
        pair = PhotoPair(Item("scene.NEF", 20, 1), Item("scene.JPG", 10, 1))
        raw = replace(CAPTURE, offset="+00:00")
        jpg = replace(raw, text="2024-04-05 20:38:52", offset="+01:00")
        self.assertTrue(pair.matches({pair.raw.key: raw, pair.jpg.key: jpg}))
        self.assertTrue(pair.matches({pair.raw.key: raw, pair.jpg.key: CAPTURE}))
        self.assertFalse(pair.matches({pair.raw.key: raw, pair.jpg.key: replace(jpg, sort_key=101)}))

    def test_sources_and_folders_are_separate(self):
        raw = Item("archive.zip", 20, 1, "nested/SCENE.NEF")
        jpg = Item("archive.zip", 10, 2, "nested/scene.HEIC")
        self.assertEqual(pair_candidates([raw, jpg])[raw.key], PhotoPair(raw, jpg))
        for other in (replace(jpg, zip_entry="other/scene.HEIC"), replace(jpg, rel_path="other.zip"),
                      Item("archive.zip/nested/scene.HEIC", 10, 2)):
            self.assertEqual(pair_candidates([raw, other]), {})
        self.assertEqual(pair_candidates([Item("first/scene.NEF", 20, 1), Item("second/scene.JPG", 10, 1)]), {})

    def test_ambiguity_and_extensions(self):
        raw = Item("scene.ARW", 20, 1)
        jpg = Item("scene.JPG", 10, 2)
        for extra in (Item("SCENE.jpeg", 11, 1), Item("SCENE.heif", 11, 1), Item("scene.NEF", 22, 1)):
            self.assertEqual(pair_candidates([raw, jpg, extra]), {})
        for extension in RAW_EXTENSIONS:
            with self.subTest(extension=extension):
                member = replace(raw, rel_path=f"scene.{extension}")
                self.assertEqual(pair_candidates([member, jpg])[member.key], PhotoPair(member, jpg))
        self.assertEqual(pair_candidates([raw, replace(jpg, rel_path="scene.png")]), {})

    def test_cycle_and_upload_members(self):
        pair = PhotoPair(Item("scene.NEF", 20, 1), Item("scene.JPG", 10, 1))
        self.assertEqual(pair.uploads("jpg"), (pair.jpg,))
        self.assertEqual(pair.uploads("raw"), (pair.raw,))
        self.assertEqual(pair.uploads("both"), pair.items)
        self.assertEqual([cycle_choice(choice) for choice in ("jpg", "raw", "both")], ["raw", "both", "jpg"])


class PairFlagTests(unittest.TestCase):
    def test_round_trip_and_old_flag_inference(self):
        raw = Item("archive.zip", 20, 1, "scene.CR2")
        jpg = Item("archive.zip", 10, 2, "scene.JPG")
        raw_green = Flag.for_item(raw, "green", "manual", "reviewer")
        jpg_green = Flag.for_item(jpg, "green", "manual", "reviewer")
        raw_red = replace(raw_green, flag="red", pair_choice="both")
        jpg_upload = replace(jpg_green, pair_choice="jpg")
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = FlagStore(identify_drive(root), root)
            expected = {flag.key: flag for flag in (raw_red, jpg_upload)}
            path, fallback = store.save(expected)
            self.assertFalse(fallback)
            self.assertEqual(store.load(), (expected, False))
            self.assertEqual(restore_choice(None, read_flags(path)[jpg.key]), "jpg")
            self.assertEqual(restore_choice(read_flags(path)[raw.key], None), "both")
            with path.open(encoding="utf-8-sig", newline="") as source:
                self.assertEqual(tuple(next(csv.reader(source))), FLAG_COLUMNS)
                self.assertEqual(FLAG_COLUMNS[-1], "pair_choice")

            columns = FLAG_COLUMNS[:-1]
            for green, choice in (((raw_green,), "raw"), ((raw_green, jpg_green), "both"), ((jpg_green,), "jpg")):
                with self.subTest(choice=choice):
                    atomic_csv(path, columns, [{key: value for key, value in asdict(flag).items() if key in columns}
                                               for flag in green])
                    flags = read_flags(path)
                    self.assertTrue(all(not flag.pair_choice for flag in flags.values()))
                    self.assertEqual(restore_choice(flags.get(raw.key), flags.get(jpg.key)), choice)
