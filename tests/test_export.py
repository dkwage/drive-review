import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from zipfile import ZipFile

from openpyxl import load_workbook

from drive_review.export import inventory_excel, prepare_upload
from drive_review.flags import Flag
from drive_review.scan import scan


class ExportTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        base = Path(area.name).resolve()
        self.root = base / "source"
        self.root.mkdir()
        self.output = base / "output"
        self.output.mkdir()
        (self.root / "=formula.txt").write_text("file")
        with ZipFile(self.root / "album.zip", "w") as archive:
            archive.writestr("child/file.txt", b"archived")
        self.items = scan(self.root).items
        self.flags = {item.key: Flag.for_item(item, "green", "manual", "검토자") for item in self.items}

    def test_upload_preserves_paths_and_namespaces(self):
        manifest = prepare_upload(self.root, self.output, self.items, self.flags)
        self.assertEqual((self.output / "=formula.txt").read_text(), "file")
        self.assertEqual((self.output / "album.zip").read_bytes(),
                         (self.root / "album.zip").read_bytes())
        self.assertEqual((self.output / "zip_entries" / "album.zip" / "child" / "file.txt").read_bytes(), b"archived")
        with manifest.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 3)
        self.assertEqual({len(row["hash"]) for row in rows}, {64})
        self.assertEqual({row["zip_entry"] for row in rows}, {"", "child/file.txt"})
        with self.assertRaises(FileExistsError):
            prepare_upload(self.root, self.output, self.items, self.flags)
        with self.assertRaises(ValueError):
            prepare_upload(self.root, self.root, self.items, self.flags)

    def test_inventory_flags_filters_literal_text_and_atomic_cancel(self):
        workbook = self.output / "inventory.xlsx"
        item = next(item for item in self.items if item.name == "=formula.txt")
        hashes = {item.key: "abc123"}
        inventory_excel(self.root, workbook, "외장/목록", self.items, self.flags, hashes)
        inventory_excel(self.root, workbook, "외장/목록", self.items, self.flags, hashes)
        book = load_workbook(workbook)
        try:
            self.assertEqual(book.sheetnames, ["외장_목록", "외장_목록 (2)"])
            for sheet in book:
                self.assertEqual(sheet.freeze_panes, "A2")
                self.assertEqual(sheet.auto_filter.ref, "A1:H4")
                row = next(row for row in sheet.iter_rows(min_row=2) if row[1].value == item.name)
                self.assertEqual(row[1].data_type, "s")
                self.assertEqual((row[7].value, row[8].value), ("green", "abc123"))
        finally:
            book.close()

        before = workbook.read_bytes()
        stop = Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            inventory_excel(self.root, workbook, "취소", self.items, self.flags, {}, stop)
        with self.assertRaises(InterruptedError):
            inventory_excel(self.root, self.output / "new.xlsx", "취소", self.items, self.flags, {}, stop)
        self.assertEqual(workbook.read_bytes(), before)
        self.assertFalse((self.output / "new.xlsx").exists())
