"""Collect the preview plugins and FFmpeg runtime for Windows wheel builds."""

from pathlib import Path

from PyInstaller.compat import is_win
from PyInstaller.utils.hooks.qt import pyside6_library_info

if is_win:
    qt = pyside6_library_info
    plugins = Path(qt.location["PluginsPath"])
    ffmpeg = plugins / "multimedia" / "ffmpegmediaplugin.dll"
    pdf = plugins / "imageformats" / "qpdf.dll"
    for plugin in (ffmpeg, pdf):
        if not plugin.is_file():
            raise FileNotFoundError(f"Required Qt preview plugin is missing from PySide6: {plugin}")
    binaries = [
        (str(ffmpeg), f"{qt.qt_rel_dir}/plugins/multimedia"),
        (str(pdf), f"{qt.qt_rel_dir}/plugins/imageformats"),
    ]
    # Qt also looks for the FFmpeg libraries at runtime. Put them on the frozen app's DLL path.
    library_dirs = {Path(qt.location["BinariesPath"]), qt.qt_lib_dir}
    for library in ("avcodec", "avformat", "avutil", "swresample", "swscale"):
        files = {file for directory in library_dirs for file in directory.glob(f"{library}-*.dll")}
        if not files:
            raise FileNotFoundError(f"The PySide6 FFmpeg runtime is missing {library}.")
        binaries.extend((str(file), ".") for file in sorted(files))
