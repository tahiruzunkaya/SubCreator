"""Win32 yardımcıları (ctypes). Koordinatların hepsi fiziksel piksel."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

dwmapi = ctypes.WinDLL("dwmapi")
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOT = 2
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SW_RESTORE = 9
DWMWA_CLOAKED = 14

Rect = tuple[int, int, int, int]  # x, y, w, h

_exe_cache: dict[int, str] = {}


def process_exe_name(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    _exe_cache[pid] = name
    return name


def window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_window(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def client_rect_screen(hwnd) -> Rect | None:
    """Pencerenin istemci alanı, ekran koordinatlarında (x, y, w, h)."""
    rc = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top


def _process_matches(exe: str, process_part: str) -> bool:
    """`chiaki.exe` gibi tam exe adı verildiyse birebir, yoksa (ör. `python`) içerme kontrolü."""
    exe = exe.lower()
    if process_part.endswith(".exe"):
        return exe == process_part
    return process_part in exe


def _is_cloaked(hwnd) -> bool:
    """Arka planda gizli tutulan (UWP vb.) pencereler listede görünmesin."""
    cloaked = wintypes.DWORD(0)
    res = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return res == 0 and cloaked.value != 0


def list_windows() -> list[tuple[int, str, str]]:
    """Seçilebilecek pencereler: (hwnd, exe adı, başlık). Bizim pencerelerimiz hariç."""
    own_pid = os.getpid()
    out: list[tuple[int, str, str]] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        title = window_title(hwnd)
        pid = window_pid(hwnd)
        if not title or pid == own_pid:
            return True
        if not user32.IsIconic(hwnd):
            rect = client_rect_screen(hwnd)
            if rect is None or rect[2] < 200 or rect[3] < 100:
                return True
        exe = process_exe_name(pid)
        if exe:
            out.append((hwnd, exe, title))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return out


def bring_to_front(hwnd) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def find_target_window(process_part: str, title_part: str) -> int | None:
    """Süreç adı ve başlık eşleşen, görünür ve en büyük pencereyi bulur."""
    process_part = process_part.lower()
    title_part = title_part.lower()
    own_pid = os.getpid()
    best: tuple[int, int] | None = None  # (area, hwnd)

    def callback(hwnd, _lparam):
        nonlocal best
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        title = window_title(hwnd)
        if not title or (title_part and title_part not in title.lower()):
            return True
        pid = window_pid(hwnd)
        if pid == own_pid:
            return True
        if process_part and not _process_matches(process_exe_name(pid), process_part):
            return True
        rect = client_rect_screen(hwnd)
        if rect is None:
            return True
        area = rect[2] * rect[3]
        if area > 0 and (best is None or area > best[0]):
            best = (area, hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return best[1] if best else None


def foreground_state(hwnd) -> str:
    """Öndeki pencere: "target" (hedef uygulama), "self" (SubCreator) veya "other"."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return "other"
    if window_pid(fg) == os.getpid():
        return "self"
    if user32.GetAncestor(fg, GA_ROOT) == user32.GetAncestor(hwnd, GA_ROOT):
        return "target"
    return "other"


def exclude_from_capture(hwnd) -> bool:
    """Pencereyi ekran yakalamadan hariç tutar (Windows 10 2004+)."""
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def set_capture_excluded(hwnd, excluded: bool) -> bool:
    """True: OBS/ekran görüntüsü pencereyi görmez. False: normal pencere gibi yakalanır."""
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if excluded else WDA_NONE))


def place_window(hwnd, x: int, y: int, w: int, h: int) -> None:
    """Pencereyi fiziksel koordinatlara taşır ve en üstte tutar, odak çalmadan."""
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)


def region_to_rect(region: list[float], client: Rect) -> Rect:
    cx, cy, cw, ch = client
    fx, fy, fw, fh = region
    x = cx + round(fx * cw)
    y = cy + round(fy * ch)
    w = max(8, round(fw * cw))
    h = max(8, round(fh * ch))
    return x, y, w, h
