"""Drive Review's main window and asynchronous review workflow."""

from collections import Counter
from dataclasses import replace
from functools import partial
from pathlib import Path
from sqlite3 import Error as SQLiteError
import tempfile

from PySide6.QtCore import QItemSelectionModel, QSettings, QThreadPool, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSlider, QSplitter, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..drives import discover_drives, identify_drive
from ..dupes import check_cancel, find_duplicates
from ..export import inventory_excel, prepare_upload
from ..flags import Flag, FlagStore
from ..index import Index
from ..items import CATEGORY_LABELS, bytes_label
from ..paths import local_data_dir
from ..quarantine import quarantine_items, undo_quarantine
from ..scan import scan
from ..thumbs import ThumbnailCache, copy_for_view
from .grid import ReviewDelegate, ThumbnailQueue
from .models import FOLDER, ITEM, FolderModel, ReviewModel
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


class PreviewImage(QLabel):
    def __init__(self, pixmap):
        super().__init__()
        self.original = pixmap
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def resizeEvent(self, event):
        self.setPixmap(self.original.scaled(event.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
        super().resizeEvent(event)


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
        self.operation = None
        self.discovery = None
        self.saving = None
        self.after_save = None
        self.closing = False
        self.revision = self.saved_revision = 0

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(450)
        self.save_timer.timeout.connect(self._start_save)
        self.filter_timer = QTimer(self)
        self.filter_timer.setInterval(150)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.timeout.connect(self._apply_filter)

        self._build()
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
        self.source = QLabel("검토할 드라이브 또는 폴더를 선택하세요", objectName="source")
        self.drive_picker = self._combo([("외장 드라이브 선택", None)])
        self.drive_picker.setMinimumWidth(165)
        self.drive_picker.activated.connect(self._choose_drive)
        self.refresh_button = self._button("새로 고침", self.refresh_drives)
        self.folder_button = self._button("폴더 열기", self.choose_folder, "primary")
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
        self.duplicate_button = self._button("중복 찾기", self.run_duplicates)
        self.groups_button = self._button("중복 그룹", self.show_groups)
        self.verify = QCheckBox("바이트 비교")
        self.verify.setToolTip("해시가 일치하는 파일의 내용을 직접 비교합니다.")
        self.verify.setChecked(self.settings.value("verify", False, type=bool))
        self.verify.toggled.connect(partial(self.settings.setValue, "verify"))
        self.upload_button = self._button("업로드 세트 준비", self.choose_upload, "primary")
        self.upload_button.setToolTip("Prepare upload set")
        self.inventory_button = self._button("Excel 목록 내보내기", self.choose_inventory)
        self.quarantine_button = self._button("빨간 파일 격리", self.confirm_quarantine, "danger")
        self.undo_button = self._button("격리 실행 취소", self.confirm_undo)
        for widget in (self.duplicate_button, self.groups_button, self.verify):
            actions.addWidget(widget)
        actions.addStretch()
        for widget in (self.upload_button, self.inventory_button, self.quarantine_button, self.undo_button):
            actions.addWidget(widget)
        lines.addLayout(actions)

        filters = QHBoxLayout()
        self.category = self._combo([("모든 분류", "")] +
                                    [(label, value) for value, label in CATEGORY_LABELS.items()])
        self.flag_filter = self._combo([("모든 플래그", ""), ("미지정", "none"), ("업로드", "green"),
                                       ("격리", "red"), ("중복", "duplicate")])
        self.sort = self._combo([("이름순", "name"), ("크기순", "size"),
                                 ("수정 시간순", "date"), ("폴더순", "folder")])
        self.search = QLineEdit(placeholderText="파일명 또는 경로 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("파일 검색")
        self.show_hidden = QCheckBox("시스템 항목 표시")
        self.show_hidden.setChecked(self.settings.value("show_hidden", False, type=bool))
        self.show_hidden.toggled.connect(self._toggle_hidden)
        self.thumb_size = QSlider(Qt.Orientation.Horizontal)
        self.thumb_size.setRange(104, 224)
        self.thumb_size.setSingleStep(8)
        self.thumb_size.setPageStep(16)
        self.thumb_size.setValue(self.settings.value("thumb_size", 144, type=int))
        self.thumb_size.setFixedWidth(84)
        self.thumb_size.setAccessibleName("썸네일 크기")
        self.thumb_size.valueChanged.connect(self._resize_thumbs)
        self.search.textChanged.connect(lambda _text: self.filter_timer.start())
        for combo in (self.category, self.flag_filter, self.sort):
            combo.currentIndexChanged.connect(self._apply_filter)
            filters.addWidget(combo)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.show_hidden)
        filters.addWidget(QLabel("썸네일"))
        filters.addWidget(self.thumb_size)
        lines.addLayout(filters)
        layout.addWidget(controls)

        self.notice = QFrame(objectName="notice")
        notices = QHBoxLayout(self.notice)
        notices.setContentsMargins(15, 4, 15, 4)
        self.notice_text = QLabel(objectName="noticeText")
        self.notice_text.setWordWrap(True)
        self.details = self._button("상세 정보", self.show_details)
        notices.addWidget(self.notice_text, 1)
        notices.addWidget(self.details)
        layout.addWidget(self.notice)
        self.notice.hide()

        split = QSplitter()
        split.setChildrenCollapsible(False)
        sidebar = QWidget()
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(13, 10, 8, 9)
        left.addWidget(QLabel("폴더", objectName="section"))
        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.folder_model = FolderModel(self.tree)
        self.tree.setModel(self.folder_model)
        self.tree.selectionModel().currentChanged.connect(self._select_folder)
        left.addWidget(self.tree, 1)
        self.reviewer = QLineEdit(self.settings.value("reviewer", "", type=str))
        self.reviewer.setAccessibleName("검토자 이름")
        self.reviewer.setPlaceholderText("검토자 이름")
        self.reviewer.textChanged.connect(partial(self.settings.setValue, "reviewer"))
        left.addWidget(QLabel("검토자", objectName="section"))
        left.addWidget(self.reviewer)
        split.addWidget(sidebar)

        gallery = QWidget()
        right = QVBoxLayout(gallery)
        right.setContentsMargins(5, 9, 12, 8)
        gallery_top = QHBoxLayout()
        self.visible_count = QLabel("파일 0개", objectName="section")
        self.selection_count = QLabel("")
        gallery_top.addWidget(self.visible_count)
        gallery_top.addWidget(self.selection_count)
        gallery_top.addStretch()
        gallery_top.addWidget(QLabel("P 업로드   X 격리   U 해제   Space 미리 보기"))
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
        self.grid.setAccessibleName("파일 검토 그리드")
        self.model = ReviewModel(self.grid)
        self.grid.setModel(self.model)
        self.thumbnails = ThumbnailQueue(ThumbnailCache(self.data_dir / "thumbs"), self.grid)
        self.delegate = ReviewDelegate(self.thumbnails, self.grid)
        self.grid.setItemDelegate(self.delegate)
        self._resize_thumbs(self.thumb_size.value())
        self.grid.doubleClicked.connect(lambda _index: self.open_selected())
        self.grid.selectionModel().selectionChanged.connect(self._selected_count)
        for key, handler in (("P", partial(self.mark_selected, "green")),
                             ("X", partial(self.mark_selected, "red")),
                             ("U", partial(self.mark_selected, "")), ("Space", self.preview_selected),
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
        self.progress_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(127)
        self.progress.setTextVisible(False)
        self.cancel_button = self._button("취소", self.cancel_work)
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
        for button in (self.duplicate_button, self.inventory_button, self.quarantine_button,
                       self.undo_button, self.upload_button, self.verify):
            button.setEnabled(ready and loaded)
        self.groups_button.setEnabled(ready and bool(self.groups))

    def _set_notice(self, messages=()):
        self.messages = list(messages)
        if self.local_flags_path is not None:
            self.messages.insert(0, f"드라이브에 저장할 수 없어 플래그를 이 컴퓨터에 보관했습니다. {self.local_flags_path}")
        self.notice_text.setText(self.messages[0] if self.messages else "")
        self.details.setVisible(bool(self.messages) and (len(self.messages) > 1 or len(self.messages[0]) > 100))
        self.notice.setVisible(bool(self.messages))

    def show_details(self):
        shown = self.messages[:200]
        if len(self.messages) > len(shown):
            shown.append(f"외 {len(self.messages) - len(shown):,}건")
        dialog = QMessageBox(QMessageBox.Icon.Information, "상세 정보", self.messages[0], parent=self)
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
                self._set_notice([f"{label} 실패", str(error)])
            else:
                try:
                    success(result)
                except (OSError, ValueError, SQLiteError) as failure:
                    self._set_notice([f"{label} 실패", str(failure)])
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
        self.progress_label.setText(f"스캔 중 · {count:,}개")

    def cancel_work(self):
        if self.operation is not None:
            self.operation.stop.set()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("취소 요청됨")

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
                self._set_notice(["외장 드라이브를 조회할 수 없습니다. 폴더를 직접 열 수 있습니다.", str(error)])
            else:
                self.drive_picker.clear()
                self.drive_picker.addItem("외장 드라이브 선택", None)
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
        path = QFileDialog.getExistingDirectory(self, "검토할 폴더 선택")
        if path:
            self.open_root(path)

    def open_root(self, path):
        if self.operation is not None or self.after_save is not None or self.closing:
            return
        self._after_flags_saved(lambda: self._scan_root(path))

    def _scan_root(self, path):
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
            return drive, store, result, flags, hashes, local

        def loaded(data):
            self.drive, self.store, result, self.flags, self.hashes, local = data
            self.scan_result = result
            self.local_flags_path = self.store.local_path if local else None
            self.revision = self.saved_revision = 0
            self.source.setText(self.drive.label)
            self.source.setToolTip(str(self.drive.root))
            self.model.items = result.items
            self.groups = []
            self.model.duplicates = {}
            self.model.flags = {item.key: flag.flag for item in result.items
                                if (flag := self._current_flag(item)) is not None}
            self.thumbnails.set_drive(self.drive)
            self.folder = ""
            self.folder_model.rebuild(result.directories, result.archives)
            root = self.folder_model.index(0, 0)
            self.tree.expand(root)
            self.tree.setCurrentIndex(root)
            self._apply_filter()
            self._update_summary()
            messages = []
            if result.errors:
                messages = [f"스캔 중 확인이 필요한 항목 {len(result.errors):,}개", *result.errors]
            stale = sum(1 for item in result.items if item.key in self.flags and item.key not in self.model.flags)
            if stale:
                messages.append(f"검토 이후 변경되어 플래그를 다시 확인해야 하는 파일 {stale:,}개")
            self._set_notice(messages)
            self.grid.setFocus()

        self._run("스캔 중", collect, loaded, self._scan_progress)

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
        self.visible_count.setText(f"파일 {self.model.rowCount():,}개")
        self._selected_count()

    def _resize_thumbs(self, size):
        self.settings.setValue("thumb_size", size)
        self.delegate.extent = size
        self.grid.setGridSize(self.delegate.sizeHint(None, None))
        self.grid.viewport().update()

    def _selected_count(self, *_args):
        count = len(self.grid.selectionModel().selectedRows())
        self.selection_count.setText(f"· {count:,}개 선택" if count else "")

    def _update_summary(self):
        count = Counter()
        sizes = Counter()
        for item in self.model.items:
            flag = self.model.flags.get(item.key, "none")
            for key in (flag, "duplicate") if item.key in self.model.duplicates else (flag,):
                count[key] += 1
                sizes[key] += item.size
        labels = (("green", "업로드"), ("red", "격리"), ("none", "미지정"), ("duplicate", "중복"))
        self.summary.setText("   ·   ".join(f"{label} {count[key]:,}개 / {bytes_label(sizes[key])}"
                                             for key, label in labels))

    def _current_flag(self, item):
        flag = self.flags.get(item.key)
        if flag and flag.matches(item) and flag.hash in ("", self.hashes.get(item.key, flag.hash)):
            return flag
        return None

    def _selected_item(self):
        return self.grid.currentIndex().data(ITEM) or next(iter(self._selected_items()), None)

    def _selected_items(self):
        return [row.data(ITEM) for row in self.grid.selectionModel().selectedRows()]

    def mark_selected(self, flag):
        if self._busy() or self.drive is None:
            return
        items = self._selected_items()
        if not items and (item := self._selected_item()) is not None:
            items = [item]
        keys = []
        for item in items:
            if flag:
                self.flags[item.key] = Flag.for_item(item, flag, "manual", self.reviewer.text().strip(),
                                                     self.hashes.get(item.key, ""))
                self.model.flags[item.key] = flag
                keys.append(item.key)
            elif self.flags.pop(item.key, None) is not None:
                self.model.flags.pop(item.key, None)
                keys.append(item.key)
        if keys:
            if self.flag_filter.currentData() in {"none", "green", "red"}:
                self._apply_filter()
            else:
                self.model.changed_flags(keys)
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
                self._set_notice(["플래그를 저장할 수 없습니다. 플래그를 다시 변경하거나 작업을 다시 시도하세요.",
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
                label = f"다른 드라이브 · {external}" if external else "중복"
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
            self.model.flags = {item.key: flag.flag for item in items
                                if (flag := self._current_flag(item)) is not None}
            self.model.duplicates = duplicates
            self._apply_filter()
            self._update_summary()
            if changed:
                self._dirty()
            self._set_notice([f"중복 그룹 {len(result.groups):,}개 · 자동 격리 플래그 {suggestions:,}개",
                              *result.errors])

        self._run("중복 검색", find, show, self._work_progress)

    def show_groups(self):
        if not self.groups:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("중복 그룹")
        dialog.resize(860, 570)
        layout = QVBoxLayout(dialog)
        tree = QTreeWidget()
        tree.setUniformRowHeights(True)
        tree.setHeaderLabels(["파일 또는 ZIP 항목", "드라이브", "크기", "플래그 / 보관"])
        tree.setColumnWidth(0, 395)
        tree.setColumnWidth(1, 145)
        tree.setColumnWidth(2, 84)
        layout.addWidget(tree)
        for group in self.groups:
            keeper = group.keeper.item.identity if group.keeper else "보관 확인 필요"
            parent = QTreeWidgetItem([keeper, "", bytes_label(group.records[0].item.size),
                                      f"{len(group.records):,}개"])
            tree.addTopLevelItem(parent)
            for record in group.records:
                status = self.model.flags.get(record.item.key, "") if record.drive_id == self.drive.id else record.flag
                labels = {"green": "업로드", "red": "격리"}
                details = [labels[status]] if status else []
                if record is group.keeper:
                    details.append("보관 제안")
                if record.drive_id in group.unverified:
                    details.append("인덱스 일치")
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
        item = self._selected_item()
        if self._busy() or self.drive is None or item is None:
            return
        drive = self.drive

        def preview(stop, _report):
            return self.thumbnails.cache.get(drive, item, 1800, explicit=True, stop=stop)

        def display(data):
            pixmap = QPixmap()
            if not data or not pixmap.loadFromData(data, "PNG"):
                self._set_notice([f"미리 보기를 만들 수 없습니다. Enter 키로 기본 앱에서 여세요. {item.name}"])
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(item.name)
            dialog.resize(940, 660)
            layout = QVBoxLayout(dialog)
            label = PreviewImage(pixmap)
            label.setAccessibleName(item.name)
            layout.addWidget(label, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()

        self._run("미리 보기", preview, display)

    def open_selected(self):
        item = self._selected_item()
        if self._busy() or self.drive is None or item is None:
            return
        root = self.drive.root

        def copy(stop, _report):
            directory = tempfile.mkdtemp(dir=self.viewer_files.name)
            return copy_for_view(root, item, directory, stop)

        def display(path):
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self._set_notice([f"기본 앱을 열 수 없습니다. {item.name}"])

        self._run("기본 앱용 사본 준비", copy, display)

    def confirm_quarantine(self):
        if self._busy() or self.drive is None:
            return
        items = [item for item in self.model.items
                 if not item.zip_entry and self.model.flags.get(item.key) == "red"]
        if not items:
            self._set_notice(["격리할 일반 파일이 없습니다. ZIP 내부 항목은 이동 대상에서 제외됩니다."])
            return
        answer = QMessageBox.question(self, "빨간 파일 격리",
                                      f"{len(items):,}개 파일 ({bytes_label(sum(item.size for item in items))})을 "
                                      f"_drive_review/quarantine으로 옮길까요?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            root = self.drive.root
            self._after_flags_saved(lambda: self._run(
                "격리 중", lambda stop, report: quarantine_items(root, items, stop, report),
                partial(self._moved, "격리"), self._work_progress))

    def confirm_undo(self):
        if self._busy() or self.drive is None:
            return
        answer = QMessageBox.question(self, "격리 실행 취소", "격리한 파일을 원래 위치로 되돌릴까요?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            root = self.drive.root
            self._after_flags_saved(lambda: self._run(
                "복구 중", lambda stop, report: undo_quarantine(root, stop, report),
                partial(self._moved, "복구"), self._work_progress))

    def _moved(self, action, result):
        messages = [f"{action} {result.moved:,}개 · {bytes_label(result.size)}", *result.errors]
        if result.moved:
            self._scan_root(self.drive.root, messages)
        else:
            self._set_notice(messages)

    def choose_upload(self):
        if self._busy() or self.drive is None:
            return
        destination = QFileDialog.getExistingDirectory(self, "업로드 세트를 저장할 로컬 폴더")
        if destination:
            root = self.drive.root
            items, flags = list(self.model.items), dict(self.flags)
            self._after_flags_saved(lambda: self._run(
                "업로드 세트 준비", lambda stop, report: prepare_upload(root, destination, items, flags, stop, report),
                lambda path: self._set_notice([f"업로드 세트 저장 완료 · {path}"]), self._work_progress))

    def choose_inventory(self):
        if self._busy() or self.drive is None:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "Excel 목록 저장", "DriveReview.xlsx",
                                                    "Excel 통합 문서 (*.xlsx)")
        if destination:
            if not Path(destination).suffix:
                destination += ".xlsx"
            drive, items = self.drive, list(self.model.items)
            flags, hashes = dict(self.flags), dict(self.hashes)
            self._after_flags_saved(lambda: self._run(
                "Excel 목록 저장", lambda stop, report: inventory_excel(
                    drive.root, destination, drive.label, items, flags, hashes, stop, report),
                lambda path: self._set_notice([f"Excel 목록 저장 완료 · {path}"]), self._work_progress))

    def closeEvent(self, event):
        self.closing = True
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
            self.settings.sync()
            event.accept()
