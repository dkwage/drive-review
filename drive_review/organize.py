"""Plan and perform same-volume moves inside a review source."""

from dataclasses import dataclass, field, replace
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from uuid import uuid4

from .paths import REVIEW_DIR, is_link, move_no_replace, native_path, relative_parts, safe_path


@dataclass(slots=True)
class MoveResult:
    moved: int = 0
    size: int = 0
    errors: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)


def validate_name(name):
    if (not name or name in {".", ".."} or name.endswith((" ", "."))
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
            or PureWindowsPath(name).is_reserved() or name.casefold() == REVIEW_DIR):
        raise ValueError(f"Invalid Windows file or folder name: {name!r}")


def _source_root(root):
    root = native_path(root)
    info = root.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("The source root must be a real folder.")
    return root, info.st_dev


def _path(root, relative):
    if any(part.casefold() == REVIEW_DIR for part in relative_parts(relative)):
        raise ValueError("Drive Review management paths cannot be organized.")
    return safe_path(root, relative)


def _folder(root, relative, device):
    path = _path(root, relative) if relative else root
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"The destination must be a real folder: {relative}")
    if info.st_dev != device:
        raise ValueError("Moves must stay on the source volume.")
    return path


def create_folder(root, parent_rel, name):
    validate_name(name)
    root, device = _source_root(root)
    parent = _folder(root, parent_rel, device)
    (parent / name).mkdir()
    return _relative(parent_rel, name)


def _relative(folder, name):
    return f"{folder}/{name}" if folder else name


def _names(names, number, *, directory=False):
    if number == 1:
        return names
    if directory:
        return tuple(f"{name} ({number})" for name in names)
    return tuple(f"{path.stem} ({number}){path.suffix}" for path in map(PurePosixPath, names))


def _occupied(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _rename_case_only(source, target):
    # The destination may still resolve to the source until it vacates its old name.
    intermediate = source.with_name(f".drive-review-rename-{uuid4().hex}")
    move_no_replace(source, intermediate)
    try:
        move_no_replace(intermediate, target)
    except OSError as error:
        try:
            move_no_replace(intermediate, source)
        except OSError as failure:
            raise OSError(f"{error}. Could not restore {source.name} from {intermediate}. {failure}") from error
        raise


def _move_group(root, parent_rel, device, entries, names, result, *, directory=False):
    """Use one suffix for a group and roll back earlier renames on failure."""
    number = 1
    while True:
        parent = _folder(root, parent_rel, device)
        targets = [parent / name for name in _names(names, number, directory=directory)]
        completed = []
        try:
            if any(_occupied(target) for target in targets):
                raise FileExistsError
            for entry, target in zip(entries, targets, strict=True):
                source = _path(root, entry[0].relative_to(root).as_posix())
                move_no_replace(source, target)
                completed.append((entry, target))
        except (OSError, ValueError) as error:
            rollback_failed = False
            for entry, target in reversed(completed):
                try:
                    move_no_replace(target, entry[0])
                except OSError as failure:
                    rollback_failed = True
                    _record(result, root, parent_rel, entry, target)
                    result.errors.append(f"Could not undo move of {target.name}: {failure}")
            if isinstance(error, FileExistsError) and not rollback_failed:
                number += 1
                continue
            raise
        else:
            for entry, target in completed:
                _record(result, root, parent_rel, entry, target)
            return


def _record(result, root, parent_rel, entry, target):
    source, count, size = entry
    result.mapping[source.relative_to(root).as_posix()] = _relative(parent_rel, target.name)
    result.moved += count
    result.size += size


def move_items(root, items, destination_rel, stop=None, *, pairs=()):
    """Move scanned files flat into a folder, with optional groups of paired keys."""
    result = MoveResult()
    items = {item.key: item for item in items}
    for item in tuple(items.values()):
        if item.zip_entry:
            result.errors.append(f"ZIP entry skipped: {item.identity}")
            del items[item.key]
    if not items:
        return result
    try:
        root, device = _source_root(root)
        parent = _folder(root, destination_rel, device)
    except (OSError, ValueError) as error:
        result.errors.append(str(error))
        return result
    groups = []
    for keys in pairs:
        if all(key in items for key in keys):
            groups.append(tuple(items.pop(key) for key in keys))
    groups.extend((item,) for item in items.values())
    for group in groups:
        if stop is not None and stop.is_set():
            result.errors.append("Move canceled.")
            break
        try:
            entries = []
            for item in group:
                source = _path(root, item.rel_path)
                info = source.lstat()
                if (is_link(info) or not stat.S_ISREG(info.st_mode)
                        or (info.st_size, info.st_mtime_ns) != (item.size, item.mtime_ns)):
                    raise ValueError(f"The file changed after the scan: {item.rel_path}")
                if info.st_dev != device:
                    raise ValueError("Moves must stay on the source volume.")
                if os.path.samefile(source.parent, parent):
                    raise ValueError("The file is already in this folder.")
                entries.append((source, 1, info.st_size))
            _move_group(root, destination_rel, device, entries,
                        tuple(item.name for item in group), result)
        except (OSError, ValueError) as error:
            result.errors.append(f"{', '.join(item.rel_path for item in group)}: {error}")
    return result


def _folder_totals(root, source, device, expected, stop):
    count = size = 0
    pending = [source]
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Move canceled.")
                # Keep the directory entry's reparse attributes. On Windows its
                # device and inode fields are zero, so get identity from lstat.
                entry_info = child.stat(follow_symlinks=False)
                if is_link(entry_info) or child.name.casefold() == REVIEW_DIR:
                    raise ValueError(f"The folder contains a link, mount or management path: {child.path}")
                info = os.lstat(child.path) if not entry_info.st_dev else entry_info
                if is_link(info) or info.st_dev != device:
                    raise ValueError(f"The folder contains a link, mount or management path: {child.path}")
                if expected:
                    relative = Path(child.path).relative_to(root).as_posix()
                    if item := expected.pop(relative, None):
                        if (not stat.S_ISREG(info.st_mode)
                                or (info.st_size, info.st_mtime_ns) != (item.size, item.mtime_ns)):
                            raise ValueError(f"The file changed after the scan: {relative}")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child.path)
                elif stat.S_ISREG(info.st_mode):
                    count += 1
                    size += info.st_size
    if expected:
        raise ValueError(f"The file is missing after the scan: {next(iter(expected))}")
    return count, size


def move_path(root, source_rel, destination_rel, *, name=None, item=None, scanned=(), stop=None):
    """Move or rename a folder or a scanned loose file such as a ZIP archive."""
    result = MoveResult()
    if item is not None and item.zip_entry:
        result.errors.append(f"ZIP entry skipped: {item.identity}")
        return result
    try:
        root, device = _source_root(root)
        source = _path(root, source_rel)
        parent = _folder(root, destination_rel, device)
        info = source.lstat()
        if is_link(info) or info.st_dev != device:
            raise ValueError("Moves must stay on the source volume and cannot move links.")
        if name is not None:
            validate_name(name)
        same_parent = os.path.samefile(source.parent, parent)
        if same_parent and (name is None or name == source.name):
            raise ValueError("The path is already in this folder.")
        directory = stat.S_ISDIR(info.st_mode)
        if directory:
            if item is not None:
                raise ValueError("The file was replaced with a folder after the scan.")
            for ancestor in (parent, *parent.parents):
                if os.path.samefile(source, ancestor):
                    raise ValueError("A folder cannot be moved into itself or a descendant.")
                if ancestor == root:
                    break
            expected = {entry.rel_path: entry for entry in scanned
                        if not entry.zip_entry and entry.rel_path.startswith(source_rel + "/")}
            count, size = _folder_totals(root, source, device, expected, stop)
        else:
            if (not stat.S_ISREG(info.st_mode) or item is None or item.rel_path != source_rel
                    or (info.st_size, info.st_mtime_ns) != (item.size, item.mtime_ns)):
                raise ValueError("The file changed after the scan or has no scan record.")
            count, size = 1, info.st_size
        if stop is not None and stop.is_set():
            raise InterruptedError("Move canceled.")
        entry = source, count, size
        target = parent / (name or source.name)
        if (name is not None and same_parent and name.casefold() == source.name.casefold()
                and (not _occupied(target) or
                     (os.path.samefile(source, target) and name not in os.listdir(parent)))):
            _rename_case_only(source, target)
            _record(result, root, destination_rel, entry, target)
        else:
            _move_group(root, destination_rel, device, [entry], (name or source.name,), result, directory=directory)
    except (OSError, ValueError) as error:
        result.errors.append(f"{source_rel}: {error}")
    return result


def remap_path(relative, mapping):
    """Apply the most specific matching path once."""
    prefix = relative
    while prefix:
        if prefix in mapping:
            return mapping[prefix] + relative[len(prefix):]
        prefix = prefix.rpartition("/")[0]
    return relative


def remap_flags(flags, mapping):
    unchanged = {}
    remapped = {}
    for flag in flags.values():
        path = remap_path(flag.rel_path, mapping)
        if path == flag.rel_path:
            unchanged[flag.key] = flag
        else:
            updated = replace(flag, rel_path=path)
            remapped[updated.key] = updated
    # Moved flags take precedence over stale rows for a previously missing destination.
    return unchanged | remapped
