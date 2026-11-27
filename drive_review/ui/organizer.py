"""Drag and drop and folder dialogs for source-local organization."""

from PySide6.QtCore import QMimeData, QTimer, Qt, Signal
from PySide6.QtGui import QDrag, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QInputDialog, QListView, QMessageBox, QTreeView, QVBoxLayout,
)

from .models import FOLDER, ITEM, FolderModel

MOVE_MIME = "application/x-drive-review-move"


def start_move(view):
    drag = QDrag(view)
    mime = QMimeData()
    mime.setData(MOVE_MIME, b"")
    drag.setMimeData(mime)
    drag.exec(Qt.DropAction.MoveAction)


class ReviewGrid(QListView):
    def __init__(self):
        super().__init__()
        self.drag_items = []
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)

    def startDrag(self, _actions):
        self.drag_items = [index.data(ITEM) for index in self.selectedIndexes()]
        if self.drag_items:
            try:
                start_move(self)
            finally:
                self.drag_items = []


class FolderTree(QTreeView):
    path_dropped = Signal(str, str)
    items_dropped = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.grid = None
        self.drag_path = ""
        self.drop_path = None
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.hover_timer = QTimer(self, singleShot=True, interval=500)
        self.hover_timer.timeout.connect(self._expand_drop_target)

    def startDrag(self, _actions):
        path = self.currentIndex().data(FOLDER)
        if self.model().is_movable(path):
            self.drag_path = path
            try:
                start_move(self)
            finally:
                self.drag_path = ""

    def dragEnterEvent(self, event):
        if (self.isEnabled() and event.mimeData().hasFormat(MOVE_MIME)
                and event.source() in (self, self.grid) and event.source() is not None):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _target(self, event):
        path = self.indexAt(event.position().toPoint()).data(FOLDER)
        if not self.isEnabled() or not self.model().is_real_folder(path):
            return None
        if event.source() is self and (
            path == self.drag_path or path.startswith(self.drag_path + "/")
            or path == self.drag_path.rpartition("/")[0]
        ):
            return None
        return path

    def dragMoveEvent(self, event):
        self._set_drop_path(self._target(event))
        if self.drop_path is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_path(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        target = self._target(event)
        self._set_drop_path(None)
        if target is None or not event.mimeData().hasFormat(MOVE_MIME):
            event.ignore()
            return
        if event.source() is self and self.drag_path:
            event.acceptProposedAction()
            self.path_dropped.emit(self.drag_path, target)
        elif event.source() is self.grid and self.grid.drag_items:
            event.acceptProposedAction()
            self.items_dropped.emit(self.grid.drag_items, target)
        else:
            event.ignore()

    def _set_drop_path(self, path):
        if self.drop_path != path:
            self.drop_path = path
            self.hover_timer.stop()
            if path is not None:
                self.hover_timer.start()
            self.viewport().update()

    def _expand_drop_target(self):
        if self.drop_path is not None:
            self.expand(self.model().index_for_path(self.drop_path))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_path is not None:
            rect = self.visualRect(self.model().index_for_path(self.drop_path))
            with QPainter(self.viewport()) as painter:
                painter.setPen(self.palette().highlight().color().darker(180))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))


def select_folder(tree, path):
    index = tree.model().index_for_path(path)
    ancestor = index.parent()
    while ancestor.isValid():
        tree.expand(ancestor)
        ancestor = ancestor.parent()
    tree.setCurrentIndex(index)
    tree.scrollTo(index)


def ask_name(parent, title, text, current=""):
    name, accepted = QInputDialog.getText(parent, title, text, text=current)
    return name if accepted else None


class FolderPicker(QDialog):
    def __init__(self, parent, directories, current, create):
        super().__init__(parent)
        self.setWindowTitle("Move to folder")
        self.resize(420, 365)
        self.directories = set(directories)
        self.create = create
        layout = QVBoxLayout(self)
        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.model = FolderModel(self.tree)
        self.tree.setModel(self.model)
        self.model.rebuild(self.directories, set())
        select_folder(self.tree, current)
        self.tree.expand(self.model.index_for_path(current))
        self.tree.doubleClicked.connect(self.accept)
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Choose folder")
        buttons.addButton("New folder", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(self._new_folder)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def folder(self):
        return self.tree.currentIndex().data(FOLDER)

    def _new_folder(self):
        name = ask_name(self, "New folder", "Folder name")
        if name is None:
            return
        try:
            path = self.create(self.folder, name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not create folder", str(error))
            return
        self.directories.add(path)
        self.model.rebuild(self.directories, set())
        select_folder(self.tree, path)
