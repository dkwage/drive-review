"""Host Explorer's registered preview handlers in their COM server processes."""

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import sys
from uuid import UUID

from .thumbs_windows import GUID

PREVIEW_HANDLER = UUID("8895b1c6-b41f-4c1c-a562-0d564250836f")
INITIALIZE_WITH_STREAM = UUID("b824b49d-22ac-4161-ac8a-9916e8fa3f7f")
INITIALIZE_WITH_FILE = UUID("b7d14566-0509-4cce-a71f-0a554233bd9b")
OBJECT_WITH_SITE = UUID("fc4801a3-2ba9-11cf-a229-00aa003d7352")
PREVIEW_FRAME = UUID("fec87aaf-35f9-447a-adb7-20234491401a")
UNKNOWN = UUID("00000000-0000-0000-c000-000000000046")
ASSOCSTR_SHELLEXTENSION = 16
CLSCTX_LOCAL_SERVER = 4
STGM_READ = 0
STGM_SHARE_DENY_WRITE = 0x20
WM_KEYDOWN = 0x0100
E_NOINTERFACE = -2147467262
E_NOTIMPL = -2147467263
PREVIEW_KEYS = {
    0x1B: "Escape", 0x20: "Space", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2E: "Delete",
    ord("Z"): "Z", ord("X"): "X", ord("C"): "C", ord("R"): "R",
}


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


@lru_cache(maxsize=1)
def _association_query():
    return _function(c.WinDLL("shlwapi"), "AssocQueryStringW", w.LONG,
                     w.DWORD, c.c_int, w.LPCWSTR, w.LPCWSTR, w.LPWSTR, c.POINTER(w.DWORD))


def handler_for(extension):
    """Return the registered handler CLSID, or None when unavailable."""
    if sys.platform != "win32" or not extension:
        return None
    import winreg

    interface = f"{{{PREVIEW_HANDLER}}}"
    buffer = c.create_unicode_buffer(64)
    length = w.DWORD(len(buffer))
    if _association_query()(0, ASSOCSTR_SHELLEXTENSION, extension, interface,
                            buffer, c.byref(length)) == 0:
        try:
            return UUID(buffer.value)
        except ValueError:
            pass

    def default_value(key):
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key) as entry:
            return winreg.QueryValueEx(entry, None)[0]

    try:
        progid = default_value(extension)
    except OSError:
        progid = ""
    for key in (extension, progid, rf"SystemFileAssociations\{extension}"):
        if key:
            try:
                return UUID(default_value(rf"{key}\shellex\{interface}"))
            except (OSError, ValueError, AttributeError, TypeError):
                continue
    return None


def _guid(value):
    return GUID.from_buffer_copy(value.bytes_le)


def _method(pointer, slot, result, *arguments):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    function = c.WINFUNCTYPE(result, c.c_void_p, *arguments)(table[slot])
    return lambda *values: function(pointer, *values)


def _release(pointer):
    _method(pointer, 2, w.ULONG)()


def _query(pointer, interface):
    result = c.c_void_p()
    query = _method(pointer, 0, w.LONG, c.POINTER(GUID), c.POINTER(c.c_void_p))
    if query(c.byref(_guid(interface)), c.byref(result)) >= 0:
        return result
    return None


def _initialize(pointer, path):
    initializer = _query(pointer, INITIALIZE_WITH_STREAM)
    if initializer:
        stream = c.c_void_p()
        try:
            open_stream = _function(c.WinDLL("shlwapi"), "SHCreateStreamOnFileEx", w.LONG,
                                    w.LPCWSTR, w.DWORD, w.DWORD, w.BOOL, c.c_void_p,
                                    c.POINTER(c.c_void_p))
            mode = STGM_READ | STGM_SHARE_DENY_WRITE
            if open_stream(str(path), mode, 0, False, None, c.byref(stream)) >= 0:
                initialize = _method(initializer, 3, w.LONG, c.c_void_p, w.DWORD)
                if initialize(stream, STGM_READ) >= 0:
                    return True
        finally:
            if stream:
                _release(stream)
            _release(initializer)

    initializer = _query(pointer, INITIALIZE_WITH_FILE)
    if initializer:
        try:
            initialize = _method(initializer, 3, w.LONG, w.LPCWSTR, w.DWORD)
            return initialize(str(path), STGM_READ) >= 0
        finally:
            _release(initializer)
    return False


class _PreviewFrame:
    """COM site for handlers that forward keyboard shortcuts to their host."""

    def __init__(self, on_key, stop, user):
        self.refs = 1
        interfaces = {PREVIEW_FRAME.bytes_le, UNKNOWN.bytes_le}
        get_key_state = _function(user, "GetAsyncKeyState", c.c_short, c.c_int)

        def query(this, interface, answer):
            if c.string_at(interface, 16) not in interfaces:
                answer[0] = None
                return E_NOINTERFACE
            answer[0] = this
            add_ref(this)
            return 0

        def add_ref(_this):
            self.refs += 1
            return self.refs

        def release(_this):
            self.refs -= 1
            return self.refs

        def window_context(_this, _context):
            # Forward individual keys without sharing a native accelerator table.
            return E_NOTIMPL

        def translate(_this, message):
            if not stop.is_set() and message and message.contents.message == WM_KEYDOWN:
                key = PREVIEW_KEYS.get(message.contents.wParam)
                if key and not any(get_key_state(modifier) < 0 for modifier in (0x11, 0x12, 0x5B, 0x5C)):
                    if key != "Delete" or not message.contents.lParam & (1 << 30):
                        on_key(key)
                    return 0  # S_OK
            return 1  # S_FALSE

        def callback(function, result, *arguments):
            return c.WINFUNCTYPE(result, c.c_void_p, *arguments)(function)

        self.callbacks = (
            callback(query, w.LONG, c.POINTER(GUID), c.POINTER(c.c_void_p)),
            callback(add_ref, w.ULONG),
            callback(release, w.ULONG),
            callback(window_context, w.LONG, c.c_void_p),
            callback(translate, w.LONG, c.POINTER(w.MSG)),
        )
        self.table = (c.c_void_p * len(self.callbacks))(
            *(c.cast(function, c.c_void_p) for function in self.callbacks))
        self.vtable = c.cast(self.table, c.c_void_p)
        self.pointer = c.cast(c.pointer(self.vtable), c.c_void_p)


def show_handler(clsid, path, hwnd, stop, on_key):
    """Run a single handler on a worker STA until the preview is canceled.

    The caller keeps the native child window and local preview copy alive until this returns.
    """
    if sys.platform != "win32" or stop.is_set():
        return False

    ole = c.WinDLL("ole32")
    initialize = _function(ole, "CoInitializeEx", w.LONG, c.c_void_p, w.DWORD)
    if initialize(None, 2) < 0:  # COINIT_APARTMENTTHREADED
        return False
    handler = c.c_void_p()
    site = None
    frame = None
    try:
        create = _function(ole, "CoCreateInstance", w.LONG, c.POINTER(GUID),
                           c.c_void_p, w.DWORD, c.POINTER(GUID), c.POINTER(c.c_void_p))
        if create(c.byref(_guid(clsid)), None, CLSCTX_LOCAL_SERVER,
                  c.byref(_guid(PREVIEW_HANDLER)), c.byref(handler)) < 0:
            return False
        if stop.is_set() or not _initialize(handler, path):
            return False

        user = c.WinDLL("user32")
        site = _query(handler, OBJECT_WITH_SITE)
        if site:
            frame = _PreviewFrame(on_key, stop, user)
            _method(site, 3, w.LONG, c.c_void_p)(frame.pointer)  # SetSite
        get_rect = _function(user, "GetClientRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        peek = _function(user, "PeekMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        translate = _function(user, "TranslateMessage", w.BOOL, c.POINTER(w.MSG))
        dispatch = _function(user, "DispatchMessageW", c.c_ssize_t, c.POINTER(w.MSG))
        set_window = _method(handler, 3, w.LONG, w.HWND, c.POINTER(w.RECT))
        set_rect = _method(handler, 4, w.LONG, c.POINTER(w.RECT))
        do_preview = _method(handler, 5, w.LONG)

        rect = w.RECT()
        if (stop.is_set() or not get_rect(hwnd, c.byref(rect))
                or set_window(hwnd, c.byref(rect)) < 0 or stop.is_set() or do_preview() < 0):
            return False
        size = (rect.right, rect.bottom)
        message = w.MSG()
        while not stop.wait(0.04):
            while peek(c.byref(message), None, 0, 0, 1):
                translate(c.byref(message))
                dispatch(c.byref(message))
            if not get_rect(hwnd, c.byref(rect)):
                break
            new_size = (rect.right, rect.bottom)
            if new_size != size:
                if set_rect(c.byref(rect)) < 0:
                    return False
                size = new_size
        return True
    finally:
        if handler:
            _method(handler, 6, w.LONG)()  # Unload
            if site:
                _method(site, 3, w.LONG, c.c_void_p)(None)
                _release(site)
            _release(handler)
        _function(ole, "CoUninitialize", None)()
