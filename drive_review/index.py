"""Local SQLite metadata, resumable hashes and mirrored flags."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from .flags import Flag
from .items import Item
from .scan import is_junk

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

    def replace_flags(self, drive, flags):
        with self.connect() as db:
            self._register(db, drive)
            db.execute("DELETE FROM flags WHERE drive_id = ?", (drive.id,))
            db.executemany("INSERT INTO flags VALUES (?, ?, ?, ?)", [
                (drive.id, *flag.key, json.dumps(asdict(flag), ensure_ascii=False)) for flag in flags.values()
            ])

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

    def previous(self, current_drive_id, sizes):
        sizes = sorted(sizes)
        with self.connect() as db:
            for start in range(0, len(sizes), 500):
                batch = sizes[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                query = f"""SELECT f.rel_path, f.zip_entry, f.size, f.mtime_ns, f.crc, f.archive_stamp,
                            f.drive_id, d.label, d.root, f.partial, f.digest, f.content_crc, g.data
                            FROM files f JOIN drives d ON d.id = f.drive_id
                            LEFT JOIN flags g USING(drive_id, rel_path, zip_entry)
                            WHERE f.digest <> '' AND f.drive_id <> ? AND f.size IN ({placeholders})"""
                for row in db.execute(query, (current_drive_id, *batch)):
                    rel, entry, size, mtime, crc, stamp, drive, label, root, partial, digest, content_crc, data = row
                    item = Item(rel, size, mtime, entry, crc, stamp)
                    flag = Flag(**json.loads(data)) if data else None
                    status = flag.flag if flag and flag.matches(item) and flag.hash in ("", digest) else ""
                    yield Record(item, drive, label, Path(root), partial, digest, content_crc, status)
