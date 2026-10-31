"""Every Windows call the MapleJet code makes, in one place.

Windows APIs are reached through ctypes (and winsound for the alert sound), so no
new package is needed. Each function also works elsewhere through a plain
fallback, so the rest of the code, and its tests, run on Linux.

Off Windows, and in an unfrozen run on Windows, STRIPES_HX_USER, STRIPES_HX_COMPUTER
and STRIPES_HX_SIDS (comma-separated) set the identity for development and tests.
A frozen exe ignores them: there it always comes from the Windows sign-in.
"""

from __future__ import annotations

import getpass
import os
import socket
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import BinaryIO

from hx_model import Identity

# Windows constants
_NAME_SAM_COMPATIBLE = 2        # GetUserNameExW: "DOMAIN\name"
_TOKEN_QUERY = 0x0008
_TOKEN_GROUPS = 2               # GetTokenInformation class
_SE_GROUP_ENABLED = 0x00000004
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_MORE_DATA = 234
_GENERIC_READ = 0x80000000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4   # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_MONITORINFOF_PRIMARY = 1

_sound_lock = threading.Lock()
_looping = False                # the alert sound is playing on a loop right now


def is_windows():
    return sys.platform == "win32"


def _frozen():
    return bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def current_identity():
    """Who is signed in on this PC, the PC's name, the enabled group SIDs and this process."""
    user = computer = sids = None
    if not _frozen():
        user = os.environ.get("STRIPES_HX_USER") or None
        computer = os.environ.get("STRIPES_HX_COMPUTER") or None
        raw = os.environ.get("STRIPES_HX_SIDS")
        if raw is not None:
            sids = frozenset(s.strip() for s in raw.split(",") if s.strip())
    if is_windows():
        user = user or _windows_user()
        computer = computer or _windows_computer()
        if sids is None:
            try:
                sids = _windows_group_sids()
            except (OSError, ValueError):
                # No groups means watching only, which is safe; self_check() reports the failure
                sids = frozenset()
    else:
        user = user or "LOCAL\\" + _login_name()
        computer = computer or socket.gethostname()
        if sids is None:
            sids = frozenset()
    return Identity(user=user, computer=computer, sids=sids, pid=os.getpid())


def _login_name():
    try:
        return getpass.getuser()
    except Exception:       # no USER variable and no password entry (some containers)
        return "unknown"


def _windows_user():
    import ctypes
    from ctypes import wintypes
    secur32 = ctypes.WinDLL("secur32", use_last_error=True)
    get_name = secur32.GetUserNameExW
    get_name.restype = ctypes.c_ubyte       # BOOLEAN
    get_name.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    size = wintypes.ULONG(256)
    for _ in range(2):
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if get_name(_NAME_SAM_COMPATIBLE, buf, ctypes.byref(size)):
            return buf.value
        if ctypes.get_last_error() != _ERROR_MORE_DATA:
            break
    # Shouldn't happen; the environment still names the person
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME") or _login_name()
    return f"{domain}\\{name}" if domain else name


def _windows_computer():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_name = kernel32.GetComputerNameW
    get_name.restype = wintypes.BOOL
    get_name.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD(256)
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if get_name(buf, ctypes.byref(size)):
        return buf.value
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def _sid_to_string(advapi32, kernel32, psid):
    """A SID pointer as text, "S-1-5-21-..."."""
    import ctypes
    from ctypes import wintypes
    convert = advapi32.ConvertSidToStringSidW
    convert.restype = wintypes.BOOL
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    text = wintypes.LPWSTR()
    if not convert(psid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _windows_group_sids():
    """The enabled group SIDs of this process's sign-in token."""
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_GROUPS(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    info = advapi32.GetTokenInformation
    info.restype = wintypes.BOOL
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        info(token, _TOKEN_GROUPS, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(needed.value)
        if not info(token, _TOKEN_GROUPS, buf, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        count = TOKEN_GROUPS.from_buffer(buf).GroupCount
        groups = (SID_AND_ATTRIBUTES * count).from_buffer(buf, TOKEN_GROUPS.Groups.offset)
        return frozenset(_sid_to_string(advapi32, kernel32, g.Sid)
                         for g in groups if g.Attributes & _SE_GROUP_ENABLED)
    finally:
        kernel32.CloseHandle(token)


def lookup_group_sid(name):
    """The SID of a group (or any account) name, as text; None if not found or off Windows."""
    if not is_windows() or not name:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lookup = advapi32.LookupAccountNameW
        lookup.restype = wintypes.BOOL
        lookup.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
        sid_size, domain_size, use = wintypes.DWORD(0), wintypes.DWORD(0), ctypes.c_int(0)
        lookup(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(use))
        if not sid_size.value:
            return None
        sid = ctypes.create_string_buffer(sid_size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value + 1)
        if not lookup(None, name, sid, ctypes.byref(sid_size), domain, ctypes.byref(domain_size),
                      ctypes.byref(use)):
            return None
        return _sid_to_string(advapi32, kernel32, sid)
    except OSError:
        return None


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def open_for_read(path) -> BinaryIO:
    """Open a file for reading, sharing read, write and delete. Windows' os.replace (MoveFileExW) can
    still refuse to replace the file while it's open, so writers retry (storage.md §8.2 item 3).
    Raises FileNotFoundError, PermissionError or OSError like open()."""
    if not is_windows():
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    path = os.fspath(path)
    handle = create(path, _GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        # With a Windows error code, OSError picks FileNotFoundError, PermissionError, ... itself
        raise OSError(None, ctypes.FormatError(code).strip(), path, code)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool
    # The screen less the taskbar and other docked bars (Windows' rcWork) as (x, y, width, height); None:
    # the whole screen, as off Windows
    work: tuple[int, int, int, int] | None = None

    def work_area(self) -> tuple[int, int, int, int]:
        """(x, y, width, height) a normal window should keep inside."""
        return self.work if self.work is not None else (self.x, self.y, self.width, self.height)


def monitors():
    """The connected screens, the primary first; [] off Windows (the UI then uses Tk's screen)."""
    if not is_windows():
        return []
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

    found = []

    def each(hmonitor, hdc, rect, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            r, wa = mi.rcMonitor, mi.rcWork
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                          bool(mi.dwFlags & _MONITORINFOF_PRIMARY),
                          (wa.left, wa.top, wa.right - wa.left, wa.bottom - wa.top)))
        return True

    callback = callback_type(each)      # kept referenced until EnumDisplayMonitors returns
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    found.sort(key=lambda m: not m[4])  # primary first, the rest in Windows' order
    return [Monitor(i, x, y, w, h, primary, work) for i, (x, y, w, h, primary, work) in enumerate(found)]


# --------------------------------------------------------------------------
# Alert sound
# --------------------------------------------------------------------------

def _alert_path():
    # Found the way stripes.resource_path finds bundled files (hx_win must not import stripes)
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "alert.wav")


def play_alert(repeat=True):
    """Play the alert sound, on a loop unless repeat is False. Nothing happens while the loop is
    already playing, or off Windows."""
    global _looping
    if not is_windows():
        return
    import winsound
    with _sound_lock:
        if _looping:
            return
        flags = winsound.SND_FILENAME | winsound.SND_ASYNC | (winsound.SND_LOOP if repeat else 0)
        try:
            winsound.PlaySound(_alert_path(), flags)
        except RuntimeError:
            # The file couldn't be played: a plain beep is better than silence for an alert
            try:
                winsound.MessageBeep()
            except RuntimeError:
                pass
            return
        _looping = bool(repeat)


def stop_alert():
    """Stop the alert sound; off Windows nothing happens."""
    global _looping
    if not is_windows():
        return
    import winsound
    with _sound_lock:
        _looping = False
        winsound.PlaySound(None, 0)


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def self_check():
    """Run every call once, as the exe's --self-check does; raises on any failure."""
    ident = current_identity()
    if not ident.user or not ident.computer:
        raise RuntimeError("no user or computer name")
    if is_windows():
        _windows_group_sids()       # current_identity() hides a failure here; the check doesn't

    # None is fine here: the group is named differently when Windows isn't in English
    lookup_group_sid("Administrators")
    folder = tempfile.mkdtemp(prefix="stripes-hx-")
    try:
        path = os.path.join(folder, "check.bin")
        with open(path, "wb") as f:
            f.write(b"stripes")
        with open_for_read(path) as f:
            if f.read() != b"stripes":
                raise RuntimeError("open_for_read read the wrong bytes")
    finally:
        try:
            os.remove(os.path.join(folder, "check.bin"))
            os.rmdir(folder)
        except OSError:
            pass
    if not isinstance(monitors(), list):
        raise RuntimeError("monitors() didn't return a list")
    if is_windows():
        import winsound
        winsound.PlaySound(None, 0)
