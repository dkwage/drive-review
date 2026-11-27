"""Modeless Quick Look panel and its Qt viewers."""

from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSignalBlocker, QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QFontDatabase, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QDialog, QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider, QStackedWidget, QStyle,
    QVBoxLayout, QWidget,
)

from ..items import bytes_label, date_label
from ..paths import display_path
from ..preview import PreviewKind, load_preview
from ..preview_windows import show_handler
from .workers import Worker

QUEUED = Qt.ConnectionType.QueuedConnection
ARROWS = {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down}
FLAGS = {Qt.Key.Key_Z: "green", Qt.Key.Key_X: "red", Qt.Key.Key_C: ""}
ZOOM_IN = {Qt.Key.Key_Plus, Qt.Key.Key_Equal}


class ImageItem(QGraphicsItem):
    def __init__(self, image):
        super().__init__()
        self.image = image
        self.bounds = QRectF(image.rect())

    def boundingRect(self):
        return self.bounds

    def paint(self, painter, option, widget=None):
        painter.drawImage(self.bounds, self.image)


class ImageView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        self.fitted = True

    def set_image(self, image):
        self.scene().clear()
        self.scene().setSceneRect(QRectF())
        if image is not None:
            item = ImageItem(image)
            self.scene().addItem(item)
            self.scene().setSceneRect(item.boundingRect())
            self.fit()

    def fit(self):
        self.fitted = True
        # Keep the bars hidden while fitting to avoid resize recursion at their visibility threshold.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if self.scene().items() and self.viewport().width() > 4 and self.viewport().height() > 4:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, steps):
        if not self.scene().items():
            return
        scale = self.transform().m11()
        factor = 1.25 ** steps
        if 0.01 <= scale * factor <= 32:
            self.fitted = False
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()

    def wheelEvent(self, event):
        if event.angleDelta().y():
            self.zoom(event.angleDelta().y() / 120)
        event.accept()


class MediaView(QWidget):
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.video, 1)
        self.audio_title = QLabel("Audio", alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.audio_title, 1)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        self.play = QPushButton("Play")
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Playback position")
        self.time = QLabel("0:00 / 0:00")
        self.mute = QPushButton("Unmute")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName("Volume")
        self.volume.setRange(0, 100)
        self.volume.setValue(65)
        self.volume.setFixedWidth(85)
        for widget in (self.play, self.seek, self.time, self.mute, self.volume):
            controls.addWidget(widget, int(widget is self.seek))

        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.volume.value() / 100)
        self.audio.setMuted(True)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.durationChanged.connect(self.update_time)
        self.player.positionChanged.connect(self.update_time)
        self.player.seekableChanged.connect(self.seek.setEnabled)
        self.player.playbackStateChanged.connect(self.update_state)
        self.player.errorOccurred.connect(lambda _error, message: self.failed.emit(message))
        self.play.clicked.connect(self.toggle)
        self.seek.sliderMoved.connect(self.seek_to)
        self.seek.sliderReleased.connect(lambda: self.seek_to(self.seek.value()))
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.mute.clicked.connect(lambda: self.audio.setMuted(not self.audio.isMuted()))
        self.audio.mutedChanged.connect(lambda muted: self.mute.setText("Unmute" if muted else "Mute"))

    def load(self, path, video):
        self.video.setVisible(video)
        self.audio_title.setVisible(not video)
        self.seek.setEnabled(False)
        self.player.setSource(QUrl.fromLocalFile(display_path(path)))

    def unload(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def update_state(self, state):
        self.play.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    @staticmethod
    def timestamp(milliseconds):
        minutes, seconds = divmod(milliseconds // 1000, 60)
        hours, minutes_of_hour = divmod(minutes, 60)
        return f"{hours}:{minutes_of_hour:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    def update_time(self, _unused):
        duration = self.player.duration()
        position = self.player.position()
        if not self.seek.isSliderDown():
            with QSignalBlocker(self.seek):
                self.seek.setRange(0, 10_000 if duration else 0)
                self.seek.setValue(round(position * 10_000 / duration) if duration else 0)
        self.time.setText(f"{self.timestamp(position)} / {self.timestamp(duration)}")

    def seek_to(self, value):
        self.player.setPosition(round(self.player.duration() * value / 10_000))


class QuickLook(QDialog):
    navigate = Signal(object)
    flag_requested = Signal(str)
    open_requested = Signal(object)
    idle = Signal()

    def __init__(self, parent, settings):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Quick Look")
        self.setMinimumSize(460, 340)
        self.resize(980, 720)
        self.settings = settings
        self.item = self.root = self.data = self.active = None
        self.native_host = None
        self.pending = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.load_timer = QTimer(self, singleShot=True, interval=55)
        self.load_timer.timeout.connect(self.load)
        self.build()
        if geometry := settings.value("quick_look_geometry"):
            self.restoreGeometry(geometry)
        QApplication.instance().installEventFilter(self)

    @staticmethod
    def label(text=""):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def build(self):
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.name = self.label()
        font = self.name.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 3)
        self.name.setFont(font)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.flag = self.label()
        self.position = self.label()
        header.addWidget(self.name, 1)
        header.addWidget(self.flag)
        header.addWidget(self.position)
        layout.addLayout(header)
        self.details = self.label()
        self.path = self.label()
        self.path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        layout.addWidget(self.path)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.message_page = QWidget()
        message_layout = QVBoxLayout(self.message_page)
        message_layout.addStretch()
        self.icon = self.label()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = self.label()
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = QProgressBar()
        message_layout.addWidget(self.icon)
        message_layout.addWidget(self.message)
        message_layout.addWidget(self.progress)
        message_layout.addStretch()
        self.image = ImageView()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.pdf = QPdfView()
        self.empty_pdf = QPdfDocument(self)
        self.pdf.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf.pageNavigator().currentPageChanged.connect(self.update_pdf_page)
        self.media = None
        for view in (self.message_page, self.image, self.text, self.pdf):
            self.stack.addWidget(view)

        footer = QHBoxLayout()
        self.page_count = self.label()
        self.fit = QPushButton("Fit")
        self.fit.setToolTip("Fit the image to the window")
        self.fit.clicked.connect(self.image.fit)
        self.open_button = QPushButton("Open with default app")
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.item))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(self.page_count)
        footer.addStretch()
        for text, flag, tip in (("Z  Upload", "green", "Flag for upload (Z)"),
                                ("X  Delete", "red", "Flag for deletion (X)"),
                                ("C  Clear", "", "Clear the flag (C)")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(partial(self.flag_requested.emit, flag))
            footer.addWidget(button)
        hint = self.label()
        hint.setText("←→ next · K play/pause · Space/Esc close")
        footer.addWidget(hint)
        footer.addWidget(self.fit)
        footer.addWidget(self.open_button)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.set_view(self.message_page)

    def set_view(self, widget):
        self.stack.setCurrentWidget(widget)
        self.fit.setVisible(widget is self.image)
        self.page_count.setVisible(widget is self.pdf)

    def update_pdf_page(self, page):
        if (document := self.pdf.document()) and document.pageCount():
            self.page_count.setText(f"Page {max(0, page) + 1} / {document.pageCount()}")

    def set_item(self, root, item, position, count, flag):
        if item != self.item or root != self.root:
            self.clear()
            self.root, self.item = root, item
            self.name.setText(item.name if item else "No file selected")
            self.name.setToolTip(item.name if item else "")
            path = ""
            if item:
                path = display_path(Path(root) / item.rel_path)
                if item.zip_entry:
                    path += f"::{item.zip_entry}"
            self.set_taken(None)
            self.path.setText(path)
            self.path.setToolTip(path)
            self.open_button.setEnabled(item is not None)
            self.show_message("Loading preview…" if item else "Select a file to preview.")
            if item:
                self.load_timer.start()
        self.position.setText(f"{position:,} / {count:,}" if item else "")
        self.flag.setText({"green": "Upload", "red": "Delete"}.get(flag, "Unflagged") if item else "")
        self.flag.setStyleSheet({"green": "color: #176440", "red": "color: #9A3734"}.get(flag, ""))

    def set_taken(self, taken):
        if self.item is None:
            self.details.clear()
            return
        item = self.item
        parts = [bytes_label(item.size)]
        if taken:
            parts.append(f"Taken {taken.label}")
        parts.extend((f"Created {date_label(item.ctime_ns)}", f"Modified {date_label(item.mtime_ns)}"))
        self.details.setText(" · ".join(parts))

    def show_message(self, message, *, file_icon=False):
        self.message.setText(message)
        self.progress.hide()
        self.icon.setVisible(file_icon)
        if file_icon:
            self.icon.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon).pixmap(112))
        self.set_view(self.message_page)

    def clear(self):
        self.load_timer.stop()
        if self.active is not None:
            self.active.stop.set()
            self.active = None
        view = self.stack.currentWidget()
        self.set_view(self.message_page)
        if view is self.pdf:
            self.pdf.setDocument(self.empty_pdf)
        elif view is self.image:
            self.image.set_image(None)
        elif view is self.text:
            self.text.clear()
        elif view is self.media:
            self.media.unload()
        if self.data is not None:
            self.data.release()
            self.data = None
        if self.native_host is not None:
            self.native_host.hide()
            self.stack.removeWidget(self.native_host)
            self.native_host = None

    def start(self, function, done, progress):
        worker = Worker(function)
        self.pending.add(worker)
        self.active = worker

        def finished(result, error):
            self.pending.remove(worker)
            current = worker is self.active
            if current:
                self.active = None
            done(current, result, error)
            if not self.pending:
                self.idle.emit()

        worker.signals.progress.connect(
            lambda values: progress(*values) if worker is self.active else None, QUEUED)
        worker.signals.done.connect(finished, QUEUED)
        self.pool.start(worker)

    def load(self, *, use_handler=True):
        function = partial(load_preview, self.root, self.item, self.thread(), use_handler=use_handler)
        self.start(function, self.loaded, self.copy_progress)

    def copy_progress(self, copied, size):
        action = "Extracting ZIP entry" if self.item.zip_entry else "Preparing a local preview copy"
        self.message.setText(f"{action} · {bytes_label(copied)} / {bytes_label(size)}")
        self.progress.show()
        self.progress.setRange(0, 1000)
        self.progress.setValue(round(copied * 1000 / size) if size else 1000)

    def loaded(self, current, data, error):
        if not current:
            if data is not None:
                data.release()
            return
        if error is not None:
            self.show_message(f"Preview unavailable. {error}", file_icon=True)
        elif data.handler:
            self.start_handler(data)
        else:
            self.data = data
            if data.image is not None:
                self.set_view(self.image)
                self.image.set_image(data.image)
            elif data.text is not None:
                self.text.setPlainText(data.text)
                self.set_view(self.text)
            elif data.pdf is not None:
                self.pdf.setDocument(data.pdf)
                self.pdf.pageNavigator().jump(0, QPointF())
                self.update_pdf_page(0)
                self.set_view(self.pdf)
            elif data.kind in {PreviewKind.VIDEO, PreviewKind.AUDIO}:
                if self.media is None:
                    self.media = MediaView()
                    self.media.failed.connect(self.media_error)
                    self.stack.addWidget(self.media)
                self.set_view(self.media)
                self.media.load(data.path, data.kind == PreviewKind.VIDEO)
            else:
                self.show_message("No inline preview is available for this file.", file_icon=True)

    def media_error(self, error):
        if self.stack.currentWidget() is self.media:
            self.show_message(f"Media could not be played. {error}", file_icon=True)
            self.media.unload()
            if self.data is not None:
                self.data.release()
                self.data = None

    def start_handler(self, data):
        host = QWidget()
        host.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.native_host = host
        self.stack.addWidget(host)
        self.set_view(host)
        hwnd = int(host.winId())

        def run(stop, report):
            return show_handler(data.handler, data.path, hwnd, stop, report)

        def finished(current, _success, _error):
            if self.native_host is host:
                self.native_host = None
                self.stack.removeWidget(host)
            host.hide()
            host.deleteLater()
            data.release()
            if current:
                self.show_message("Loading preview…")
                self.load(use_handler=False)

        self.start(run, finished, self.native_key)

    def native_key(self, name):
        if action := self.key_action(getattr(Qt.Key, f"Key_{name}")):
            action()

    def eventFilter(self, watched, event):
        if (not self.isVisible() or event.type() not in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress}
                or not isinstance(watched, QWidget) or not (watched is self or self.isAncestorOf(watched))):
            return super().eventFilter(watched, event)
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
                                | Qt.KeyboardModifier.AltModifier):
            return super().eventFilter(watched, event)

        action = self.key_action(event.key())
        if action is None:
            return super().eventFilter(watched, event)
        event.accept()
        if event.type() == QEvent.Type.KeyPress:
            action()
        return True

    def key_action(self, key):
        if key in {Qt.Key.Key_Space, Qt.Key.Key_Escape}:
            return self.close
        if key in ARROWS:
            return partial(self.navigate.emit, key)
        if key in FLAGS:
            return partial(self.flag_requested.emit, FLAGS[key])
        if key == Qt.Key.Key_K and self.stack.currentWidget() is self.media:
            return self.media.toggle
        if key in ZOOM_IN | {Qt.Key.Key_Minus} and self.stack.currentWidget() is self.image:
            return partial(self.image.zoom, 1 if key in ZOOM_IN else -1)
        return None

    def closeEvent(self, event):
        self.settings.setValue("quick_look_geometry", self.saveGeometry())
        self.clear()
        self.root = self.item = None
        super().closeEvent(event)
