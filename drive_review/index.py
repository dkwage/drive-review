"""Local SQLite metadata, resumable hashes and mirrored flags."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from .flags import Flag
from .items import Item
from .scan import is_junk
from .taken import Taken

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
  id TEXT PRIMARY KEY, label TEXT NOT NULL, serial TEXT NOT NULL, root TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  drive_id TEXT NOT NULL, rel_path TEXT NOT NULL, zip_entry TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, crc INTEGER, archive_stamp TEXT NOT NULL,
  partial TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '', content_crc INTEGER,
  PRIMARY KEY (drive_id, rel_path, zip_entry)
);
CREATE INDEX IF NOT EXISTS files_by_size ON files(size) WHERE digest <> '';
CREATE TABLE IF NOT EXISTS flags (
  drive_id TEXT NOT NULL, rel_path TEXT NOT NULL, zip_entry TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (drive_id, rel_path, zip_entry)
);
CREATE TABLE IF NOT EXISTS capture_dates (
  drive_id TEXT NOT NULL, rel_path TEXT NOT NULL, zip_entry TEXT NOT NULL, stamp TEXT NOT NULL,
  text TEXT, utc_offset TEXT, sort_key INTEGER, subsec TEXT, make TEXT, model TEXT,
  PRIMARY KEY (drive_id, rel_path, zip_entry, stamp)
);
CREATE TABLE IF NOT EXISTS cleared_pair_choices (
  drive_id TEXT NOT NULL, raw_path TEXT NOT NULL, raw_entry TEXT NOT NULL,
  jpg_path TEXT NOT NULL, jpg_entry TEXT NOT NULL, raw_stamp TEXT NOT NULL, jpg_stamp TEXT NOT NULL,
  choice TEXT NOT NULL,
  PRIMARY KEY (drive_id, raw_path, raw_entry, jpg_path, jpg_entry, raw_stamp, jpg_stamp)
);
"""


@dataclass(slots=True)
class Record:
    item: Item
    drive_id: str
    label: str
    root: Path
    partial: str = ""
    digest: str = ""
    content_crc: int | None = None
    flag: str = ""


class Index:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(capture_dates)")}
            missing = {"subsec", "make", "model"} - columns
            if missing:
                for column in sorted(missing):
                    db.execute(f"ALTER TABLE capture_dates ADD COLUMN {column} TEXT")
                # A legacy date was read without camera tags. Re-read it before pairing.
                db.execute("DELETE FROM capture_dates")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _register(db, drive):
        db.execute("""INSERT INTO drives VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                   label=excluded.label, serial=excluded.serial, root=excluded.root""",
                   (drive.id, drive.label, drive.serial, str(drive.root)))

    def sync_scan(self, drive, result, show_hidden):
        rows = [(drive.id, *item.key, item.size, item.mtime_ns, item.crc, item.archive_stamp)
                for item in result.items]
        present = {item.key for item in result.items}
        with self.connect() as db:
            self._register(db, drive)
            db.executemany("""INSERT INTO files(drive_id, rel_path, zip_entry, size, mtime_ns, crc, archive_stamp)
                              VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(drive_id, rel_path, zip_entry)
                              DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, crc=excluded.crc,
                              archive_stamp=excluded.archive_stamp, partial='', digest='', content_crc=NULL
                              WHERE files.size <> excluded.size OR files.mtime_ns <> excluded.mtime_ns
                              OR files.crc IS NOT excluded.crc OR files.archive_stamp <> excluded.archive_stamp""",
                           rows)
            if not result.incomplete:
                stored = db.execute("SELECT rel_path, zip_entry FROM files WHERE drive_id = ?", (drive.id,))
                db.executemany("DELETE FROM files WHERE drive_id = ? AND rel_path = ? AND zip_entry = ?",
                               [(drive.id, rel, entry) for rel, entry in stored
                                if (rel, entry) not in present
                                and (show_hidden or not (is_junk(rel) or is_junk(entry)))])

    def replace_flags(self, drive, flags, *, cleared_choices=None):
        with self.connect() as db:
            self._register(db, drive)
            db.execute("DELETE FROM flags WHERE drive_id = ?", (drive.id,))
            db.executemany("INSERT INTO flags VALUES (?, ?, ?, ?)", [
                (drive.id, *flag.key, json.dumps(asdict(flag), ensure_ascii=False)) for flag in flags.values()
            ])
            if cleared_choices is not None:
                db.execute("DELETE FROM cleared_pair_choices WHERE drive_id = ?", (drive.id,))
                db.executemany("INSERT INTO cleared_pair_choices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               [(drive.id, *stamp, choice) for stamp, choice in cleared_choices.items()])

    def cleared_pair_choices(self, drive_id):
        with self.connect() as db:
            rows = db.execute("""SELECT raw_path, raw_entry, jpg_path, jpg_entry, raw_stamp, jpg_stamp, choice
                                 FROM cleared_pair_choices WHERE drive_id = ?""", (drive_id,))
            return {tuple(stamp): choice for *stamp, choice in rows}

    def cached(self, drive_id):
        with self.connect() as db:
            rows = db.execute("""SELECT rel_path, zip_entry, size, mtime_ns, crc, archive_stamp,
                                 partial, digest, content_crc FROM files WHERE drive_id = ?""", (drive_id,))
            return {(rel, entry): ((size, mtime, crc, stamp), partial, digest, content_crc)
                    for rel, entry, size, mtime, crc, stamp, partial, digest, content_crc in rows}

    def update_hashes(self, records):
        with self.connect() as db:
            db.executemany("""UPDATE files SET partial = ?, digest = ?, content_crc = ?
                              WHERE drive_id = ? AND rel_path = ? AND zip_entry = ? AND size = ?
                              AND mtime_ns = ? AND crc IS ? AND archive_stamp = ?""",
                           [(r.partial, r.digest, r.content_crc, r.drive_id, *r.item.key, r.item.size,
                             r.item.mtime_ns, r.item.crc, r.item.archive_stamp) for r in records])

    def cached_dates(self, drive_id, items):
        stamps = {item.key: item.stamp for item in items}
        with self.connect() as db:
            rows = db.execute("""SELECT rel_path, zip_entry, stamp, text, utc_offset, sort_key, subsec, make, model
                                 FROM capture_dates WHERE drive_id = ?""", (drive_id,))
            return {(rel, entry): Taken(*values) if values[0] is not None else None
                    for rel, entry, stamp, *values in rows if stamps.get((rel, entry)) == stamp}

    def save_dates(self, drive_id, dates):
        with self.connect() as db:
            db.executemany("""INSERT OR REPLACE INTO capture_dates
                              (drive_id, rel_path, zip_entry, stamp, text, utc_offset, sort_key, subsec, make, model)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
                (drive_id, *item.key, item.stamp, taken.text, taken.offset, taken.sort_key,
                 taken.subsec, taken.make, taken.model) if taken else
                (drive_id, *item.key, item.stamp, None, None, None, None, None, None) for item, taken in dates.items()
            ])

    def previous(self, drive, sizes):
        sizes = sorted(sizes)
        with self.connect() as db:
            for start in range(0, len(sizes), 500):
                batch = sizes[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                query = f"""SELECT f.rel_path, f.zip_entry, f.size, f.mtime_ns, f.crc, f.archive_stamp,
                            f.drive_id, d.label, d.root, f.partial, f.digest, f.content_crc, g.data
                            FROM files f JOIN drives d ON d.id = f.drive_id
                            LEFT JOIN flags g USING(drive_id, rel_path, zip_entry)
                            WHERE f.digest <> '' AND f.drive_id <> ? AND (? = '' OR d.serial <> ?)
                            AND f.size IN ({placeholders})"""
                for row in db.execute(query, (drive.id, drive.serial, drive.serial, *batch)):
                    rel, entry, size, mtime, crc, stamp, drive_id, label, root, partial, digest, content_crc, data = row
                    item = Item(rel, size, mtime, entry, crc, stamp)
                    flag = Flag(**json.loads(data)) if data else None
                    status = flag.flag if flag and flag.matches(item) and flag.hash in ("", digest) else ""
                    yield Record(item, drive_id, label, Path(root), partial, digest, content_crc, status)
