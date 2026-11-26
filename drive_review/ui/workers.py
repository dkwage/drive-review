"""Small Qt thread-pool wrappers."""

from threading import Event

from PySide6.QtCore import QObject, QRunnable, Signal


class Signals(QObject):
    done = Signal(object, object)
    progress = Signal(object)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = Signals()
        self.stop = Event()

    def run(self):
        try:
            result = self.function(self.stop, lambda *args: self.signals.progress.emit(args))
        except Exception as error:
            self.signals.done.emit(None, error)
        else:
            self.signals.done.emit(result, None)
