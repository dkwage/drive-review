"""Virtualized grid delegate and bounded, on-demand thumbnail scheduling."""

from collections import OrderedDict

from PySide6.QtCore import QObject, QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from ..items import CATEGORY_LABELS, bytes_label
from ..pairs import CHOICE_LABELS
from ..thumbs import MAX_AUTO_MEMBER, ThumbnailCache, can_thumbnail
from .models import ITEM
from .workers import Worker

COLORS = {
    "Video": ("#E9E7FA", "#544398"),
    "Image": ("#E3F1E9", "#2B6248"),
    "Docs": ("#E6EDF8", "#385680"),
    "Music": ("#F4E6EF", "#854466"),
    "Compressed": ("#F5EBD8", "#805E28"),
    "Design": ("#F8E9E1", "#8E5334"),
    "Etc": ("#EAEDF0", "#525E69"),
}


class ThumbnailQueue(QObject):
    idle = Signal()

    def __init__(self, cache, view):
        super().__init__(view)
        self.cache = cache
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.drive = None
        self.paused = False
        self.pending = {}
        self.images = OrderedDict()
        self.repaint = QTimer(self)
        self.repaint.setSingleShot(True)
        self.repaint.setInterval(24)
        self.repaint.timeout.connect(view.viewport().update)

    def set_drive(self, drive):
        for worker in self.pending.values():
            worker.stop.set()
        self.drive = drive
        self.images.clear()

    def cancel(self):
        self.drive = None
        for worker in self.pending.values():
            worker.stop.set()

    def set_paused(self, paused):
        self.paused = paused
        if paused:
            for worker in self.pending.values():
                worker.stop.set()
        else:
            self.parent().viewport().update()

    def request(self, item, size):
        if (self.drive is None or not can_thumbnail(item)
                or (item.zip_entry and (item.category == "Video" or item.size > MAX_AUTO_MEMBER))):
            return None
        key = ThumbnailCache.key(self.drive.id, item, size)
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        if self.paused or len(self.pending) >= 20 or key in self.pending:
            return None

        drive = self.drive

        def load(stop, _report):
            data = self.cache.get(drive, item, size, stop=stop)
            return QImage.fromData(data, "PNG") if data else None

        def finished(image, error):
            self.pending.pop(key, None)
            if self.drive is drive and not isinstance(error, InterruptedError):
                self.images[key] = QPixmap.fromImage(image) if image is not None and not image.isNull() else None
                if len(self.images) > 220:
                    self.images.popitem(last=False)
            if not self.repaint.isActive():
                self.repaint.start()
            if not self.pending:
                self.idle.emit()

        worker = Worker(load)
        worker.signals.done.connect(finished, Qt.ConnectionType.QueuedConnection)
        self.pending[key] = worker
        self.pool.start(worker)
        return None


class ReviewDelegate(QStyledItemDelegate):
    def __init__(self, thumbnails, parent=None):
        super().__init__(parent)
        self.thumbnails = thumbnails
        self.extent = 144

    def sizeHint(self, option, index):
        return QSize(self.extent + 24, self.extent + 74)

    def paint(self, painter, option, index):
        item = index.data(ITEM)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        fg = QColor("#172D2A")
        painter.setPen(QPen(QColor("#237E69" if selected or focused else "#DCE5E0"), 2 if selected else 1))
        painter.setBrush(QColor("#E9F6F0" if selected else "#FFFFFF"))
        painter.drawRoundedRect(rect, 8, 8)

        thumb = QRect(rect.x() + 6, rect.y() + 6, rect.width() - 12, self.extent)
        background, label = COLORS[item.category]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#F0F3F1"))
        painter.drawRoundedRect(thumb, 5, 5)
        dpr = painter.device().devicePixelRatioF()
        pixmap = self.thumbnails.request(item, round(self.extent * dpr))
        if pixmap:
            bounds = pixmap.size()
            bounds.scale(thumb.size(), Qt.AspectRatioMode.KeepAspectRatio)
            source = QRect(0, 0, pixmap.width(), pixmap.height())
            dest = QRect(thumb.center() - QRect(0, 0, bounds.width(), bounds.height()).center(), bounds)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(dest, pixmap, source)
        else:
            icon = QRect(0, 0, min(76, thumb.width() - 8), 34)
            icon.moveCenter(thumb.center())
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(icon, 7, 7)
            painter.setPen(QColor(label))
            painter.drawText(icon, Qt.AlignmentFlag.AlignCenter, CATEGORY_LABELS[item.category])

        model = index.model()
        pair = model.pair_for(item)
        flag = model.flag_for(item)
        duplicate = model.duplicate_for(item)
        chips = []
        if flag:
            chips.append(("✓", "#087654") if flag == "green" else ("✕", "#B43C38"))
        if duplicate:
            chips.append(("=", "#6D509A"))
        if item.warning:
            chips.append(("!", "#8B5C1A"))
        for i, (text, color) in enumerate(chips):
            badge = QRect(thumb.right() - 25 - i * 25, thumb.y() + 5, 22, 22)
            self.draw_badge(painter, badge, text, color)
        if pair:
            width = painter.fontMetrics().horizontalAdvance("RAW+JPG") + 12
            badge = QRect(thumb.x() + 5, thumb.bottom() - 26, width, 22)
            self.draw_badge(painter, badge, "RAW+JPG", "#305C56")

        painter.setPen(fg)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        name = QRect(rect.x() + 8, thumb.bottom() + 4, rect.width() - 16, 21)
        painter.drawText(name, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(
            item.name, Qt.TextElideMode.ElideRight, name.width()))
        painter.setFont(option.font)
        painter.setPen(QColor("#53635F"))
        subtitle = QRect(name.x(), name.bottom(), name.width(), 20)
        subtitle_text = bytes_label(item.size)
        if pair and flag == "green":
            subtitle_text = f"↑ {CHOICE_LABELS[model.pair_choices[pair.jpg.key]]} · {subtitle_text}"
        if duplicate:
            subtitle_text += f" · {duplicate}"
        painter.drawText(subtitle, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(
            subtitle_text, Qt.TextElideMode.ElideRight, subtitle.width()))
        painter.restore()

    @staticmethod
    def draw_badge(painter, rect, text, color):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
