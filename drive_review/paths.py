"""Platform paths and safe access below the selected source or output root."""

import ctypes
import os
from pathlib import Path, PurePosixPath
import stat
import sys

REVIEW_DIR = "_drive_review"


def local_data_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DriveReview"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DriveReview"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "DriveReview"


def native_path(value):
    path = os.path.abspath(value)
    if sys.platform != "win32" or path.startswith("\\\\?\\"):
        return Path(path)
    if path.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + path[2:])
    return Path("\\\\?\\" + path)


def display_path(value):
    path = os.fspath(value)
    if sys.platform == "win32":
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        if path.startswith("\\\\?\\"):
            return path[4:]
    return path


def relative_parts(value):
    path = PurePosixPath(value)
    parts = path.parts
    if (not parts or path.is_absolute() or path.as_posix() != value
            or ".." in parts or "\\" in value or ":" in value or "\x00" in value):
        raise ValueError(f"Unsafe relative path: {value!r}")
    if sys.platform == "win32":
        for part in parts:
            if (part.endswith((" ", ".")) or any(ord(c) < 32 or c in '<>"|?*' for c in part)
                    or Path(part).is_reserved()):
                raise ValueError(f"Path is invalid on Windows: {value!r}")
    return parts


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def safe_path(root, relative, *, create_parents=False):
    """Reject symlinks and Windows reparse points below a trusted root."""
    parts = relative_parts(relative)
    current = native_path(root)
    if not current.is_dir():
        raise FileNotFoundError(f"Cannot access folder: {root}")
    for i, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_parents or i == len(parts) - 1:
                continue
            try:
                current.mkdir()
            except FileExistsError:
                pass
            info = current.lstat()
        if is_link(info):
            raise ValueError(f"Links are not allowed in the path: {relative}")
    return current


def outside_source(root, destination):
    source = native_path(root).resolve()
    target = native_path(destination).resolve()
    if target.is_relative_to(source):
        raise ValueError("Choose a location outside the source drive or folder.")
    # An external volume remains protected when a subfolder was selected.
    from .drives import external_volume
    volume = external_volume(root)
    if volume and target.is_relative_to(native_path(volume).resolve()):
        raise ValueError("Choose a location outside the source drive.")
    return target


def move_no_replace(source, destination):
    """Rename on the same volume without replacing an existing file or directory."""
    if sys.platform == "win32":
        os.rename(source, destination)
    elif sys.platform == "darwin":
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 4):  # RENAME_EXCL
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    else:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):  # RENAME_NOREPLACE
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
