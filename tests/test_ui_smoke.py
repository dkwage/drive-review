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
from PySide6.QtCore import QEventLoop, QItemSelection, QItemSelectionModel, QTimer, Qt
from PySide6.QtGui import QFont, QPainter, QPdfWriter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QMessageBox

from drive_review.flags import read_flags
from drive_review.quarantine import QUARANTINE, quarantine_items
from drive_review.scan import scan
from drive_review.ui.window import MainWindow


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
                QTest.keyClick(window.grid, Qt.Key.Key_P)
                path = root / "_drive_review" / "flags.csv"
                self.wait_until(lambda: path.exists() and window.saved_revision == window.revision)
                flags = read_flags(path)
                self.assertEqual(len(flags), 2)
                self.assertEqual({flag.flag for flag in flags.values()}, {"green"})
                self.assertEqual({flag.reviewer for flag in flags.values()}, {"검토자"})

                QTest.keyClick(window.grid, Qt.Key.Key_X)
                self.assertEqual({flag.flag for flag in window.flags.values()}, {"red"})
                QTest.keyClick(window.grid, Qt.Key.Key_U)
                self.assertEqual(window.flags, {})
                QTest.keyClick(window.grid, Qt.Key.Key_P)
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

                QTest.keyClick(preview, Qt.Key.Key_P)
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
                QTest.keyClick(preview.text, Qt.Key.Key_U)
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
