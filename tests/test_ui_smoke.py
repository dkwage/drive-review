import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QEventLoop, QItemSelection, QItemSelectionModel, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from drive_review.flags import read_flags
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
                self.assertIn("업로드 2개", window.summary.text())

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
                    window.confirm_undo()
                    self.wait_until(lambda: window.model.rowCount() == 2 and not window._busy())
                self.assertEqual(dupe.read_bytes(), keeper.read_bytes())
            finally:
                window.close()
                self.wait_until(lambda: not window.isVisible())
