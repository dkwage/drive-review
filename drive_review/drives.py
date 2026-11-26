"""Removable drive discovery and stable volume identities."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys


@dataclass(frozen=True, slots=True)
class Drive:
    id: str
    label: str
    serial: str
    root: Path


def _command(args):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    return subprocess.run(args, capture_output=True, timeout=15, check=True, **options).stdout


def _windows_removable():
    import ctypes
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    return {f"{chr(65 + i)}:\\" for i in range(26)
            if mask & (1 << i) and kernel.GetDriveTypeW(f"{chr(65 + i)}:\\") == 2}


def _windows_external():
    roots = _windows_removable()
    script = ("Get-Disk -ErrorAction Stop | Where-Object { $_.BusType -in 'USB','SD','MMC','1394' } | "
              "Get-Partition | Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }")
    try:
        letters = _command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    except (OSError, subprocess.SubprocessError):
        if not roots:
            raise
        return roots
    roots.update(line.upper() + ":\\" for raw in letters.splitlines()
                 if len(line := raw.decode("ascii", errors="ignore").strip()) == 1 and line.isalpha())
    return roots


def discover_drives():
    if sys.platform == "win32":
        roots = _windows_external()
    elif sys.platform == "darwin":
        disks = plistlib.loads(_command(["diskutil", "list", "-plist", "external"]))
        roots = set()
        for disk in disks.get("AllDisks", ()):
            try:
                info = plistlib.loads(_command(["diskutil", "info", "-plist", disk]))
                if mount := info.get("MountPoint"):
                    roots.add(mount)
            except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
                continue
    else:
        roots = set()
    return [identify_drive(path) for path in sorted(roots) if Path(path).is_dir()]


@lru_cache(maxsize=32)
def external_volume(root):
    """Resolve the external mount containing root, including for folder picks."""
    if sys.platform == "darwin":
        path = Path(root).resolve()
        volumes = Path("/Volumes")
        if path.is_relative_to(volumes):
            parts = path.relative_to(volumes).parts
            if parts:
                return volumes / parts[0]
    if sys.platform == "win32":
        mount = Path(root).anchor
        try:
            return mount if mount.upper() in _windows_external() else None
        except (OSError, subprocess.SubprocessError):
            return mount if mount.upper() in _windows_removable() else None
    return None


def identify_drive(root):
    from .paths import display_path, native_path

    root = Path(display_path(native_path(root).resolve()))
    label = root.name or root.drive or "Drive"
    serial = ""
    relative = ""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        buffersize = 32768
        mount = ctypes.create_unicode_buffer(buffersize)
        name = ctypes.create_unicode_buffer(256)
        number = wintypes.DWORD()
        if (kernel.GetVolumePathNameW(str(native_path(root)), mount, buffersize)
                or kernel.GetVolumePathNameW(str(root), mount, buffersize)):
            if kernel.GetVolumeInformationW(mount, name, len(name), ctypes.byref(number),
                                            None, None, None, 0):
                volume_root = Path(display_path(mount.value))
                serial = f"{number.value:08X}"
                if root == volume_root:
                    label = name.value or label
                if root.is_relative_to(volume_root):
                    relative = str(root.relative_to(volume_root)).casefold()
    elif sys.platform == "darwin" and root.is_relative_to("/Volumes"):
        try:
            volume = external_volume(root)
            info = plistlib.loads(_command(["diskutil", "info", "-plist", str(volume)])) if volume else {}
            serial = info.get("VolumeUUID") or info.get("DiskUUID", "")
            if mount := info.get("MountPoint"):
                relative = os.path.relpath(root, mount)
                if relative == ".":
                    label = info.get("VolumeName") or label
        except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
            pass
    identity = f"{sys.platform}:{serial}:{relative}" if serial else os.path.normcase(str(root.resolve()))
    return Drive(hashlib.sha256(os.fsencode(identity)).hexdigest(), label, serial, root)
