"""Reversible, same-volume quarantine with a write-ahead CSV log."""

from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import batched
import os
import stat
import sys

from .dupes import check_cancel
from .flags import atomic_csv
from .paths import REVIEW_DIR, is_link, move_no_replace, native_path, relative_parts, safe_path

QUARANTINE = f"{REVIEW_DIR}/quarantine"
LOG = f"{REVIEW_DIR}/quarantine_log.csv"
COLUMNS = ("rel_path", "size", "mtime_ns", "state", "moved_at_iso", "restored_at_iso")


@dataclass(slots=True)
class LogEntry:
    rel_path: str
    size: int
    mtime_ns: int
    state: str = "planned"
    moved_at_iso: str = ""
    restored_at_iso: str = ""


@dataclass(slots=True)
class MoveResult:
    moved: int
    size: int
    errors: list[str]


@dataclass(frozen=True, slots=True)
class DeleteCandidate:
    rel_path: str
    size: int
    mtime_ns: int
    moved_at_iso: str

    @classmethod
    def from_row(cls, row):
        return cls(row.rel_path, row.size, row.mtime_ns, row.moved_at_iso)


@dataclass(slots=True)
class QuarantineInventory:
    count: int
    size: int
    candidates: list[DeleteCandidate]
    errors: list[str]


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked(root, create):
    path = safe_path(root, f"{REVIEW_DIR}/quarantine.lock", create_parents=create)
    with path.open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read_log(path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != list(COLUMNS):
                raise ValueError(f"Invalid quarantine log format: {path}")
            rows = []
            for row in reader:
                try:
                    if None in row or None in row.values():
                        raise ValueError
                    rows.append(LogEntry(row["rel_path"], int(row["size"]), int(row["mtime_ns"]),
                                         row["state"], row["moved_at_iso"], row["restored_at_iso"]))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"Invalid quarantine log row {reader.line_num}: {path}") from error
            return rows
    except FileNotFoundError:
        return []


def write_log(path, rows):
    atomic_csv(path, COLUMNS, (asdict(row) for row in rows))


def matching_file(path, row):
    # An entry removed externally, including a broken symlink, is handled by safe_path/lstat.
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (is_link(info) or not stat.S_ISREG(info.st_mode)
            or (info.st_size, info.st_mtime_ns) != (row.size, row.mtime_ns)):
        raise OSError(f"The file changed after review: {path}")
    return True


def original_path(root, rel_path, *, create_parents=False):
    if rel_path.split("/", 1)[0].casefold() == REVIEW_DIR:
        raise ValueError("Drive Review management files cannot be moved.")
    return safe_path(root, rel_path, create_parents=create_parents)


def quarantine_path(root, rel_path, *, create_parents=False):
    if relative_parts(rel_path)[0].casefold() == REVIEW_DIR:
        raise ValueError("Drive Review management paths are invalid in the quarantine log.")
    return safe_path(root, f"{QUARANTINE}/{rel_path}", create_parents=create_parents)


def recover_deletes(root, rows):
    changed = False
    for row in rows:
        if row.state != "deleting":
            continue
        try:
            quarantine_path(root, row.rel_path).lstat()
        except FileNotFoundError:
            row.state = "deleted"
        except (OSError, ValueError):
            continue
        else:
            row.state = "quarantined"
        changed = True
    return changed


def quarantine_items(root, items, stop=None, progress=lambda count, total: None):
    items = {item.rel_path: item for item in items if not item.zip_entry}
    if not items:
        return MoveResult(0, 0, [])
    with locked(root, create=True):
        log = safe_path(root, LOG)
        rows = read_log(log)
        recover_deletes(root, rows)
        active = {row.rel_path: row for row in rows if row.state in {"planned", "quarantined", "deleting"}}
        work = []
        errors = []
        for rel_path, item in items.items():
            check_cancel(stop)
            try:
                row = active.get(rel_path)
                source = original_path(root, rel_path)
                target = quarantine_path(root, rel_path)
                if row and target.exists():
                    if not source.exists() and matching_file(target, row):
                        row.state = "quarantined"
                        row.moved_at_iso = row.moved_at_iso or now()
                        continue
                    raise FileExistsError(f"A file already exists in quarantine: {target}")
                new_row = LogEntry(rel_path, item.size, item.mtime_ns)
                if not matching_file(source, new_row):
                    raise FileNotFoundError(f"The original file could not be found: {rel_path}")
                if row:
                    row.state = "restored"
                rows.append(new_row)
                work.append(new_row)
            except (OSError, ValueError) as error:
                errors.append(f"{rel_path}: {error}")
        write_log(log, rows)
        moved = size = 0
        try:
            for count, row in enumerate(work, 1):
                if stop is not None and stop.is_set():
                    errors.append("Quarantine move canceled.")
                    break
                try:
                    source = original_path(root, row.rel_path)
                    target = quarantine_path(root, row.rel_path, create_parents=True)
                    if not matching_file(source, row):
                        raise FileNotFoundError(row.rel_path)
                    move_no_replace(source, target)
                    row.state = "quarantined"
                    row.moved_at_iso = now()
                    moved += 1
                    size += row.size
                except (OSError, ValueError) as error:
                    errors.append(f"{row.rel_path}: {error}")
                if count % 64 == 0 or count == len(work):
                    progress(count, len(work))
        finally:
            write_log(log, rows)
    return MoveResult(moved, size, errors)


def undo_quarantine(root, stop=None, progress=lambda count, total: None):
    log = safe_path(root, LOG)
    if not log.exists():
        return MoveResult(0, 0, [])
    with locked(root, create=False):
        rows = read_log(log)
        recover_deletes(root, rows)
        work = [row for row in reversed(rows) if row.state in {"planned", "quarantined"}]
        moved = size = 0
        errors = []
        try:
            for count, row in enumerate(work, 1):
                if stop is not None and stop.is_set():
                    errors.append("Restore canceled.")
                    break
                try:
                    source = quarantine_path(root, row.rel_path)
                    if matching_file(source, row):
                        target = original_path(root, row.rel_path, create_parents=True)
                        move_no_replace(source, target)
                        moved += 1
                        size += row.size
                    elif not matching_file(original_path(root, row.rel_path), row):
                        raise FileNotFoundError(f"The quarantined file could not be found: {row.rel_path}")
                    row.state = "restored"
                    row.restored_at_iso = now()
                except (OSError, ValueError) as error:
                    errors.append(f"{row.rel_path}: {error}")
                if count % 64 == 0 or count == len(work):
                    progress(count, len(work))
        finally:
            write_log(log, rows)
    return MoveResult(moved, size, errors)


def quarantine_inventory(root, stop=None):
    """Count tracked files in quarantine and identify the ones eligible for deletion."""
    rows = read_log(safe_path(root, LOG))
    count = size = 0
    candidates = []
    errors = []
    seen = set()
    for row in reversed(rows):
        check_cancel(stop)
        if row.state not in {"quarantined", "deleting", "planned"} or row.rel_path in seen:
            continue
        seen.add(row.rel_path)
        try:
            path = quarantine_path(root, row.rel_path)
            info = path.lstat()
            if is_link(info) or not stat.S_ISREG(info.st_mode):
                raise ValueError("The quarantine path is not a regular file.")
            count += 1
            size += info.st_size
            if (info.st_size, info.st_mtime_ns) != (row.size, row.mtime_ns):
                raise OSError("The quarantined file has changed.")
            if row.state in {"quarantined", "deleting"}:
                candidates.append(DeleteCandidate.from_row(row))
        except FileNotFoundError as error:
            if row.state == "quarantined":
                errors.append(f"{row.rel_path}: {error}")
        except (OSError, ValueError) as error:
            errors.append(f"{row.rel_path}: {error}")
    return QuarantineInventory(count, size, candidates, errors)


def delete_quarantine(root, confirmed, stop=None, progress=lambda count, total: None):
    """Delete only the confirmed log rows, with recoverable intent ahead of each batch."""
    with locked(root, create=False):
        log = safe_path(root, LOG)
        rows = read_log(log)
        if recover_deletes(root, rows):
            write_log(log, rows)
        active = {row.rel_path: row for row in rows if row.state in {"planned", "quarantined", "deleting"}}
        work = []
        errors = []
        for candidate in set(confirmed):
            row = active.get(candidate.rel_path)
            if row and row.state == "quarantined" and DeleteCandidate.from_row(row) == candidate:
                work.append(row)
            else:
                errors.append(f"{candidate.rel_path}: The quarantine log changed after confirmation.")
        work.sort(key=lambda row: row.rel_path)
        deleted = size = completed = 0
        empty_dirs = set()
        quarantine = safe_path(root, QUARANTINE)
        for batch in batched(work, 1024):
            if stop is not None and stop.is_set():
                errors.append("Permanent deletion canceled.")
                break
            for row in batch:
                row.state = "deleting"
            write_log(log, rows)
            try:
                for row in batch:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        path = quarantine_path(root, row.rel_path)
                        if not matching_file(path, row):
                            raise FileNotFoundError("The quarantined file could not be found.")
                        path.unlink()
                    except (OSError, ValueError) as error:
                        errors.append(f"{row.rel_path}: {error}")
                        row.state = "quarantined"
                    else:
                        row.state = "deleted"
                        deleted += 1
                        size += row.size
                        empty_dirs.update(parent for parent in path.parents
                                          if parent.is_relative_to(quarantine))
                    completed += 1
                    if completed % 64 == 0:
                        progress(completed, len(work))
            finally:
                for row in batch:
                    if row.state == "deleting":
                        row.state = "quarantined"
                write_log(log, rows)
            progress(completed, len(work))
            if completed < len(work) and stop is not None and stop.is_set():
                errors.append("Permanent deletion canceled.")
                break
        for directory in sorted(empty_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                safe_path(root, directory.relative_to(native_path(root)).as_posix()).rmdir()
            except (OSError, ValueError):
                # A directory may still contain untracked, changed, or unconfirmed files.
                pass
    return MoveResult(deleted, size, errors)
