from base64 import b64encode
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sys
import unittest
from unittest.mock import patch
from uuid import UUID
import wave
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QEventLoop, QItemSelection, QItemSelectionModel, QPointF, QRect, QTimer, Qt
from PySide6.QtGui import QFont, QImage, QPainter, QPdfWriter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit, QMessageBox, QPushButton, QStyleOptionViewItem

from drive_review.capture import read_item_taken
from drive_review.drives import identify_drive
from drive_review.flags import Flag, FlagStore, read_flags
from drive_review.items import bytes_label
from drive_review.quarantine import QUARANTINE, quarantine_items
from drive_review.scan import flagged_items, scan
from drive_review.thumbs import ThumbnailCache
from drive_review.ui.models import FOLDER
from drive_review.ui.organizer import MOVE_MIME, FolderPicker, select_folder
from drive_review.ui.window import MainWindow


def write_pair(folder, stem):
    fields = {0x9003: "2024:04:05 19:38:52", 0x9291: "032", 0x010F: "Canon", 0x0110: "EOS R6"}
    Image.new("RGB", (8, 8), "red").save(folder / f"{stem}.CR2", format="TIFF", tiffinfo=fields)
    jpg = Image.new("RGB", (20, 10), "blue")
    exif = jpg.getexif()
    exif[0x010F], exif[0x0110] = fields[0x010F], fields[0x0110]
    exif[0x8769] = {0x9003: fields[0x9003], 0x9291: fields[0x9291]}
    jpg.save(folder / f"{stem}.JPG", exif=exif)


class ReviewSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        if sys.platform == "darwin":
            cls.app.setFont(QFont("Helvetica Neue", 13))

    def wait_until(self, predicate, timeout=10):
        if predicate():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(10)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll.start()
        deadline.start(int(timeout * 1000))
        loop.exec()
        self.assertTrue(predicate(), "Qt operation did not finish in time")

    def test_offscreen_scan_grid_shortcuts_and_portable_flags(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            (root / "photos").mkdir(parents=True)
            for number in range(2_000):
                (root / "photos" / f"notes-{number:04}.txt").touch()
            Image.new("RGB", (30, 20), "#209030").save(root / "a-image.png")
            with ZipFile(root / "album.zip", "w") as archive:
                archive.writestr("folder/entry.txt", b"text")

            window = MainWindow(base / "local", list_drives=False)
            window.show()
            entered = Event()
            release = Event()
            ticks = []

            def hold_scan(*args):
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("Qt event loop failed to run while scan was active")
                return scan(*args)

            def release_from_event_loop():
                if entered.is_set():
                    ticks.append(window.operation is not None)
                    release.set()
                else:
                    QTimer.singleShot(10, release_from_event_loop)

            try:
                with patch("drive_review.ui.window.scan", side_effect=hold_scan):
                    window.open_root(root)
                    QTimer.singleShot(0, release_from_event_loop)
                    self.wait_until(lambda: window.operation is None)
                self.assertEqual(ticks, [True])
                self.assertEqual(window.model.rowCount(), 2_003)
                tree = window.folder_model
                root_index = tree.index(0, 0)
                self.assertEqual(tree.rowCount(root_index), 2)
                zip_index = tree.index(0, 0, root_index)
                self.assertEqual(zip_index.data(), "album.zip")
                self.assertEqual(tree.index(0, 0, zip_index).data(), "folder")

                window.reviewer.setText("검토자")
                first = window.model.index(0)
                second = window.model.index(1)
                selection = window.grid.selectionModel()
                selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
                selection.select(QItemSelection(first, second), QItemSelectionModel.SelectionFlag.ClearAndSelect)
                window.activateWindow()
                window.grid.setFocus()
                QTest.keyClick(window.grid, Qt.Key.Key_Z)
                path = root / "_drive_review" / "flags.csv"
                self.wait_until(lambda: path.exists() and window.saved_revision == window.revision)
                flags = read_flags(path)
                self.assertEqual(len(flags), 2)
                self.assertEqual({flag.flag for flag in flags.values()}, {"green"})
                self.assertEqual({flag.reviewer for flag in flags.values()}, {"검토자"})

                QTest.keyClick(window.grid, Qt.Key.Key_X)
                self.assertEqual({flag.flag for flag in window.flags.values()}, {"red"})
                QTest.keyClick(window.grid, Qt.Key.Key_C)
                self.assertEqual(window.flags, {})
                QTest.keyClick(window.grid, Qt.Key.Key_Z)
                self.wait_until(lambda: window.saved_revision == window.revision)
                window.open_root(root)
                self.wait_until(lambda: window.operation is None)
                self.assertEqual(len(window.model.flag_states), 2)
                self.assertIn("Upload 2", window.summary.text())

                window.tree.setCurrentIndex(window.folder_model.index(0, 0, window.folder_model.index(0, 0)))
                self.assertEqual(window.model.rowCount(), 1)
                self.assertEqual(window.model.index(0).data(), "entry.txt")
            finally:
                release.set()
                window.close()
                self.wait_until(lambda: not window.isVisible())
                self.assertTrue(window.thumbnails.pool.waitForDone(10_000))

    def test_offscreen_duplicates_upload_and_quarantine_round_trip(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            keeper = root / "keep.txt"
            dupe = root / "copy.txt"
            keeper.write_bytes(b"keep these bytes")
            dupe.write_bytes(keeper.read_bytes())
            timestamp = dupe.stat().st_mtime - 10
            os.utime(keeper, (timestamp, timestamp))
            upload = base / "upload"
            upload.mkdir()
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                window.open_root(root)
                self.wait_until(lambda: window.scan_result is not None)
                window.run_duplicates()
                self.wait_until(lambda: bool(window.groups))
                self.assertEqual(window.flags[("copy.txt", "")].reason, "duplicate")
                self.assertNotIn(("keep.txt", ""), window.flags)
                window.grid.setCurrentIndex(window.model.index(window.model.rows[("keep.txt", "")]))
                window.mark_selected("green")

                with patch("drive_review.ui.window.QFileDialog.getExistingDirectory", return_value=str(upload)):
                    window.choose_upload()
                    self.wait_until(lambda: (upload / "manifest.csv").exists() and not window._busy())
                self.assertEqual((upload / keeper.name).read_bytes(), keeper.read_bytes())

                with patch("drive_review.ui.window.QMessageBox.question",
                           return_value=QMessageBox.StandardButton.Yes):
                    window.confirm_quarantine()
                    self.wait_until(lambda: window.model.rowCount() == 1 and not window._busy())
                    self.assertFalse(dupe.exists())
                    self.assertTrue((root / "_drive_review/quarantine/copy.txt").exists())
                    self.assertIn("In quarantine 1", window.summary.text())
                    window.confirm_undo()
                    self.wait_until(lambda: window.model.rowCount() == 2 and not window._busy())
                self.assertEqual(dupe.read_bytes(), keeper.read_bytes())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_quick_look_follows_grid_flags_and_cleans_up_zip_audio(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            image = Image.new("RGB", (75, 45), "#209030")
            exif = image.getexif()
            exif[274] = 6
            image.save(root / "01-photo.jpg", exif=exif)
            writer = QPdfWriter(str(root / "02-report.pdf"))
            painter = QPainter(writer)
            painter.drawText(80, 120, "Tiny PDF")
            writer.newPage()
            painter.drawText(80, 120, "Second page")
            painter.end()
            del writer
            (root / "03-notes.txt").write_text("Notes for Quick Look", encoding="utf-8")
            sound = BytesIO()
            with wave.open(sound, "wb") as stream:
                stream.setparams((1, 2, 8_000, 0, "NONE", ""))
                stream.writeframes(b"\0" * 16_000)
            with ZipFile(root / "04-archive.zip", "w") as archive:
                archive.writestr("05-audio.wav", sound.getvalue())
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            preview = window.quick_look
            try:
                window.open_root(root)
                self.wait_until(lambda: window.scan_result is not None)
                window.grid.setCurrentIndex(window.model.index(0))
                window.activateWindow()
                window.grid.setFocus()
                QTest.keyClick(window.grid, Qt.Key.Key_Space)
                self.wait_until(lambda: preview.stack.currentWidget() is preview.image)
                self.assertEqual(preview.position.text(), "1 / 5")
                self.assertEqual((preview.data.image.width(), preview.data.image.height()), (45, 75))
                scale = preview.image.transform().m11()
                QTest.keyClick(preview.image, Qt.Key.Key_Plus)
                self.assertGreater(preview.image.transform().m11(), scale)
                preview.fit.click()
                self.assertAlmostEqual(preview.image.transform().m11(), scale)

                QTest.keyClick(preview, Qt.Key.Key_Z)
                self.assertEqual(preview.flag.text(), "Upload")
                QTest.keyClick(preview, Qt.Key.Key_Right)
                self.wait_until(lambda: preview.stack.currentWidget() is preview.pdf)
                self.assertEqual(window.grid.currentIndex().row(), 1)
                self.assertEqual(preview.page_count.text(), "Page 1 / 2")
                QTest.keyClick(preview.pdf, Qt.Key.Key_Right)
                self.wait_until(lambda: preview.stack.currentWidget() is preview.text)
                self.assertEqual(preview.text.toPlainText(), "Notes for Quick Look")
                QTest.keyClick(preview.text, Qt.Key.Key_X)
                self.assertEqual(preview.flag.text(), "Delete")
                QTest.keyClick(preview.text, Qt.Key.Key_C)
                self.assertEqual(preview.flag.text(), "Unflagged")
                QTest.keyClick(preview.text, Qt.Key.Key_Escape)
                self.assertFalse(preview.isVisible())

                window.grid.setCurrentIndex(window.model.index(window.model.rows[("04-archive.zip", "05-audio.wav")]))
                window.activateWindow()
                window.grid.setFocus()
                self.wait_until(window.isActiveWindow)
                QTest.keyClick(window.grid, Qt.Key.Key_Space)
                self.wait_until(preview.isVisible)
                self.wait_until(lambda: preview.media is not None and preview.media.player.duration() > 0)
                temp_path = Path(preview.media.player.source().toLocalFile())
                self.assertTrue(temp_path.is_file())
                self.assertEqual(preview.path.text(), f"{root / '04-archive.zip'}::05-audio.wav")
                QTest.keyClick(preview.media, Qt.Key.Key_Space)
                self.assertFalse(preview.isVisible())
                self.wait_until(lambda: not temp_path.exists())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_quick_look_reads_taken_before_background_pass_and_keeps_selection_when_sorted(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            (root / "00-notes.txt").touch()
            image = Image.new("RGB", (20, 20))
            image.save(root / "z-unknown.jpg")
            for name, date, offset in (("a-blocked.jpg", "2024:01:02 10:14:59", "+00:00"),
                                       ("b-preview.jpg", "2019:06:02 05:04:05", "+02:00"),
                                       ("c-equal.jpg", "2019:06:02 03:04:05", "+00:00")):
                exif = image.getexif()
                exif[0x8769] = {0x9003: date, 0x9011: offset}
                image.save(root / name, exif=exif)

            entered, release = Event(), Event()

            def hold_first(reader, source, item):
                if item.name == "a-blocked.jpg":
                    entered.set()
                    if not release.wait(8):
                        raise TimeoutError("Capture date worker was not released")
                return read_item_taken(reader, source, item)

            window = MainWindow(base / "local", list_drives=False)
            window.show()
            preview = window.quick_look
            try:
                with patch("drive_review.capture.read_item_taken", side_effect=hold_first) as reads:
                    window.open_root(root)
                    self.wait_until(entered.is_set)
                    self.assertEqual(window.capture_status.text(), "Reading capture dates 0 / 4")
                    window.sort.setCurrentIndex(window.sort.findData("taken"))
                    window.grid.setCurrentIndex(window.model.index(window.model.rows[("b-preview.jpg", "")]))
                    window.preview_selected()
                    self.wait_until(lambda: "Taken 2019-06-02 05:04:05 +02:00" in preview.details.text())
                    self.assertIn(" · Taken 2019-06-02 05:04:05 +02:00 · Created ", preview.details.text())
                    self.assertIn(" · Modified ", preview.details.text())
                    self.wait_until(lambda: window.model.visible[0].name == "b-preview.jpg")
                    release.set()
                    expected = ["a-blocked.jpg", "b-preview.jpg", "c-equal.jpg", "00-notes.txt", "z-unknown.jpg"]
                    # Dates can arrive in separate batches; the taken sort is debounced.
                    self.wait_until(lambda: window.capture_pass is None
                                    and [item.name for item in window.model.visible] == expected)
                    self.assertEqual(window.grid.currentIndex().data(), "b-preview.jpg")
                    self.assertEqual(preview.position.text(), "2 / 5")
                    self.assertIn("\nTaken 2019-06-02 05:04:05 +02:00\n",
                                  window.grid.currentIndex().data(Qt.ItemDataRole.ToolTipRole))
                    self.assertEqual(sum(call.args[2].name == "b-preview.jpg" for call in reads.call_args_list), 1)
                    window.grid.setCurrentIndex(window.model.index(4))
                    self.assertNotIn("Taken", preview.details.text())
            finally:
                release.set()
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_pairs_merge_during_capture_and_keep_selection_and_flags(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            write_pair(root, "a")
            write_pair(root, "b")
            (root / "notes.txt").write_text("notes")
            raw_a, jpg_a, raw_b, jpg_b = [(name, "") for name in ("a.CR2", "a.JPG", "b.CR2", "b.JPG")]
            entered, release = Event(), Event()

            def hold_first(reader, source, item):
                if item.key == raw_a:
                    entered.set()
                    if not release.wait(8):
                        raise TimeoutError("Capture date worker was not released")
                return read_item_taken(reader, source, item)

            window = MainWindow(base / "local", list_drives=False)
            window.show()
            preview = window.quick_look
            try:
                with patch("drive_review.capture.read_item_taken", side_effect=hold_first):
                    window.open_root(root)
                    self.wait_until(entered.is_set)
                    self.assertEqual(window.model.rowCount(), 5)
                    selection = window.grid.selectionModel()
                    selection.setCurrentIndex(window.model.index(window.model.rows[raw_a]),
                                              QItemSelectionModel.SelectionFlag.ClearAndSelect)
                    QTest.keyClick(window.grid, Qt.Key.Key_Z)
                    for key in (raw_b, jpg_b, ("notes.txt", "")):
                        selection.select(window.model.index(window.model.rows[key]),
                                         QItemSelectionModel.SelectionFlag.Select)
                    window.preview_selected()
                    self.assertEqual(preview.item.key, raw_a)
                    self.assertTrue(preview.pair_controls.isHidden())
                    release.set()
                    self.wait_until(lambda: window.capture_pass is None and window.model.rowCount() == 3)

                self.assertEqual(window.visible_count.text(), "3 files")
                self.assertEqual(window.model.rows[raw_a], window.model.rows[jpg_a])
                self.assertEqual(window.model.rows[raw_b], window.model.rows[jpg_b])
                self.assertEqual(window.grid.currentIndex().data(), "a.JPG")
                self.assertEqual({item.key for item in window._selected_items()}, {jpg_a, jpg_b, ("notes.txt", "")})
                self.assertEqual(window.model.flag_states, {raw_a: "green"})
                self.assertIn(f"Upload 1 ({bytes_label((root / 'a.CR2').stat().st_size)})", window.summary.text())
                self.assertEqual(preview.item.key, jpg_a)
                self.wait_until(lambda: preview.stack.currentWidget() is preview.image)
                bounds = preview.image.scene().sceneRect()
                self.assertEqual((bounds.width(), bounds.height()), (20, 10))
                self.assertTrue(preview.pair_controls.isVisible())
                self.assertEqual([button.text() for button in preview.choice_buttons.values()], ["JPG", "RAW", "RAW+JPG"])
                self.assertTrue(preview.choice_buttons["raw"].isChecked())
                self.assertTrue(all(button.focusPolicy() == Qt.FocusPolicy.NoFocus
                                    for button in preview.choice_buttons.values()))

                index = window.grid.currentIndex()
                tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
                for name in ("a.CR2", "a.JPG"):
                    self.assertIn(f"{name} · {bytes_label((root / name).stat().st_size)}", tooltip)
                self.assertIn("Upload choice · RAW", tooltip)
                option = QStyleOptionViewItem()
                option.rect = QRect(0, 0, 180, 222)
                surface = QImage(180, 222, QImage.Format.Format_RGB32)
                painter = QPainter(surface)
                try:
                    with patch.object(window.delegate, "draw_badge", wraps=window.delegate.draw_badge) as badge, \
                            patch.object(window.thumbnails, "request", return_value=None) as thumbnail, \
                            patch.object(painter, "drawText", wraps=painter.drawText) as drawn:
                        window.delegate.paint(painter, option, index)
                    self.assertIn("RAW+JPG", [call.args[2] for call in badge.call_args_list])
                    self.assertEqual(thumbnail.call_args.args[0].key, jpg_a)
                    self.assertTrue(any(call.args[2].startswith("↑ RAW") for call in drawn.call_args_list))
                finally:
                    painter.end()
            finally:
                release.set()
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_pair_keyboard_controls_filters_and_settings(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            for stem in ("a", "b"):
                write_pair(root, stem)
            (root / "notes.txt").touch()
            raw_a, jpg_a, raw_b, jpg_b = [(name, "") for name in ("a.CR2", "a.JPG", "b.CR2", "b.JPG")]
            path = root / "_drive_review" / "flags.csv"
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy() and window.capture_pass is None)
                self.assertTrue(window.group_pairs.isChecked())
                self.assertEqual(window.model.rowCount(), 3)
                selection = window.grid.selectionModel()
                selection.setCurrentIndex(window.model.index(window.model.rows[jpg_a]),
                                          QItemSelectionModel.SelectionFlag.ClearAndSelect)
                for key in (jpg_b, ("notes.txt", "")):
                    selection.select(window.model.index(window.model.rows[key]), QItemSelectionModel.SelectionFlag.Select)

                QTest.keyClick(window.grid, Qt.Key.Key_R)
                self.assertEqual(window.model.flag_states, {raw_a: "green", raw_b: "green"})
                window.preview_selected()
                preview = window.quick_look
                QTest.keyClick(preview, Qt.Key.Key_R)
                self.assertEqual(window.model.flag_states, dict.fromkeys((raw_a, jpg_a, raw_b, jpg_b), "green"))
                total = sum((root / key[0]).stat().st_size for key in (raw_a, jpg_a, raw_b, jpg_b))
                self.assertIn(f"Upload 4 ({bytes_label(total)})", window.summary.text())
                QTest.keyClick(preview, Qt.Key.Key_R)
                self.assertEqual(window.model.flag_states, {jpg_a: "green", jpg_b: "green"})
                QTest.mouseClick(preview.choice_buttons["raw"], Qt.MouseButton.LeftButton)
                self.assertEqual(window.model.flag_states, {raw_a: "green", jpg_b: "green"})
                QTest.keyClick(preview, Qt.Key.Key_X)
                self.assertEqual(window.model.flag_states, {raw_a: "red", jpg_a: "red", jpg_b: "green"})
                QTest.keyClick(preview, Qt.Key.Key_Z)
                self.assertEqual(window.model.flag_states, {raw_a: "green", jpg_b: "green"})
                QTest.keyClick(preview, Qt.Key.Key_C)
                self.assertEqual(window.model.flag_states, {jpg_b: "green"})
                self.assertTrue(preview.choice_buttons["raw"].isChecked())
                preview.close()

                window.category.setCurrentIndex(window.category.findData("Image"))
                self.assertEqual(window.model.rowCount(), 2)
                window.flag_filter.setCurrentIndex(window.flag_filter.findData("green"))
                self.assertEqual(window.model.rowCount(), 1)
                self.assertEqual(window.model.index(0).data(), "b.JPG")
                window.flag_filter.setCurrentIndex(0)
                window.search.setText("A.cr2")
                self.wait_until(lambda: window.model.rowCount() == 1)
                self.assertEqual(window.model.index(0).data(), "a.JPG")
                window.search.clear()
                window.group_pairs.trigger()
                self.assertEqual(window.model.rowCount(), 4)
                self.assertEqual(window.model.index(window.model.rows[raw_a]).data(), "a.CR2")
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

            saved = read_flags(path)
            self.assertEqual({key: (flag.flag, flag.pair_choice) for key, flag in saved.items()},
                             {jpg_b: ("green", "jpg")})
            upload, errors = flagged_items(root, saved, "green")
            self.assertEqual(([item.key for item in upload], errors), ([jpg_b], []))
            reopened = MainWindow(base / "local", list_drives=False)
            reopened.show()
            try:
                reopened.open_root(root)
                self.wait_until(lambda: not reopened._busy())
                self.assertFalse(reopened.group_pairs.isChecked())
                self.assertEqual(reopened.model.rowCount(), 5)
                reopened.group_pairs.trigger()
                self.assertEqual(reopened.model.rowCount(), 3)
                reopened.grid.setCurrentIndex(reopened.model.index(reopened.model.rows[jpg_a]))
                QTest.keyClick(reopened.grid, Qt.Key.Key_Z)
                self.assertEqual(reopened.model.flag_states, {raw_a: "green", jpg_b: "green"})
            finally:
                reopened.close()
                self.wait_until(lambda: not reopened.isVisible())

    def test_duplicate_suggestions_resolve_pair_metadata_before_auto_flagging(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            photos = root / "photos"
            photos.mkdir(parents=True)
            write_pair(photos, "scene")
            (root / "copy.jpg").write_bytes((photos / "scene.JPG").read_bytes())
            raw = next(item for item in scan(root).items if item.name.endswith(".CR2"))
            jpg = ("photos/scene.JPG", "")
            store = FlagStore(identify_drive(root), base / "local")
            store.save({raw.key: Flag.for_item(raw, "green", "manual", "reviewer", pair_choice="raw")})
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                with patch.object(window, "_start_capture_pass"):
                    window.open_root(root)
                    self.wait_until(lambda: not window._busy())
                self.assertEqual(window.model.rowCount(), 3)
                self.assertEqual(window.model.taken, {})
                window.run_duplicates()
                self.wait_until(lambda: bool(window.groups) and not window._busy())
                self.assertEqual(window.model.flag_states, {raw.key: "green"})
                self.assertIn(jpg, window.groups[0].suggestions)
                self.assertEqual(window.model.pair_choices[jpg], "raw")
                window.flag_filter.setCurrentIndex(window.flag_filter.findData("duplicate"))
                self.assertEqual(window.model.rowCount(), 2)
                index = window.model.index(window.model.rows[raw.key])
                self.assertIn("Duplicate · Suggested Delete", index.data(Qt.ItemDataRole.ToolTipRole))
                self.assertIn("0 automatic Delete flags", window.notice_text.text())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_quarantine_protects_a_pending_pair_from_a_legacy_duplicate_flag(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            write_pair(root, "scene")
            jpg = next(item for item in scan(root).items if item.name == "scene.JPG")
            store = FlagStore(identify_drive(root), base / "local")
            path, _ = store.save({jpg.key: Flag.for_item(jpg, "red", "duplicate", "reviewer")})
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                with patch.object(window, "_start_capture_pass"):
                    window.open_root(root)
                    self.wait_until(lambda: not window._busy())
                self.assertEqual(window.model.flag_states, {jpg.key: "red"})
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
                    window.confirm_quarantine()
                    self.wait_until(lambda: not window._busy())
                    confirm.assert_not_called()
                self.assertEqual(window.model.rowCount(), 1)
                self.assertEqual(window.model.flag_states, {})
                self.assertIn("Suggested Delete", window.model.index(0).data(Qt.ItemDataRole.ToolTipRole))
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())
            self.assertEqual(read_flags(path), {})

    def test_owner_permanent_delete_requires_the_file_count(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            (root / "remove.txt").write_bytes(b"remove")
            quarantine_items(root, scan(root).items)
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            confirmation = []

            def type_count():
                dialog = self.app.activeModalWidget()
                if dialog is None:
                    QTimer.singleShot(10, type_count)
                    return
                entry = dialog.findChild(QLineEdit)
                button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
                QTest.keyClicks(entry, "2")
                confirmation.append(button.isEnabled())
                entry.clear()
                QTest.keyClicks(entry, "1")
                confirmation.append(button.isEnabled())
                button.click()

            try:
                window.open_root(root)
                self.wait_until(lambda: window.scan_result is not None)
                self.assertIn("In quarantine 1 (6 B)", window.summary.text())
                action = window.owner.menu().actions()[-1]
                self.assertEqual(action.text(), "Delete quarantine permanently…")
                action.trigger()
                QTimer.singleShot(0, type_count)
                self.wait_until(lambda: "Permanently deleted" in window.notice_text.text() and not window._busy())
                self.assertEqual(confirmation, [False, True])
                self.assertFalse((root / QUARANTINE).exists())
                self.assertIn("In quarantine 0 (0 B)", window.summary.text())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_native_preview_failure_uses_office_text_and_cleans_its_copy(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            document = root / "notes.docx"
            with ZipFile(document, "w") as archive:
                archive.writestr("word/document.xml", """
                    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                      <w:body><w:p><w:r><w:t>Fallback text</w:t></w:r></w:p></w:body>
                    </w:document>
                """)
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            copies = []

            def fail_handler(_clsid, path, _handle, _stop, _on_key):
                copies.append((path, path.read_bytes()))
                return False

            try:
                window.open_root(root)
                self.wait_until(lambda: window.scan_result is not None)
                with (
                    patch("drive_review.preview.handler_for", return_value=UUID(int=1)),
                    patch("drive_review.ui.quick_look.show_handler", side_effect=fail_handler),
                ):
                    window.preview_selected()
                    preview = window.quick_look
                    self.wait_until(lambda: preview.stack.currentWidget() is preview.text)
                self.assertEqual(preview.text.toPlainText(), "Fallback text")
                self.assertEqual(len(copies), 1)
                path, contents = copies[0]
                self.assertEqual(contents, document.read_bytes())
                self.assertFalse(path.exists())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_delete_key_quarantines_pair_without_confirmation_or_rescan(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            write_pair(root, "a")
            (root / "b-next.txt").write_bytes(b"next")
            bytes_moved = sum((root / name).stat().st_size for name in ("a.CR2", "a.JPG"))
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            removed, resets = [], []
            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy() and window.capture_pass is None)
                window.model.rowsRemoved.connect(lambda *_: removed.append(True))
                window.model.modelReset.connect(lambda: resets.append(True))
                window.grid.setCurrentIndex(window.model.index(0))
                window.grid.setFocus()
                QTest.keyClick(window.grid, Qt.Key.Key_X)
                self.assertEqual(len(window.flags), 2)
                with patch("drive_review.ui.window.scan") as rescan, patch.object(QMessageBox, "question") as confirm:
                    QTest.keyClick(window.grid, Qt.Key.Key_Delete)
                    self.wait_until(lambda: window.quarantine_count == 2 and window.saved_revision == window.revision)
                    rescan.assert_not_called()
                    confirm.assert_not_called()
                self.assertEqual((removed, resets), ([True], []))
                self.assertEqual(window.model.rowCount(), 1)
                self.assertEqual(window.grid.currentIndex().data(), "b-next.txt")
                self.assertEqual(window.model.pairs, {})
                self.assertEqual(window.model.flag_states, {})
                self.assertEqual(read_flags(root / "_drive_review/flags.csv"), {})
                for name in ("a.CR2", "a.JPG"):
                    self.assertFalse((root / name).exists())
                    self.assertTrue((root / QUARANTINE / name).is_file())
                    self.assertNotIn((name, ""), window.index.cached(window.drive.id))
                self.assertIn(f"In quarantine 2 ({bytes_label(bytes_moved)})", window.summary.text())
                self.assertEqual(window.notice_text.text(), "Moved 2 files to quarantine · Restore from Owner menu")
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_quick_look_delete_skips_zip_and_keeps_changed_pair_member(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            write_pair(root, "a")
            with ZipFile(root / "archive.zip", "w") as archive:
                archive.writestr("entry.txt", b"entry")
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy() and window.capture_pass is None)
                window.grid.setCurrentIndex(window.model.index(window.model.rows["a.JPG", ""]))
                window.mark_selected("red")
                (root / "a.CR2").write_bytes(b"changed after scan")
                entry = window.model.index(window.model.rows["archive.zip", "entry.txt"])
                window.grid.selectionModel().select(entry, QItemSelectionModel.SelectionFlag.Select)
                window.preview_selected()
                with patch("drive_review.ui.window.scan") as rescan:
                    key = Qt.Key.Key_Backspace if sys.platform == "darwin" else Qt.Key.Key_Delete
                    QTest.keyClick(window.quick_look, key)
                    self.wait_until(lambda: window.quarantine_count == 1 and not window._busy())
                    rescan.assert_not_called()
                self.assertEqual(window.quick_look.item.key, ("a.CR2", ""))
                self.assertIn(("a.CR2", ""), window.model.rows)
                self.assertIn(("archive.zip", "entry.txt"), window.model.rows)
                self.assertEqual(window.model.flag_states, {("a.CR2", ""): "red"})
                self.assertTrue((root / QUARANTINE / "a.JPG").exists())
                self.assertTrue((root / "a.CR2").exists())
                self.assertTrue(window.details.isVisible())
                self.assertTrue(any("ZIP entry skipped" in message for message in window.messages))
                self.assertTrue(any("changed after review" in message for message in window.messages))
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_delete_waits_for_an_open_thumbnail_before_quarantine(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            source = root / "photo.jpg"
            Image.new("RGB", (8, 8)).save(source)
            opened, release, closed = Event(), Event(), Event()

            def read_thumbnail(_drive, _item, _size, *, stop):
                with source.open("rb"):
                    opened.set()
                    if not release.wait(8):
                        raise TimeoutError("Thumbnail was not released")
                closed.set()

            def quarantine(*args):
                self.assertTrue(closed.is_set(), "Quarantine started with the thumbnail handle open")
                return quarantine_items(*args)

            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                with (patch.object(window.thumbnails.cache, "get", side_effect=read_thumbnail),
                      patch("drive_review.ui.window.WAIT_FOR_SOURCE_READERS", True),
                      patch("drive_review.ui.window.quarantine_items", side_effect=quarantine)):
                    window.open_root(root)
                    self.wait_until(lambda: not window._busy() and window.capture_pass is None)
                    item = window.model.items[0]
                    window.thumbnails.request(item, 144)
                    self.wait_until(opened.is_set)
                    window.grid.setCurrentIndex(window.model.index(0))
                    window.grid.setFocus()
                    QTest.keyClick(window.grid, Qt.Key.Key_Delete)
                    self.assertTrue(window._busy())
                    self.assertTrue(source.is_file())
                    release.set()
                    self.wait_until(lambda: window.quarantine_count == 1 and window.saved_revision == window.revision)
                    self.assertTrue((root / QUARANTINE / source.name).is_file())
            finally:
                release.set()
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_organize_m_shortcut_opens_picker_and_moves_pair_with_flags(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            write_pair(root, "a")
            (root / "target").mkdir()
            Image.new("RGB", (4, 4), "green").save(root / "target/a.JPG")
            existing = (root / "target/a.JPG").read_bytes()
            window = MainWindow(base / "local", list_drives=False)
            window.show()

            opened = []

            def select_target(picker):
                opened.append(True)
                select_folder(picker.tree, "target")
                return QDialog.DialogCode.Accepted

            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy() and window.capture_pass is None)
                window.grid.setCurrentIndex(window.model.index(window.model.rows["a.JPG", ""]))
                window.grid.setFocus()
                QTest.keyClick(window.grid, Qt.Key.Key_Z)
                with patch.object(FolderPicker, "exec", select_target), \
                        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm:
                    QTest.keyClick(window.grid, Qt.Key.Key_M)
                    self.wait_until(lambda: ("target/a (2).JPG", "") in window.model.rows
                                    and not window._busy() and window.capture_pass is None)
                    self.assertEqual(opened, [True])
                    self.assertIn("Move 2 files", confirm.call_args.args[2])
                self.assertEqual(window.folder, "")
                self.assertEqual(window.grid.currentIndex().data(), "a (2).JPG")
                self.assertEqual(window.model.rows["target/a (2).JPG", ""],
                                 window.model.rows["target/a (2).CR2", ""])
                self.assertEqual(window.model.flag_states, {("target/a (2).JPG", ""): "green"})
                self.assertEqual((root / "target/a.JPG").read_bytes(), existing)
                self.assertEqual(read_flags(root / "_drive_review/flags.csv"), window.flags)
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_flagged_moves_use_all_flags_and_skip_zip_entries(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            (root / "album").mkdir(parents=True)
            (root / "target").mkdir()
            (root / "album/green.txt").write_bytes(b"green")
            (root / ".DS_Store").write_bytes(b"hidden")
            (root / "root-red.txt").write_bytes(b"red")
            with ZipFile(root / "archive.zip", "w") as archive:
                archive.writestr("inside.txt", b"inside")
            flags = {}
            for item in scan(root, show_hidden=True).items:
                status = "red" if item.name == "root-red.txt" else "green"
                if item.name != "archive.zip":
                    flags[item.key] = Flag.for_item(item, status, "manual", "reviewer")
            FlagStore(identify_drive(root), base / "local").save(flags)
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy())
                select_folder(window.tree, "album")
                actions = window.organize_menu.actions()
                self.assertEqual([action.text() for action in actions], [
                    "Move selected to folder…", "Move Upload files to folder…",
                    "Move Delete files to folder…", "New folder…",
                ])
                with patch.object(window, "_choose_move_folder", return_value="target"), \
                        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm:
                    actions[1].trigger()
                    self.wait_until(lambda: (root / "target/green.txt").exists() and not window._busy())
                    self.assertEqual(window.folder, "album")
                    self.assertTrue((root / "target/.DS_Store").is_file())
                    self.assertIn("Move 2 files (11 B)", confirm.call_args.args[2])
                    self.assertTrue(window.details.isVisible())
                    self.assertIn("ZIP entry skipped: archive.zip::inside.txt", window.messages)
                    actions[2].trigger()
                    self.wait_until(lambda: (root / "target/root-red.txt").exists() and not window._busy())
                    self.assertIn("Move 1 file", confirm.call_args.args[2])
                self.assertEqual({key: flag.flag for key, flag in window.flags.items()}, {
                    ("target/green.txt", ""): "green", ("target/.DS_Store", ""): "green",
                    ("target/root-red.txt", ""): "red", ("archive.zip", "inside.txt"): "green",
                })
                self.assertEqual(read_flags(root / "_drive_review/flags.csv"), window.flags)
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_tree_context_picker_and_drop_targets(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            (root / "album").mkdir(parents=True)
            (root / "target").mkdir()
            (root / "album/photo.txt").write_bytes(b"photo")
            with ZipFile(root / "archive.zip", "w") as archive:
                archive.writestr("member/inside.txt", b"inside")
            window = MainWindow(base / "local", list_drives=False)
            window.show()

            class Drop:
                def __init__(self, source, path):
                    self.origin = source
                    self.point = window.tree.visualRect(window.folder_model.index_for_path(path)).center()
                    self.accepted = False

                def source(self):
                    return self.origin

                def position(self):
                    return QPointF(self.point)

                def mimeData(self):
                    return self

                def hasFormat(self, name):
                    return name == MOVE_MIME

                def acceptProposedAction(self):
                    self.accepted = True

                def ignore(self):
                    self.accepted = False

            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy())
                for path, enabled in (("", [True, False, False]), ("album", [True, True, True]),
                                      ("archive.zip", [False, True, True])):
                    actions = window._tree_menu(path).actions()
                    self.assertEqual([action.text() for action in actions], ["New folder…", "Rename…", "Move to…"])
                    self.assertEqual([action.isEnabled() for action in actions], enabled)
                self.assertFalse(window.folder_model.is_real_folder("archive.zip/member"))

                def create_and_choose(picker):
                    self.assertNotIn("archive.zip", picker.model.nodes)
                    select_folder(picker.tree, "target")
                    button = next(button for button in picker.findChildren(QPushButton) if button.text() == "New folder")
                    with patch("drive_review.ui.organizer.ask_name", return_value="new"):
                        button.click()
                    self.assertEqual(picker.tree.currentIndex().data(FOLDER), "target/new")
                    return QDialog.DialogCode.Accepted

                with patch.object(FolderPicker, "exec", create_and_choose):
                    self.assertEqual(window._choose_move_folder(), "target/new")
                self.assertTrue((root / "target/new").is_dir())

                select_folder(window.tree, "album")
                window.tree.drag_path = "album"
                invalid = Drop(window.tree, "archive.zip")
                window.tree.dropEvent(invalid)
                self.assertFalse(invalid.accepted)
                valid = Drop(window.tree, "target")
                window.tree.dropEvent(valid)
                self.assertTrue(valid.accepted)
                self.wait_until(lambda: window.folder == "target/album" and not window._busy())
                self.assertTrue((root / "target/album/photo.txt").is_file())

                window.grid.setCurrentIndex(window.model.index(0))
                window.grid.drag_items = window._selected_items()
                drop = Drop(window.grid, "")
                window.tree.dropEvent(drop)
                self.assertTrue(drop.accepted)
                self.wait_until(lambda: (root / "photo.txt").exists() and not window._busy())

                member_key = ("archive.zip", "member/inside.txt")
                select_folder(window.tree, "archive.zip/member")
                window.grid.setCurrentIndex(window.model.index(window.model.rows[member_key]))
                window.mark_selected("green")
                with patch("drive_review.ui.window.ask_name", return_value="renamed.zip"):
                    window._tree_menu("archive.zip").actions()[1].trigger()
                    self.wait_until(lambda: window.folder == "renamed.zip/member" and not window._busy())
                self.assertEqual(window.grid.currentIndex().data(), "inside.txt")
                self.assertEqual(window.model.flag_states, {("renamed.zip", "member/inside.txt"): "green"})
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())

    def test_quick_look_and_grid_preview_design_files(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "drive"
            root.mkdir()
            path = root / "a-pdf.ai"
            writer = QPdfWriter(str(path))
            painter = QPainter(writer)
            painter.drawText(500, 500, "First page")
            painter.end()
            del writer
            jpeg = BytesIO()
            Image.new("RGB", (30, 12), "blue").save(jpeg, format="JPEG")
            xmp = b"<xmpGImg:image>" + b64encode(jpeg.getvalue()) + b"</xmpGImg:image>"
            with ZipFile(root / "design.zip", "w") as archive:
                archive.writestr("b-design.indd", xmp)
            window = MainWindow(base / "local", list_drives=False)
            window.show()
            preview = window.quick_look
            try:
                window.open_root(root)
                self.wait_until(lambda: not window._busy())
                window.grid.setCurrentIndex(window.model.index(window.model.rows["a-pdf.ai", ""]))
                window.preview_selected()
                self.wait_until(lambda: preview.stack.currentWidget() is preview.image)
                bounds = preview.image.scene().sceneRect()
                self.assertEqual(max(bounds.width(), bounds.height()), 2048)
                item = preview.item
                thumbnail = ThumbnailCache(base / "test-thumbs").get(window.drive, item, 128)
                with Image.open(BytesIO(thumbnail)) as image:
                    self.assertEqual(max(image.size), 128)
                window.grid.setCurrentIndex(window.model.index(window.model.rows["design.zip", "b-design.indd"]))
                self.wait_until(lambda: preview.stack.currentWidget() is preview.image)
                bounds = preview.image.scene().sceneRect()
                self.assertEqual((bounds.width(), bounds.height()), (30, 12))
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())
