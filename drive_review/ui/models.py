"""Lightweight list and folder-tree models."""

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QStyle

from ..items import bytes_label, date_label
from ..pairs import CHOICE_LABELS

ITEM = Qt.ItemDataRole.UserRole + 1
FOLDER = Qt.ItemDataRole.UserRole + 2


class ReviewModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.visible = []
        self.flag_states = {}
        self.duplicates = {}
        self.taken = {}
        self.rows = {}
        self.group_pairs = True
        self.pairs = {}
        self.pair_choices = {}

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
            pair = self.pair_for(item)
            if pair:
                values = ["RAW+JPG"]
                statuses = {"green": "Upload", "red": "Delete"}
                for member in pair.items:
                    status = statuses.get(self.flag_states.get(member.key), "Unflagged")
                    values.append(f"{member.identity} · {bytes_label(member.size)} · {status}")
                values.append(f"Upload choice · {CHOICE_LABELS[self.pair_choices[pair.jpg.key]]}")
            else:
                values = [item.identity, bytes_label(item.size)]
            if taken := self.taken.get(item.key):
                values.append(f"Taken {taken.label}")
            values.append(f"Created {date_label(item.ctime_ns)} · Modified {date_label(item.mtime_ns)}")
            if not pair and (flag := self.flag_for(item)):
                values.append("Upload" if flag == "green" else "Delete")
            if duplicate := self.duplicate_for(item):
                values.append(duplicate)
            if item.warning:
                values.append(item.warning)
            return "\n".join(values)
        return None

    def pair_for(self, item):
        return self.pairs.get(item.key) if self.group_pairs else None

    def flag_for(self, item):
        if pair := self.pair_for(item):
            raw, jpg = (self.flag_states.get(member.key) for member in pair.items)
            return "green" if "green" in (raw, jpg) else raw or jpg
        return self.flag_states.get(item.key)

    def duplicate_for(self, item):
        if pair := self.pair_for(item):
            labels = (self.duplicates.get(member.key) for member in pair.items)
            return " · ".join(dict.fromkeys(label for label in labels if label))
        return self.duplicates.get(item.key)

    def filter(self, folder, category, flag, search, sort):
        text = search.strip().casefold()
        matches = []
        prefix = folder + "/"
        for item in self.items:
            pair = self.pair_for(item)
            if pair and item == pair.raw:
                continue
            if folder and item.folder != folder and not item.folder.startswith(prefix):
                continue
            if category and item.category != category:
                continue
            if flag == "duplicate":
                if not self.duplicate_for(item):
                    continue
            elif pair and flag in {"green", "red"}:
                if all(self.flag_states.get(member.key) != flag for member in pair.items):
                    continue
            elif flag and (self.flag_for(item) or "none") != flag:
                continue
            if (text and text not in item.identity.casefold()
                    and (pair is None or text not in pair.raw.identity.casefold())):
                continue
            matches.append(item)

        if sort == "size":
            matches.sort(key=lambda item: (item.size, item.name.casefold()))
        elif sort == "date":
            matches.sort(key=lambda item: (item.mtime_ns, item.name.casefold()), reverse=True)
        elif sort == "created":
            matches.sort(key=lambda item: (item.ctime_ns, item.name.casefold()), reverse=True)
        elif sort == "taken":
            matches.sort(key=self._taken_order)
        elif sort == "folder":
            matches.sort(key=lambda item: (item.folder.casefold(), item.name.casefold()))
        else:
            matches.sort(key=lambda item: item.name.casefold())

        reset = matches != self.visible
        if reset:
            self.beginResetModel()
        self.visible = matches
        self._index_rows()
        if reset:
            self.endResetModel()
        return reset

    def _index_rows(self):
        self.rows = {}
        for row, item in enumerate(self.visible):
            self.rows[item.key] = row
            if pair := self.pair_for(item):
                self.rows[pair.raw.key] = row

    def _taken_order(self, item):
        date = self.taken.get(item.key)
        return date is None, -date.sort_key if date else 0, item.name.casefold()

    def sort_taken(self):
        matches = sorted(self.visible, key=self._taken_order)
        if matches == self.visible:
            return
        self.layoutAboutToBeChanged.emit()
        previous = self.persistentIndexList()
        keys = [self.visible[index.row()].key for index in previous]
        self.visible = matches
        self._index_rows()
        self.changePersistentIndexList(previous, [self.index(self.rows[key]) for key in keys])
        self.layoutChanged.emit()

    def changed_details(self, keys):
        rows = sorted({self.rows[key] for key in keys if key in self.rows})
        if rows:
            self.dataChanged.emit(self.index(rows[0]), self.index(rows[-1]))


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
        self.root = Folder("All files", "", None)
        self.nodes = {"": self.root}
        self.archives = set()
        style = QApplication.style()
        self.folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.archive_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)

    def rebuild(self, directories, archives):
        root = Folder("All files", "", None)
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
