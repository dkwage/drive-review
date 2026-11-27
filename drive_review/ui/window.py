"""Drive Review's main window and asynchronous review workflow."""

from collections import Counter
from dataclasses import replace
from functools import partial
from pathlib import Path
import shutil
from sqlite3 import Error as SQLiteError
import tempfile

from PySide6.QtCore import QEvent, QItemSelectionModel, QSettings, QSignalBlocker, QThread, QThreadPool, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QApplication, QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSlider, QSplitter, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..capture import CaptureDates
from ..drives import discover_drives, identify_drive
from ..dupes import check_cancel, find_duplicates
from ..export import inventory_excel, prepare_upload
from ..flags import Flag, FlagStore
from ..index import Index
from ..items import CATEGORY_LABELS, bytes_label
from ..paths import local_data_dir
from ..quarantine import MoveResult, delete_quarantine, quarantine_inventory, quarantine_items, undo_quarantine
from ..scan import flagged_items, scan
from ..thumbs import ThumbnailCache, copy_for_view
from ..zipsource import ItemReader
from .grid import ReviewDelegate, ThumbnailQueue
from .models import FOLDER, ITEM, FolderModel, ReviewModel
from .quick_look import QuickLook
from .workers import Worker

QUEUED = Qt.ConnectionType.QueuedConnection

STYLE = """
QMainWindow, QDialog { background: #F5F8F6; color: #172D2A; }
QFrame#header { background: #173E36; }
QLabel#brand { color: #FFFFFF; font-size: 22px; font-weight: 650; }
QLabel#source { color: #D7E8E0; }
QFrame#controls { background: #FFFFFF; border-bottom: 1px solid #DFE8E2; }
QLabel#section { color: #38584F; font-weight: 650; }
QFrame#notice { background: #F8F0DC; }
QLabel#noticeText { color: #654A22; }
QListView, QTreeView, QTreeWidget {
    background-color: #F5F8F6; color: #172D2A; border: 0; outline: 0;
}
QTreeView::item, QTreeWidget::item { padding: 4px; }
QTreeView::item:selected, QTreeWidget::item:selected { background: #DCEFE6; color: #173E36; }
QPushButton {
    color: #21443A; background: #FFFFFF; border: 1px solid #CBDAD1; border-radius: 6px;
    padding: 6px 10px;
}
QPushButton:hover { background: #E9F4EE; }
QPushButton:focus { border-color: #25806C; }
QPushButton#primary { background: #E0F1E7; border-color: #94C7AB; }
QPushButton#danger { color: #9A3734; }
QPushButton:disabled { color: #6E7A75; background: #EFF2F0; }
QLineEdit, QComboBox {
    color: #172D2A; background: #FFFFFF; border: 1px solid #CBDAD1; border-radius: 5px;
    padding: 5px 7px; min-height: 18px;
}
QLineEdit:focus, QComboBox:focus { border-color: #25806C; }
QLabel, QCheckBox { color: #21443A; }
QStatusBar { background: #EAF2ED; color: #21443A; }
QStatusBar QLabel { padding: 3px 5px; }
"""


class MainWindow(QMainWindow):
    def __init__(self, data_dir=None, *, list_drives=True):
        super().__init__()
        self.setWindowTitle("Drive Review")
        self.resize(1260, 810)
        self.setMinimumSize(850, 565)
        self.setStyleSheet(STYLE)

        self.data_dir = Path(data_dir) if data_dir is not None else local_data_dir()
        self.index = Index(self.data_dir / "index.sqlite")
        self.settings = QSettings(str(self.data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.viewer_files = tempfile.TemporaryDirectory(prefix="drive-review-view-", ignore_cleanup_errors=True)
        self.drive = None
        self.store = None
        self.scan_result = None
        self.flags = {}
        self.hashes = {}
        self.groups = []
        self.folder = ""
        self.local_flags_path = None
        self.messages = []
        self.quarantine_count = self.quarantine_size = 0
        self.preview_filter_dirty = False
        self.operation = None
        self.capture = None
        self.capture_pass = None
        self.capture_request = None
        self.capture_workers = set()
        self.discovery = None
        self.saving = None
        self.after_save = None
        self.closing = False
        self.revision = self.saved_revision = 0

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.capture_pool = QThreadPool(self)
        self.capture_pool.setMaxThreadCount(1)
        self.capture_pool.setThreadPriority(QThread.Priority.LowPriority)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(450)
        self.save_timer.timeout.connect(self._start_save)
        self.filter_timer = QTimer(self)
        self.filter_timer.setInterval(150)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.timeout.connect(self._apply_filter)
        self.capture_sort_timer = QTimer(self)
        self.capture_sort_timer.setSingleShot(True)
        self.capture_sort_timer.setInterval(200)
        self.capture_sort_timer.timeout.connect(self._resort_taken)

        self._build()
        self.quick_look = QuickLook(self, self.settings)
        self.quick_look.navigate.connect(self._preview_navigate)
        self.quick_look.flag_requested.connect(partial(self.mark_selected, current_only=True))
        self.quick_look.open_requested.connect(self.open_selected)
        self.quick_look.idle.connect(self._thumbnail_idle)
        self.quick_look.finished.connect(self._preview_closed)
        self.grid.selectionModel().currentChanged.connect(self._sync_preview)
        self.preview_escape = QShortcut(QKeySequence("Esc"), self)
        self.preview_escape.setEnabled(False)
        self.preview_escape.activated.connect(self.quick_look.close)
        self._update_controls()
        if list_drives:
            QTimer.singleShot(0, self.refresh_drives)

    @staticmethod
    def _button(text, action, style=None):
        button = QPushButton(text)
        button.clicked.connect(action)
        if style:
            button.setObjectName(style)
        return button

    @staticmethod
    def _combo(options):
        combo = QComboBox()
        for label, value in options:
            combo.addItem(label, value)
        return combo

    def _build(self):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(body)

        header = QFrame(objectName="header")
        title = QHBoxLayout(header)
        title.setContentsMargins(17, 10, 17, 10)
        brand = QLabel("Drive Review", objectName="brand")
        self.source = QLabel("Choose a drive or folder to review", objectName="source")
        self.source.setTextFormat(Qt.TextFormat.PlainText)
        self.drive_picker = self._combo([("Select external drive", None)])
        self.drive_picker.setMinimumWidth(165)
        self.drive_picker.activated.connect(self._choose_drive)
        self.refresh_button = self._button("Refresh", self.refresh_drives)
        self.folder_button = self._button("Open folder", self.choose_folder, "primary")
        title.addWidget(brand)
        title.addSpacing(14)
        title.addWidget(self.source, 1)
        title.addWidget(self.drive_picker)
        title.addWidget(self.refresh_button)
        title.addWidget(self.folder_button)
        layout.addWidget(header)

        controls = QFrame(objectName="controls")
        lines = QVBoxLayout(controls)
        lines.setContentsMargins(15, 8, 15, 9)
        lines.setSpacing(7)
        actions = QHBoxLayout()
        self.duplicate_button = self._button("Find duplicates", self.run_duplicates)
        self.groups_button = self._button("Duplicate groups", self.show_groups)
        self.verify = QCheckBox("Byte comparison")
        self.verify.setToolTip("Compare file contents when hashes match.")
        self.verify.setChecked(self.settings.value("verify", False, type=bool))
        self.verify.toggled.connect(partial(self.settings.setValue, "verify"))
        self.inventory_button = self._button("Export Excel inventory…", self.choose_inventory)
        self.owner = QToolButton()
        self.owner.setText("Owner")
        self.owner.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.owner)
        menu.setToolTipsVisible(True)
        self.owner.setMenu(menu)
        for label, tooltip, handler in (
            ("Copy green files for upload…",
             "Copies green files and extracts green ZIP entries into a chosen local folder + manifest.csv, "
             "for dragging into RS Uploader.", self.choose_upload),
            ("Move red files to quarantine…",
             "Moves red files into _drive_review/quarantine/ on the same drive so you can restore them later.",
             self.confirm_quarantine),
            ("Restore quarantined files…",
             "Returns quarantined files to their original locations without overwriting existing files.",
             self.confirm_undo),
            ("Delete quarantine permanently…",
             "Permanently deletes unchanged, logged files from quarantine after you type the file count.",
             self.confirm_permanent_delete),
        ):
            action = menu.addAction(label, handler)
            action.setToolTip(tooltip)
        for widget in (self.duplicate_button, self.groups_button, self.verify):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.inventory_button)
        actions.addWidget(self.owner)
        lines.addLayout(actions)

        filters = QHBoxLayout()
        self.category = self._combo([("All categories", "")] +
                                    [(label, value) for value, label in CATEGORY_LABELS.items()])
        self.flag_filter = self._combo([("All flags", ""), ("Unflagged", "none"), ("Upload", "green"),
                                       ("Delete", "red"), ("Duplicate", "duplicate")])
        self.sort = self._combo([("By name", "name"), ("By size", "size"),
                                 ("By modified", "date"), ("By created", "created"),
                                 ("By taken", "taken"), ("By folder", "folder")])
        self.search = QLineEdit(placeholderText="Search file names or paths")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search files")
        self.show_hidden = QCheckBox("Show system items")
        self.show_hidden.setChecked(self.settings.value("show_hidden", False, type=bool))
        self.show_hidden.toggled.connect(self._toggle_hidden)
        self.thumb_size = QSlider(Qt.Orientation.Horizontal)
        self.thumb_size.setRange(104, 224)
        self.thumb_size.setSingleStep(8)
        self.thumb_size.setPageStep(16)
        self.thumb_size.setValue(self.settings.value("thumb_size", 144, type=int))
        self.thumb_size.setFixedWidth(84)
        self.thumb_size.setAccessibleName("Thumbnail size")
        self.thumb_size.valueChanged.connect(self._resize_thumbs)
        self.search.textChanged.connect(lambda _text: self.filter_timer.start())
        for combo in (self.category, self.flag_filter, self.sort):
            combo.currentIndexChanged.connect(self._apply_filter)
            filters.addWidget(combo)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.show_hidden)
        filters.addWidget(QLabel("Thumbnails"))
        filters.addWidget(self.thumb_size)
        lines.addLayout(filters)
        layout.addWidget(controls)

        self.notice = QFrame(objectName="notice")
        notices = QHBoxLayout(self.notice)
        notices.setContentsMargins(15, 4, 15, 4)
        self.notice_text = QLabel(objectName="noticeText")
        self.notice_text.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_text.setWordWrap(True)
        self.details = self._button("Details", self.show_details)
        notices.addWidget(self.notice_text, 1)
        notices.addWidget(self.details)
        layout.addWidget(self.notice)
        self.notice.hide()

        split = QSplitter()
        split.setChildrenCollapsible(False)
        sidebar = QWidget()
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(13, 10, 8, 9)
        left.addWidget(QLabel("Folders", objectName="section"))
        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.folder_model = FolderModel(self.tree)
        self.tree.setModel(self.folder_model)
        self.tree.selectionModel().currentChanged.connect(self._select_folder)
        left.addWidget(self.tree, 1)
        self.reviewer = QLineEdit(self.settings.value("reviewer", "", type=str))
        self.reviewer.setAccessibleName("Reviewer name")
        self.reviewer.setPlaceholderText("Reviewer name")
        self.reviewer.textChanged.connect(partial(self.settings.setValue, "reviewer"))
        left.addWidget(QLabel("Reviewer", objectName="section"))
        left.addWidget(self.reviewer)
        split.addWidget(sidebar)

        gallery = QWidget()
        right = QVBoxLayout(gallery)
        right.setContentsMargins(5, 9, 12, 8)
        gallery_top = QHBoxLayout()
        self.visible_count = QLabel("0 files", objectName="section")
        self.selection_count = QLabel("")
        gallery_top.addWidget(self.visible_count)
        gallery_top.addWidget(self.selection_count)
        gallery_top.addStretch()
        gallery_top.addWidget(QLabel("Z Upload   X Delete   C Clear   Space Quick Look"))
        right.addLayout(gallery_top)

        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(256)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setAccessibleName("File review grid")
        self.model = ReviewModel(self.grid)
        self.grid.setModel(self.model)
        self.thumbnails = ThumbnailQueue(ThumbnailCache(self.data_dir / "thumbs"), self.grid)
        self.thumbnails.idle.connect(self._thumbnail_idle)
        self.delegate = ReviewDelegate(self.thumbnails, self.grid)
        self.grid.setItemDelegate(self.delegate)
        self._resize_thumbs(self.thumb_size.value())
        self.grid.doubleClicked.connect(lambda _index: self.open_selected())
        self.grid.selectionModel().selectionChanged.connect(self._selected_count)
        for key, handler in (("Z", partial(self.mark_selected, "green")),
                             ("X", partial(self.mark_selected, "red")),
                             ("C", partial(self.mark_selected, "")), ("Space", self.preview_selected),
                             ("Return", self.open_selected), ("Enter", self.open_selected)):
            shortcut = QShortcut(QKeySequence(key), self.grid)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
        right.addWidget(self.grid, 1)
        split.addWidget(gallery)
        split.setSizes([235, 1025])
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)

        self.summary = QLabel()
        bar = self.statusBar()
        bar.addWidget(self.summary, 1)
        self.capture_status = QLabel()
        self.capture_cancel = self._button("Stop dates", self._cancel_capture_pass)
        for widget in (self.capture_status, self.capture_cancel):
            bar.addPermanentWidget(widget)
            widget.hide()
        self.progress_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(127)
        self.progress.setTextVisible(False)
        self.cancel_button = self._button("Cancel", self.cancel_work)
        for widget in (self.progress_label, self.progress, self.cancel_button):
            bar.addPermanentWidget(widget)
            widget.hide()
        self._update_summary()

    def _busy(self):
        return self.operation is not None or self.after_save is not None or self.closing

    def _update_controls(self):
        ready = not self._busy()
        loaded = self.scan_result is not None
        self.folder_button.setEnabled(ready)
        self.drive_picker.setEnabled(ready)
        self.refresh_button.setEnabled(ready and self.discovery is None)
        self.show_hidden.setEnabled(ready)
        for button in (self.duplicate_button, self.inventory_button, self.owner, self.verify):
            button.setEnabled(ready and loaded)
        self.groups_button.setEnabled(ready and bool(self.groups))

    def _set_notice(self, messages=()):
        self.messages = list(messages)
        if self.local_flags_path is not None:
            self.messages.insert(0, f"The drive could not be written to. Flags were saved on this computer. {self.local_flags_path}")
        self.notice_text.setText(" · ".join(self.messages[:2]))
        self.details.setVisible(len(self.messages) > 1)
        self.notice.setVisible(bool(self.messages))

    def show_details(self):
        shown = self.messages[:200]
        if len(self.messages) > len(shown):
            shown.append(f"{len(self.messages) - len(shown):,} more messages")
        dialog = QMessageBox(QMessageBox.Icon.Information, "Details", self.messages[0], parent=self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDetailedText("\n".join(shown))
        dialog.exec()

    def _run(self, label, function, success, progress=None):
        if self.operation is not None:
            return
        worker = Worker(function)
        self.operation = worker
        self.progress_label.setText(label)
        self.progress.setRange(0, 0)
        self.cancel_button.setEnabled(True)
        for widget in (self.progress_label, self.progress, self.cancel_button):
            widget.show()
        self._update_controls()
        if progress is not None:
            worker.signals.progress.connect(progress, QUEUED)

        def finished(result, error):
            self.operation = None
            for widget in (self.progress_label, self.progress, self.cancel_button):
                widget.hide()
            if self.closing:
                self.close()
                return
            if isinstance(error, InterruptedError):
                self._set_notice([str(error)])
            elif error is not None:
                self._set_notice([f"{label} failed", str(error)])
            else:
                try:
                    success(result)
                except (OSError, ValueError, SQLiteError) as failure:
                    self._set_notice([f"{label} failed", str(failure)])
            self._update_controls()

        worker.signals.done.connect(finished, QUEUED)
        self.pool.start(worker)

    def _work_progress(self, values):
        count, total = values[:2]
        if len(values) > 2:
            self.progress_label.setText(values[2])
        self.progress.setRange(0, total or 1)
        self.progress.setValue(count)

    def _scan_progress(self, values):
        count, _path = values
        self.progress_label.setText(f"Scanning · {count:,} files")

    def cancel_work(self):
        if self.operation is not None:
            self.operation.stop.set()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("Cancel requested")

    def refresh_drives(self):
        if self.discovery is not None or self.closing:
            return
        worker = Worker(lambda _stop, _report: discover_drives())
        self.discovery = worker
        self._update_controls()

        def finished(drives, error):
            self.discovery = None
            if self.closing:
                self.close()
                return
            if error is not None:
                self._set_notice(["External drives could not be listed. You can open a folder directly.", str(error)])
            else:
                self.drive_picker.clear()
                self.drive_picker.addItem("Select external drive", None)
                for drive in drives:
                    self.drive_picker.addItem(f"{drive.label} · {drive.root}", drive)
            self._update_controls()

        worker.signals.done.connect(finished, QUEUED)
        self.pool.start(worker)

    def _choose_drive(self, index):
        drive = self.drive_picker.itemData(index)
        if drive is not None:
            self.open_root(drive.root)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose a folder to review",
            options=QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.open_root(path)

    def open_root(self, path):
        if self.operation is not None or self.after_save is not None or self.closing:
            return
        self._after_flags_saved(lambda: self._scan_root(path))

    def _scan_root(self, path, messages=()):
        if self.quick_look.isVisible():
            self.quick_look.close()
        self._cancel_capture_pass(detach=True)
        self.capture_sort_timer.stop()
        show_hidden = self.show_hidden.isChecked()

        def collect(stop, progress):
            drive = identify_drive(path)
            store = FlagStore(drive, self.data_dir)
            result = scan(drive.root, show_hidden, stop, progress)
            flags, local = store.load()
            check_cancel(stop)
            self.index.sync_scan(drive, result, show_hidden)
            self.index.replace_flags(drive, flags)
            cached = self.index.cached(drive.id)
            hashes = {key: value[2] for key, value in cached.items() if value[2]}
            try:
                quarantine = quarantine_inventory(drive.root, stop)
            except InterruptedError:
                raise
            except (OSError, ValueError) as error:
                result.errors.append(f"Quarantine status is unavailable. {error}")
                quarantine = None
            media = [item for item in result.items if item.category in ("Image", "Video")]
            return drive, store, result, flags, hashes, local, quarantine, media, CaptureDates(self.index, drive, media)

        def loaded(data):
            self.drive, self.store, result, self.flags, self.hashes, local, quarantine, media, self.capture = data
            self.quarantine_count = quarantine.count if quarantine else 0
            self.quarantine_size = quarantine.size if quarantine else 0
            self.scan_result = result
            self.local_flags_path = self.store.local_path if local else None
            self.revision = self.saved_revision = 0
            self.source.setText(self.drive.label)
            self.source.setToolTip(str(self.drive.root))
            self.model.items = result.items
            self.model.taken = dict(self.capture.cached)
            self.groups = []
            self.model.duplicates = {}
            self.model.flag_states = {item.key: flag.flag for item in result.items
                                if (flag := self._current_flag(item)) is not None}
            self.thumbnails.set_drive(self.drive)
            self.folder = ""
            with QSignalBlocker(self.tree.selectionModel()):
                self.folder_model.rebuild(result.directories, result.archives)
                root = self.folder_model.index(0, 0)
                self.tree.expand(root)
                self.tree.setCurrentIndex(root)
            self._apply_filter()
            self._update_summary()
            notices = list(messages)
            if result.errors:
                notices.extend([f"{len(result.errors):,} scan warnings", *result.errors])
            stale = sum(1 for item in result.items if item.key in self.flags and item.key not in self.model.flag_states)
            if stale:
                notices.append(f"{stale:,} files changed after review and need to be flagged again")
            self._set_notice(notices)
            self.grid.setFocus()
            self._start_capture_pass(media)

        self._run("Scanning", collect, loaded, self._scan_progress)

    def _start_capture_pass(self, media):
        capture = self.capture
        total = len(media)
        if total == len(capture.cached):
            return
        remaining = {item.key: item for item in media}
        first = max(0, self.grid.currentIndex().row() - 16)
        nearby = [remaining.pop(item.key) for item in self.model.visible[first:first + 64] if item.key in remaining]
        items = nearby + list(remaining.values())
        worker = Worker(partial(capture.read_all, items))
        self.capture_pass = worker
        self.capture_workers.add(worker)
        self.capture_status.setText(f"Reading capture dates {len(capture.cached):,} / {total:,}")
        self.capture_status.show()
        self.capture_cancel.setEnabled(True)
        self.capture_cancel.show()

        def progress(values):
            if self.capture_pass is worker and not self.closing:
                dates, count, total = values
                self._show_taken(dates)
                if not worker.stop.is_set():
                    self.capture_status.setText(f"Reading capture dates {count:,} / {total:,}")

        def finished(_result, _error):
            if self.capture_pass is worker:
                self._cancel_capture_pass(detach=True)
            self._capture_finished(worker)

        worker.signals.progress.connect(progress, QUEUED)
        worker.signals.done.connect(finished, QUEUED)
        self.capture_pool.start(worker)

    def _cancel_capture_pass(self, _checked=False, *, detach=False):
        if self.capture_pass is not None:
            self.capture_pass.stop.set()
            self.capture_cancel.setEnabled(False)
            self.capture_status.setText("Stopping capture dates…")
        if detach:
            self.capture_pass = None
            self.capture_status.hide()
            self.capture_cancel.hide()

    def _show_taken(self, dates):
        changed = [key for key, taken in dates.items() if taken != self.model.taken.get(key)]
        self.model.taken.update(dates)
        self.model.changed_details(changed)
        if changed and self.sort.currentData() == "taken" and not self.capture_sort_timer.isActive():
            self.capture_sort_timer.start()
        item = self.quick_look.item
        if self.quick_look.isVisible() and item is not None and item.key in dates:
            self.quick_look.set_taken(dates[item.key])

    def _resort_taken(self):
        if self.sort.currentData() == "taken" and not self.closing:
            self.model.sort_taken()
            self._sync_preview()

    def _capture_finished(self, worker):
        self.capture_workers.discard(worker)
        if self.closing:
            self.close()

    def _toggle_hidden(self, checked):
        self.settings.setValue("show_hidden", checked)
        if self.drive is not None:
            self.open_root(self.drive.root)

    def _select_folder(self, current, previous):
        self.folder = current.data(FOLDER) or ""
        self._apply_filter()

    def _apply_filter(self, *_args):
        previous = self.grid.currentIndex().data(ITEM)
        self.model.filter(self.folder, self.category.currentData(), self.flag_filter.currentData(),
                          self.search.text(), self.sort.currentData())
        if previous is not None and previous.key in self.model.rows:
            self.grid.setCurrentIndex(self.model.index(self.model.rows[previous.key]))
        self.visible_count.setText(f"{self.model.rowCount():,} files")
        self.preview_filter_dirty = False
        self._selected_count()
        self._sync_preview()

    def _resize_thumbs(self, size):
        self.settings.setValue("thumb_size", size)
        self.delegate.extent = size
        self.grid.setGridSize(self.delegate.sizeHint(None, None))
        self.grid.viewport().update()

    def _selected_count(self, *_args):
        count = len(self.grid.selectionModel().selectedRows())
        self.selection_count.setText(f"· {count:,} selected" if count else "")

    def _update_summary(self):
        count = Counter()
        sizes = Counter()
        for item in self.model.items:
            if flag := self.model.flag_states.get(item.key):
                count[flag] += 1
                sizes[flag] += item.size
        self.summary.setText(
            f"Upload {count['green']:,} ({bytes_label(sizes['green'])}) · "
            f"Delete {count['red']:,} ({bytes_label(sizes['red'])}) · "
            f"In quarantine {self.quarantine_count:,} ({bytes_label(self.quarantine_size)})"
        )

    def _current_flag(self, item):
        flag = self.flags.get(item.key)
        if flag and flag.matches(item) and flag.hash in ("", self.hashes.get(item.key, flag.hash)):
            return flag
        return None

    def _selected_item(self):
        return self.grid.currentIndex().data(ITEM) or next(iter(self._selected_items()), None)

    def _selected_items(self):
        return [row.data(ITEM) for row in self.grid.selectionModel().selectedRows()]

    def mark_selected(self, flag, *, current_only=False):
        if self._busy() or self.drive is None:
            return
        previewing = self.quick_look.isVisible()
        items = [] if current_only or previewing else self._selected_items()
        if not items and (item := self._selected_item()) is not None:
            items = [item]
        keys = []
        for item in items:
            if flag:
                self.flags[item.key] = Flag.for_item(item, flag, "manual", self.reviewer.text().strip(),
                                                     self.hashes.get(item.key, ""))
                self.model.flag_states[item.key] = flag
                keys.append(item.key)
            elif self.flags.pop(item.key, None) is not None:
                self.model.flag_states.pop(item.key, None)
                keys.append(item.key)
        if keys:
            filtered = self.flag_filter.currentData() in {"none", "green", "red"}
            if filtered and not previewing:
                self._apply_filter()
            else:
                self.preview_filter_dirty |= filtered
                self.model.changed_details(keys)
            self._sync_preview()
            self._update_summary()
            self._dirty()

    def _dirty(self):
        self.revision += 1
        self.save_timer.start()

    def _after_flags_saved(self, function):
        if self.saved_revision == self.revision:
            function()
            return
        self.after_save = function
        self._update_controls()
        self._start_save()

    def _start_save(self):
        self.save_timer.stop()
        if self.saving is not None or self.revision == self.saved_revision:
            return
        revision = self.revision
        snapshot = dict(self.flags)
        drive, store = self.drive, self.store

        def persist(_stop, _report):
            location, fallback = store.save(snapshot)
            self.index.replace_flags(drive, snapshot)
            return location, fallback

        def finished(data, error):
            self.saving = None
            if error is not None:
                self.after_save = None
                self.closing = False
                self._set_notice(["Flags could not be saved. Change a flag or retry the operation.",
                                  str(error)])
                self._update_controls()
                return
            location, fallback = data
            previous_notice = self.local_flags_path
            self.local_flags_path = location if fallback else None
            if previous_notice or fallback:
                self._set_notice()
            self.saved_revision = revision
            if self.saved_revision != self.revision:
                self._start_save()
            elif self.after_save is not None:
                callback, self.after_save = self.after_save, None
                callback()
            self._update_controls()

        worker = Worker(persist)
        worker.signals.done.connect(finished, QUEUED)
        self.saving = worker
        self.pool.start(worker)

    def run_duplicates(self):
        if self._busy() or self.drive is None:
            return
        self._after_flags_saved(self._run_duplicates)

    def _run_duplicates(self):
        drive, items = self.drive, list(self.model.items)
        flags, verify = dict(self.flags), self.verify.isChecked()

        def find(stop, progress):
            return find_duplicates(drive, items, flags, self.index, stop, progress, verify)

        def show(result):
            self.groups = result.groups
            self.hashes.update(result.hashes)
            by_key = {item.key: item for item in items}
            duplicates = {}
            suggestions = 0
            changed = False
            for group in result.groups:
                external = ", ".join(sorted({r.label for r in group.records if r.drive_id != drive.id}))
                label = f"Seen on {external}" if external else "Duplicate"
                for record in group.records:
                    if record.drive_id == drive.id:
                        duplicates[record.item.key] = label
                for key in group.suggestions:
                    if key in flags:  # Preserve an existing reviewer decision, even if the file changed.
                        continue
                    self.flags[key] = Flag.for_item(by_key[key], "red", "duplicate", self.reviewer.text().strip(),
                                                   self.hashes[key])
                    suggestions += 1
                    changed = True
            for key, digest in result.hashes.items():
                flag = self.flags.get(key)
                if flag and not flag.hash and flag.matches(by_key[key]):
                    self.flags[key] = replace(flag, hash=digest)
                    changed = True
            self.model.flag_states = {item.key: flag.flag for item in items
                                if (flag := self._current_flag(item)) is not None}
            self.model.duplicates = duplicates
            self._apply_filter()
            self._update_summary()
            if changed:
                self._dirty()
            self._set_notice([f"{len(result.groups):,} duplicate groups · {suggestions:,} automatic Delete flags",
                              *result.errors])

        self._run("Duplicate search", find, show, self._work_progress)

    def show_groups(self):
        if not self.groups:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Duplicate groups")
        dialog.resize(860, 570)
        layout = QVBoxLayout(dialog)
        tree = QTreeWidget()
        tree.setUniformRowHeights(True)
        tree.setHeaderLabels(["File or ZIP entry", "Drive", "Size", "Flag / Keeper"])
        tree.setColumnWidth(0, 395)
        tree.setColumnWidth(1, 145)
        tree.setColumnWidth(2, 84)
        layout.addWidget(tree)
        for group in self.groups:
            keeper = group.keeper.item.identity if group.keeper else "Keeper needs review"
            parent = QTreeWidgetItem([keeper, "", bytes_label(group.records[0].item.size),
                                      f"{len(group.records):,} files"])
            tree.addTopLevelItem(parent)
            for record in group.records:
                status = self.model.flag_states.get(record.item.key, "") if record.drive_id == self.drive.id else record.flag
                labels = {"green": "Upload", "red": "Delete"}
                details = [labels[status]] if status else []
                if record is group.keeper:
                    details.append("Suggested keeper")
                if record.drive_id in group.unverified:
                    details.append("Index match")
                child = QTreeWidgetItem([record.item.identity, record.label, bytes_label(record.item.size),
                                         " · ".join(details)])
                child.setToolTip(0, record.item.identity)
                if record.drive_id == self.drive.id:
                    child.setData(0, Qt.ItemDataRole.UserRole, list(record.item.key))
                parent.addChild(child)

        def go_to(row, _col):
            key = row.data(0, Qt.ItemDataRole.UserRole)
            if not key:
                return
            self.tree.setCurrentIndex(self.folder_model.index(0, 0))
            self.category.setCurrentIndex(0)
            self.flag_filter.setCurrentIndex(0)
            self.search.clear()
            self._apply_filter()
            if tuple(key) in self.model.rows:
                index = self.model.index(self.model.rows[tuple(key)])
                self.grid.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self.grid.scrollTo(index)
                self.grid.setFocus()
                dialog.accept()

        tree.itemDoubleClicked.connect(go_to)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def preview_selected(self):
        if self.quick_look.isVisible():
            self.quick_look.close()
            return
        if self._busy() or self.drive is None or self.model.rowCount() == 0:
            return
        if not self.grid.currentIndex().isValid():
            self.grid.setCurrentIndex(self.model.index(0))
        self.quick_look.show()
        self.preview_escape.setEnabled(True)
        self._sync_preview()
        self.quick_look.raise_()
        self.quick_look.activateWindow()

    def _sync_preview(self, *_args):
        if not self.quick_look.isVisible():
            return
        current = self.grid.currentIndex()
        item = current.data(ITEM)
        flag = self.model.flag_states.get(item.key, "") if item else ""
        self.quick_look.set_item(self.drive.root, item, current.row() + 1, self.model.rowCount(), flag)
        self.quick_look.set_taken(self.model.taken.get(item.key) if item else None)
        self._request_preview_taken(item)

    def _request_preview_taken(self, item):
        if self.capture_request is not None and item is not None and self.capture_request[1] == item.key:
            return
        self._cancel_preview_taken()
        if item is None or item.category not in ("Image", "Video") or item.key in self.model.taken:
            return
        capture = self.capture

        def read(stop, report):
            with ItemReader() as reader:
                taken = capture.read(item, reader, stop)
            report(taken)
            capture.save()

        def progress(values):
            if self.capture is capture and not self.closing:
                self._show_taken({item.key: values[0]})

        def finished(_result, _error):
            if self.capture_request is not None and self.capture_request[0] is worker:
                self.capture_request = None
            self._capture_finished(worker)

        worker = Worker(read)
        self.capture_request = worker, item.key
        self.capture_workers.add(worker)
        worker.signals.progress.connect(progress, QUEUED)
        worker.signals.done.connect(finished, QUEUED)
        self.pool.start(worker, 1)

    def _cancel_preview_taken(self):
        if self.capture_request is not None:
            self.capture_request[0].stop.set()
            self.capture_request = None

    def _preview_navigate(self, key):
        if not self.grid.currentIndex().isValid() and self.model.rowCount():
            self.grid.setCurrentIndex(self.model.index(0))
        else:
            event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.grid, event)

    def _preview_closed(self, _result):
        self.preview_escape.setEnabled(False)
        self._cancel_preview_taken()
        if self.preview_filter_dirty and not self.closing:
            self._apply_filter()
        if not self.closing:
            self.activateWindow()
            self.grid.setFocus()

    def open_selected(self, item=None):
        item = item or self._selected_item()
        if self._busy() or self.drive is None or item is None:
            return
        root = self.drive.root

        def copy(stop, _report):
            directory = tempfile.mkdtemp(dir=self.viewer_files.name)
            try:
                return copy_for_view(root, item, directory, stop)
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise

        def display(path):
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self._set_notice([f"Could not open the default app for {item.name}"])

        self._run("Preparing a copy for the default app", copy, display)

    def confirm_quarantine(self):
        if self._busy() or self.drive is None:
            return
        self.quick_look.close()
        root = self.drive.root
        flags, hashes = dict(self.flags), dict(self.hashes)

        def collect(stop, _report):
            items, errors = flagged_items(root, flags, "red", stop, include_entries=False)
            eligible = []
            for item in items:
                if flags[item.key].hash in ("", hashes.get(item.key, flags[item.key].hash)):
                    eligible.append(item)
                else:
                    errors.append(f"File contents changed after review: {item.identity}")
            return eligible, errors

        def confirm(result):
            items, errors = result
            if not items:
                self._set_notice(["No loose files are eligible for quarantine. ZIP entries cannot be moved individually.", *errors])
                return
            message = (f"Move {len(items):,} files ({bytes_label(sum(item.size for item in items))}) to "
                       "_drive_review/quarantine on this drive? You can restore them to their original locations.")
            if errors:
                message += f"\n{len(errors):,} unavailable paths will be skipped."
            answer = QMessageBox.question(self, "Move red files to quarantine…", message,
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                def move(stop, report):
                    result = quarantine_items(root, items, stop, report)
                    return MoveResult(result.moved, result.size, errors + result.errors)

                self._run("Moving to quarantine", move, partial(self._moved, "Moved to quarantine"), self._work_progress)

        self._after_flags_saved(lambda: self._run("Checking quarantine candidates", collect, confirm))

    def confirm_undo(self):
        if self._busy() or self.drive is None:
            return
        answer = QMessageBox.question(self, "Restore quarantined files…", "Restore quarantined files to their original locations?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            root = self.drive.root
            self._after_flags_saved(lambda: self._run(
                "Restoring from quarantine", lambda stop, report: undo_quarantine(root, stop, report),
                partial(self._moved, "Restored"), self._work_progress))

    def confirm_permanent_delete(self):
        if self._busy() or self.drive is None:
            return
        self.quick_look.close()
        root = self.drive.root

        def confirm(inventory):
            self.quarantine_count, self.quarantine_size = inventory.count, inventory.size
            self._update_summary()
            candidates = inventory.candidates
            if not candidates:
                self._set_notice(["No unchanged, logged files are eligible for permanent deletion.",
                                  *inventory.errors])
                return
            count = len(candidates)
            size = sum(candidate.size for candidate in candidates)
            message = (f"Permanently delete {count:,} files ({size:,} bytes) from quarantine? "
                       "This cannot be undone.\n\n")
            if inventory.errors:
                message += f"{len(inventory.errors):,} unavailable or changed paths will be skipped.\n\n"
            message += f"Type {count} to confirm."
            if not self._confirm_delete_count(message, count):
                return

            def delete(stop, report):
                result = delete_quarantine(root, candidates, stop, report)
                result.errors[:0] = inventory.errors
                return result

            self._run("Permanently deleting quarantined files", delete,
                      partial(self._moved, "Permanently deleted"), self._work_progress)

        self._after_flags_saved(lambda: self._run(
            "Checking quarantined files", lambda stop, _report: quarantine_inventory(root, stop), confirm))

    def _confirm_delete_count(self, message, count):
        dialog = QDialog(self)
        dialog.setWindowTitle("Delete quarantine permanently…")
        layout = QVBoxLayout(dialog)
        prompt = QLabel(message)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        entry = QLineEdit()
        entry.setAccessibleName("File count")
        layout.addWidget(entry)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        delete = buttons.button(QDialogButtonBox.StandardButton.Ok)
        delete.setText("Delete permanently")
        delete.setEnabled(False)
        entry.textChanged.connect(lambda text: delete.setEnabled(text.strip() == str(count)))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(460, dialog.sizeHint().height())
        entry.setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted and entry.text().strip() == str(count)

    def _moved(self, action, result):
        messages = [f"{action} {result.moved:,} files · {bytes_label(result.size)}", *result.errors]
        self._scan_root(self.drive.root, messages)

    def choose_upload(self):
        if self._busy() or self.drive is None:
            return
        destination = QFileDialog.getExistingDirectory(
            self, "Choose a local folder for upload files",
            options=QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if destination:
            root = self.drive.root
            flags = dict(self.flags)

            def prepare(stop, report):
                items, errors = flagged_items(root, flags, "green", stop)
                if not items and errors:
                    return [f"No files are eligible for upload. Review {len(errors):,} skipped paths.", *errors]
                path = prepare_upload(root, destination, items, flags, stop, report)
                notice = f"Upload files copied · {path}"
                if errors:
                    notice += f" · {len(errors):,} paths skipped"
                return [notice, *errors]

            self._after_flags_saved(lambda: self._run(
                "Copy green files for upload…", prepare, self._set_notice, self._work_progress))

    def choose_inventory(self):
        if self._busy() or self.drive is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Save Excel inventory", "DriveReview.xlsx", "Excel workbook (*.xlsx)",
            options=QFileDialog.Option.DontUseNativeDialog)
        if destination:
            if not Path(destination).suffix:
                destination += ".xlsx"
            drive, items = self.drive, list(self.model.items)
            capture = self.capture
            flags, hashes = dict(self.flags), dict(self.hashes)
            self._after_flags_saved(lambda: self._run(
                "Save Excel inventory", lambda stop, report: inventory_excel(
                    drive.root, destination, drive.label, items, flags, hashes, stop, report, capture=capture),
                lambda path: self._set_notice([f"Excel inventory saved · {path}"]), self._work_progress))

    def closeEvent(self, event):
        self.closing = True
        self.quick_look.close()
        self._cancel_capture_pass(detach=True)
        self._cancel_preview_taken()
        self.capture_sort_timer.stop()
        self._update_controls()
        if self.operation is not None:
            self.cancel_work()
            event.ignore()
        elif self.saved_revision != self.revision:
            self._after_flags_saved(self.close)
            event.ignore()
        elif self.discovery is not None:
            event.ignore()
        else:
            self.thumbnails.cancel()
            if self.thumbnails.pending or self.quick_look.pending or self.capture_workers:
                event.ignore()
                return
            self.settings.sync()
            event.accept()

    def _thumbnail_idle(self):
        if self.closing:
            self.close()
