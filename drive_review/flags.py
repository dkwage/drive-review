"""Portable CSV flags with an atomic, local fallback."""

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import tempfile

from .items import iso_mtime
from .paths import REVIEW_DIR, relative_parts, safe_path

FLAG_COLUMNS = ("rel_path", "zip_entry", "flag", "reason", "size", "mtime_iso", "hash",
                "category", "reviewer", "flagged_at_iso")


def atomic_csv(path, columns, rows, *, bom=True):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", newline="", dir=path.parent, prefix=".review-",
                                         delete=False, encoding="utf-8-sig" if bom else "utf-8") as output:
            temp = Path(output.name)
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Flag:
    rel_path: str
    zip_entry: str
    flag: str
    reason: str
    size: int
    mtime_iso: str
    hash: str
    category: str
    reviewer: str
    flagged_at_iso: str

    @property
    def key(self):
        return self.rel_path, self.zip_entry

    @classmethod
    def for_item(cls, item, flag, reason, reviewer, digest=""):
        return cls(item.rel_path, item.zip_entry, flag, reason, item.size, iso_mtime(item.mtime_ns),
                   digest, item.category, reviewer, datetime.now(timezone.utc).isoformat())

    def matches(self, item):
        return (self.size == item.size and self.mtime_iso == iso_mtime(item.mtime_ns)
                and self.key == item.key)


def read_flags(path):
    flags = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(FLAG_COLUMNS):
            raise ValueError(f"Invalid flags.csv columns: {path}")
        for row in reader:
            try:
                if None in row or None in row.values() or row["flag"] not in {"green", "red"}:
                    raise ValueError("Invalid flag")
                relative_parts(row["rel_path"])
                if row["rel_path"].split("/", 1)[0].casefold() == REVIEW_DIR:
                    raise ValueError("Drive Review management path")
                if row["zip_entry"]:
                    relative_parts(row["zip_entry"])
                row["size"] = int(row["size"])
                if row["size"] < 0:
                    raise ValueError("Invalid size")
                row["mtime_iso"] = datetime.fromisoformat(row["mtime_iso"]).astimezone(
                    timezone.utc).isoformat(timespec="microseconds")
                flag = Flag(**row)
                if flag.key in flags:
                    raise ValueError("Duplicate flag")
                flags[flag.key] = flag
            except (ValueError, TypeError) as error:
                raise ValueError(f"flags.csv line {reader.line_num}: {error}") from error
    return flags


class FlagStore:
    def __init__(self, drive, data_dir):
        self.drive = drive
        self.local_path = Path(data_dir) / f"flags-{drive.id}.csv"

    def load(self):
        try:
            local_time = self.local_path.stat().st_mtime_ns
        except FileNotFoundError:
            local_time = None
        try:
            portable = safe_path(self.drive.root, f"{REVIEW_DIR}/flags.csv")
            portable_time = portable.stat().st_mtime_ns
        except FileNotFoundError:
            portable_time = None
        except PermissionError:
            if local_time is None:
                raise
            portable_time = None
        if local_time is not None and (portable_time is None or local_time >= portable_time):
            return read_flags(self.local_path), True
        if portable_time is not None:
            try:
                return read_flags(portable), False
            except PermissionError:
                if local_time is None:
                    raise
                return read_flags(self.local_path), True
        return {}, False

    def save(self, flags):
        rows = [asdict(flags[key]) for key in sorted(flags)]
        try:
            portable = safe_path(self.drive.root, f"{REVIEW_DIR}/flags.csv", create_parents=True)
            atomic_csv(portable, FLAG_COLUMNS, rows)
        except OSError as error:
            if (error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT,
                                    errno.EIO, errno.ENODEV, errno.ENXIO)
                    and not isinstance(error, FileNotFoundError)
                    and getattr(error, "winerror", None) not in (3, 5, 19, 21, 32, 1117, 1167)):
                raise
            self.local_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_csv(self.local_path, FLAG_COLUMNS, rows)
            return self.local_path, True
        self.local_path.unlink(missing_ok=True)
        return portable, False
