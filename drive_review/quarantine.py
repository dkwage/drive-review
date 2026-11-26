"""Reversible, same-volume quarantine with a write-ahead CSV log."""

from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
import stat
import sys

from .dupes import check_cancel
from .flags import atomic_csv
from .paths import REVIEW_DIR, move_no_replace, safe_path

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
                raise ValueError(f"격리 로그 형식을 읽을 수 없습니다: {path}")
            return [LogEntry(row["rel_path"], int(row["size"]), int(row["mtime_ns"]),
                             row["state"], row["moved_at_iso"], row["restored_at_iso"]) for row in reader]
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
    if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (row.size, row.mtime_ns):
        raise OSError(f"파일이 검토 이후 변경되었습니다: {path}")
    return True


def original_path(root, rel_path, *, create_parents=False):
    if rel_path.split("/", 1)[0].casefold() == REVIEW_DIR:
        raise ValueError("내부 관리 파일은 이동할 수 없습니다.")
    return safe_path(root, rel_path, create_parents=create_parents)


def quarantine_items(root, items, stop=None, progress=lambda count, total: None):
    items = {item.rel_path: item for item in items if not item.zip_entry}
    if not items:
        return MoveResult(0, 0, [])
    with locked(root, create=True):
        log = safe_path(root, LOG)
        rows = read_log(log)
        active = {row.rel_path: row for row in rows if row.state != "restored"}
        work = []
        errors = []
        for rel_path, item in items.items():
            check_cancel(stop)
            try:
                row = active.get(rel_path)
                source = original_path(root, rel_path)
                target = safe_path(root, f"{QUARANTINE}/{rel_path}")
                if row and target.exists():
                    if not source.exists() and matching_file(target, row):
                        row.state = "quarantined"
                        row.moved_at_iso = row.moved_at_iso or now()
                        continue
                    raise FileExistsError(f"격리 위치에 파일이 이미 있습니다: {target}")
                new_row = LogEntry(rel_path, item.size, item.mtime_ns)
                if not matching_file(source, new_row):
                    raise FileNotFoundError(f"원본 파일을 찾을 수 없습니다: {rel_path}")
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
                    errors.append("격리가 취소되었습니다.")
                    break
                try:
                    source = original_path(root, row.rel_path)
                    target = safe_path(root, f"{QUARANTINE}/{row.rel_path}", create_parents=True)
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
        work = [row for row in reversed(rows) if row.state in {"planned", "quarantined"}]
        moved = size = 0
        errors = []
        try:
            for count, row in enumerate(work, 1):
                if stop is not None and stop.is_set():
                    errors.append("복구가 취소되었습니다.")
                    break
                try:
                    source = safe_path(root, f"{QUARANTINE}/{row.rel_path}")
                    if matching_file(source, row):
                        target = original_path(root, row.rel_path, create_parents=True)
                        move_no_replace(source, target)
                        moved += 1
                        size += row.size
                    elif not matching_file(original_path(root, row.rel_path), row):
                        raise FileNotFoundError(f"격리 파일을 찾을 수 없습니다: {row.rel_path}")
                    row.state = "restored"
                    row.restored_at_iso = now()
                except (OSError, ValueError) as error:
                    errors.append(f"{row.rel_path}: {error}")
                if count % 64 == 0 or count == len(work):
                    progress(count, len(work))
        finally:
            write_log(log, rows)
    return MoveResult(moved, size, errors)
