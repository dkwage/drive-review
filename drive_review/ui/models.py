"""Lightweight list and folder-tree models."""

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QStyle

from ..items import bytes_label

ITEM = Qt.ItemDataRole.UserRole + 1
FOLDER = Qt.ItemDataRole.UserRole + 2


class ReviewModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.visible = []
        self.flag_states = {}
        self.duplicates = {}
        self.rows = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.visible)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.visible):
            return None
        item = self.visible[index.row()]
        if role == ITEM:
            return item
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return item.name
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            values = [item.identity, bytes_label(item.size)]
            if flag := self.flag_states.get(item.key):
                values.append("업로드" if flag == "green" else "격리")
            if duplicate := self.duplicates.get(item.key):
                values.append(duplicate)
            if item.warning:
                values.append(item.warning)
            return "\n".join(values)
        return None

    def filter(self, folder, category, flag, search, sort):
        text = search.strip().casefold()
        matches = []
        prefix = folder + "/"
        for item in self.items:
            if folder and item.folder != folder and not item.folder.startswith(prefix):
                continue
            if category and item.category != category:
                continue
            current = self.flag_states.get(item.key, "none")
            if flag == "duplicate":
                if item.key not in self.duplicates:
                    continue
            elif flag and current != flag:
                continue
            if text and text not in item.identity.casefold():
                continue
            matches.append(item)

        if sort == "size":
            matches.sort(key=lambda item: (item.size, item.name.casefold()))
        elif sort == "date":
            matches.sort(key=lambda item: (item.mtime_ns, item.name.casefold()), reverse=True)
        elif sort == "folder":
            matches.sort(key=lambda item: (item.folder.casefold(), item.name.casefold()))
        else:
            matches.sort(key=lambda item: item.name.casefold())

        self.beginResetModel()
        self.visible = matches
        self.rows = {item.key: row for row, item in enumerate(matches)}
        self.endResetModel()

    def changed_flags(self, keys):
        rows = sorted({self.rows[key] for key in keys if key in self.rows})
        if rows:
            self.dataChanged.emit(self.index(rows[0]), self.index(rows[-1]),
                                  [Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole])


@dataclass(slots=True)
class Folder:
    name: str
    path: str
    parent: "Folder | None"
    children: list["Folder"] = field(default_factory=list)
    row: int = 0


class FolderModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = Folder("전체 파일", "", None)
        self.nodes = {"": self.root}
        self.archives = set()
        style = QApplication.style()
        self.folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.archive_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)

    def rebuild(self, directories, archives):
        root = Folder("전체 파일", "", None)
        nodes = {"": root}
        for path in directories | archives:
            parent = root
            step = ""
            for name in path.split("/") if path else []:
                step = f"{step}/{name}" if step else name
                node = nodes.get(step)
                if node is None:
                    node = nodes[step] = Folder(name, step, parent)
                    parent.children.append(node)
                parent = node
        for node in nodes.values():
            node.children.sort(key=lambda child: child.name.casefold())
            for row, child in enumerate(node.children):
                child.row = row
        self.beginResetModel()
        self.root, self.nodes, self.archives = root, nodes, archives
        self.endResetModel()

    def index(self, row, column, parent=QModelIndex()):
        if column != 0 or row < 0:
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(0, 0, self.root) if row == 0 else QModelIndex()
        children = parent.internalPointer().children
        return self.createIndex(row, 0, children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        parent = index.internalPointer().parent
        if parent is None:
            return QModelIndex()
        return self.createIndex(parent.row, 0, parent)

    def rowCount(self, parent=QModelIndex()):
        return len(parent.internalPointer().children) if parent.isValid() else 1

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return node.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.archive_icon if node.path in self.archives else self.folder_icon
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.path or node.name
        if role == FOLDER:
            return node.path
        return None
