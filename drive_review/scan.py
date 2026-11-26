"""Background scan implementation, independent of Qt."""

from dataclasses import dataclass, field, replace
import os
import stat
from zipfile import BadZipFile, LargeZipFile

from .items import Item
from .paths import REVIEW_DIR, is_link, native_path
from .zipsource import list_zip

JUNK_NAMES = {
    "$recycle.bin", "system volume information", ".trashes", ".spotlight-v100", ".fseventsd",
    ".ds_store", "desktop.ini", "thumbs.db", "autorun.inf", "autorun.exe",
    "seagate dashboard installer", "seagate dashboard installer.exe", "seagate dashboard installer.dmg",
    "seagate setup.exe", "seagate setup.dmg", "start_here_win.exe", "start_here_mac.app",
    "start_here_mac.dmg", "seagate toolkit.exe", "seagate toolkit.dmg",
}


def is_junk(path):
    for part in path.casefold().split("/"):
        if part in JUNK_NAMES or part.startswith("._"):
            return True
        if part.startswith("seagate") and any(word in part for word in ("installer", "setup", "start here")):
            return True
    return False


@dataclass(slots=True)
class ScanResult:
    items: list[Item] = field(default_factory=list)
    directories: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    archives: set[str] = field(default_factory=set)
    incomplete: bool = False


def scan(root, show_hidden=False, stop=None, progress=lambda count, path: None):
    root = native_path(root)
    result = ScanResult()

    def check_cancel():
        if stop is not None and stop.is_set():
            raise InterruptedError("스캔이 취소되었습니다.")

    pending = [(root, "")]
    while pending:
        check_cancel()
        folder, rel_folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    check_cancel()
                    rel_path = f"{rel_folder}/{entry.name}" if rel_folder else entry.name
                    if (rel_path.split("/", 1)[0].casefold() == REVIEW_DIR
                            or (not show_hidden and is_junk(entry.name))):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append((entry.path, rel_path))
                            result.directories.add(rel_path)
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            continue
                    except OSError as error:
                        result.errors.append(str(error))
                        result.incomplete = True
                        continue
                    item = Item(rel_path, info.st_size, info.st_mtime_ns)
                    result.items.append(item)
                    if len(result.items) % 128 == 0:
                        progress(len(result.items), rel_path)
                    if entry.name.lower().endswith(".zip"):
                        try:
                            members, directories = list_zip(entry.path, item, stop)
                        except InterruptedError:
                            raise
                        except (OSError, BadZipFile, LargeZipFile, ValueError, UnicodeError, NotImplementedError) as error:
                            warning = f"{rel_path}: {error}"
                            result.errors.append(warning)
                            result.items[-1] = replace(item, warning=warning)
                            continue
                        result.archives.add(rel_path)
                        for directory in directories:
                            if show_hidden or not is_junk(directory):
                                result.directories.add(f"{rel_path}/{directory}")
                        for member in members:
                            if show_hidden or not is_junk(member.zip_entry):
                                result.items.append(member)
                                result.directories.add(member.folder)
        except InterruptedError:
            raise
        except OSError as error:
            if not rel_folder or not root.is_dir():
                raise OSError(f"원본 드라이브에 접근할 수 없습니다: {root}") from error
            result.errors.append(str(error))
            result.incomplete = True
    check_cancel()
    if not root.is_dir():
        raise OSError(f"원본 드라이브 연결이 끊겼습니다: {root}")
    progress(len(result.items), "")
    return result
