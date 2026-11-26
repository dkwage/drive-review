"""Windows Shell thumbnail provider using the native COM and GDI interfaces."""

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import sys
from uuid import UUID

from PIL import Image

from .paths import display_path


class GUID(c.Structure):
    _fields_ = [("data1", w.DWORD), ("data2", w.WORD), ("data3", w.WORD), ("data4", c.c_ubyte * 8)]


class BITMAP(c.Structure):
    _fields_ = [("type", w.LONG), ("width", w.LONG), ("height", w.LONG),
                ("stride", w.LONG), ("planes", w.WORD), ("bits_per_pixel", w.WORD),
                ("bits", c.c_void_p)]


class BITMAPINFO(c.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits_per_pixel", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("x_pixels_per_meter", w.LONG),
                ("y_pixels_per_meter", w.LONG), ("colors_used", w.DWORD),
                ("colors_important", w.DWORD), ("color", w.DWORD)]


IMAGE_FACTORY = UUID("bcc18b79-ba16-442f-80c4-8a59c30c463b")


@lru_cache(maxsize=1)
def _apis():
    ole, shell, gdi = (c.WinDLL(name) for name in ("ole32", "shell32", "gdi32"))
    ole.CoInitializeEx.argtypes = (c.c_void_p, w.DWORD)
    ole.CoInitializeEx.restype = w.LONG
    ole.CoUninitialize.argtypes = ()
    ole.CoUninitialize.restype = None
    shell.SHCreateItemFromParsingName.argtypes = (w.LPCWSTR, c.c_void_p, c.POINTER(GUID),
                                                  c.POINTER(c.c_void_p))
    shell.SHCreateItemFromParsingName.restype = w.LONG
    gdi.GetObjectW.argtypes = (w.HANDLE, c.c_int, c.c_void_p)
    gdi.GetObjectW.restype = c.c_int
    gdi.CreateCompatibleDC.argtypes = (w.HDC,)
    gdi.CreateCompatibleDC.restype = w.HDC
    gdi.GetDIBits.argtypes = (w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p,
                            c.POINTER(BITMAPINFO), w.UINT)
    gdi.GetDIBits.restype = c.c_int
    gdi.DeleteDC.argtypes = (w.HDC,)
    gdi.DeleteDC.restype = w.BOOL
    gdi.DeleteObject.argtypes = (w.HANDLE,)
    gdi.DeleteObject.restype = w.BOOL
    return ole, shell, gdi


def _read_bitmap(gdi, handle):
    bitmap = BITMAP()
    if not gdi.GetObjectW(handle, c.sizeof(bitmap), c.byref(bitmap)):
        return None
    width, height = bitmap.width, abs(bitmap.height)
    if not (0 < width <= 8192 and 0 < height <= 8192):
        return None
    info = BITMAPINFO(size=40, width=width, height=-height, planes=1, bits_per_pixel=32)
    data = c.create_string_buffer(width * height * 4)
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        return None
    try:
        if gdi.GetDIBits(dc, handle, 0, height, data, c.byref(info), 0) != height:
            return None
        return Image.frombytes("RGB", (width, height), data.raw, "raw", "BGRX")
    finally:
        gdi.DeleteDC(dc)


def shell_thumbnail(path, size):
    if sys.platform != "win32":
        return None
    ole, shell, gdi = _apis()
    init = ole.CoInitializeEx(None, 2)  # Apartment-threaded COM on the thumbnail worker.
    if init < 0 and init & 0xFFFFFFFF != 0x80010106:  # RPC_E_CHANGED_MODE
        return None
    try:
        # Shell parsing often expects the normal DOS/UNC spelling. Try the long spelling as well.
        factory = GUID.from_buffer_copy(IMAGE_FACTORY.bytes_le)
        pointer = c.c_void_p()
        for spelling in dict.fromkeys((display_path(path), str(path))):
            if shell.SHCreateItemFromParsingName(spelling, None, c.byref(factory),
                                                 c.byref(pointer)) >= 0:
                break
        else:
            return None
        vtable = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
        release = c.WINFUNCTYPE(w.ULONG, c.c_void_p)(vtable[2])
        try:
            get_image = c.WINFUNCTYPE(w.LONG, c.c_void_p, w.SIZE, c.c_int,
                                      c.POINTER(w.HBITMAP))(vtable[3])
            handle = w.HBITMAP()
            if get_image(pointer, w.SIZE(size, size), 0x8, c.byref(handle)) < 0 or not handle:
                return None  # SIIGBF_THUMBNAILONLY: the caller supplies its own category icon.
            try:
                return _read_bitmap(gdi, handle)
            finally:
                gdi.DeleteObject(handle)
        finally:
            release(pointer)
    finally:
        if init >= 0:
            ole.CoUninitialize()
