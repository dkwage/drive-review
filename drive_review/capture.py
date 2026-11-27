"""Share capture-date reads among the background pass, Quick Look, and inventory export."""

from sqlite3 import Error as SQLiteError
from threading import Condition
from time import monotonic
from zipfile import BadZipFile

from .dupes import check_cancel
from .taken import read_taken, supports_taken
from .zipsource import ItemReader

ITEM_READ_ERRORS = (OSError, ValueError, BadZipFile, KeyError, NotImplementedError)


def read_item_taken(reader, root, item):
    if supports_taken(item.name):
        # Avoid filesystem read-ahead for scattered metadata reads in loose files.
        with reader.open(root, item, buffering=0) as source:
            return read_taken(source, item.name)
    return None


def read_pair_dates(capture, candidates, known, keys, stop):
    """Resolve missing pair metadata before accepting an automatic Delete flag."""
    pairs = {candidates[key] for key in keys if key in candidates}
    with ItemReader() as reader:
        dates = {item.key: capture.read(item, reader, stop)
                 for pair in pairs for item in pair.items if item.key not in known}
    capture.save()
    return dates


class CaptureDates:
    def __init__(self, index, drive, items):
        self.index, self.drive = index, drive
        self.cached = index.cached_dates(drive.id, items)
        self._values = dict(self.cached)
        self._pending = {}
        self._reading = set()
        self._changed = Condition()

    def read(self, item, reader, stop):
        key = item.key
        with self._changed:
            while key in self._reading:
                check_cancel(stop)
                self._changed.wait(timeout=0.05)
            check_cancel(stop)
            if key in self._values:
                return self._values[key]
            self._reading.add(key)

        try:
            taken = read_item_taken(reader, self.drive.root, item)
        except ITEM_READ_ERRORS:
            # A drive may be disconnected or a file replaced after the scan. Retry next scan.
            with self._changed:
                self._values[key] = None
            return None
        else:
            with self._changed:
                self._values[key] = taken
                self._pending[item] = taken
            return taken
        finally:
            with self._changed:
                self._reading.remove(key)
                self._changed.notify_all()

    def save(self):
        with self._changed:
            pending, self._pending = self._pending, {}
        if pending:
            try:
                self.index.save_dates(self.drive.id, pending)
            except SQLiteError:
                with self._changed:
                    self._pending.update(pending)

    def read_all(self, items, stop, report):
        total = len(items)
        count = sum(item.key in self.cached for item in items)
        batch = {}
        started = monotonic()
        try:
            with ItemReader() as reader:
                for item in items:
                    check_cancel(stop)
                    if item.key in self.cached:
                        continue
                    batch[item.key] = self.read(item, reader, stop)
                    count += 1
                    if len(batch) >= 128 or monotonic() - started >= 0.15:
                        report(batch, count, total)
                        batch = {}
                        self.save()
                        started = monotonic()
        finally:
            if batch:
                report(batch, count, total)
            self.save()
