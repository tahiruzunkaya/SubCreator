from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import mss
import numpy as np

from .winutil import Rect


class ScreenGrabber:
    """Ekranın bir bölgesini BGR numpy dizisi olarak yakalar (ekranda görüneni).

    mss nesneleri iş parçacığına bağlıdır; bu sınıfı kullanılacağı thread içinde oluştur.
    """

    def __init__(self) -> None:
        # mss 10+ `mss.MSS` kullanıyor; eski sürümlerde yalnızca `mss.mss` var.
        self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()

    def grab(self, rect: Rect) -> np.ndarray:
        x, y, w, h = rect
        shot = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def close(self) -> None:
        self._sct.close()


# --- Pencere içeriği yakalama (PrintWindow) -------------------------------------------
_user32 = ctypes.WinDLL("user32")
_gdi32 = ctypes.WinDLL("gdi32")

_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi32.GetDIBits.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
_gdi32.GetDIBits.restype = ctypes.c_int

PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
# SUBCREATOR_NO_PRINTWINDOW=1: PrintWindow'u desteklemeyen uygulamaları taklit et (yalnızca test).
_DISABLED = bool(os.environ.get("SUBCREATOR_NO_PRINTWINDOW"))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class WindowCapturer:
    """Bir pencerenin istemci alanını, önünde başka pencere olsa bile yakalar (PrintWindow).

    Overlay ve diğer pencereler görüntüye girmez. Bazı uygulamalar siyah döndürebilir;
    o durumda `None` döner ve çağıran ekran yakalamaya düşer.
    """

    def __init__(self) -> None:
        self._screen_dc = _user32.GetDC(None)
        self._mem_dc = _gdi32.CreateCompatibleDC(self._screen_dc)
        self._bitmap = None
        self._size: tuple[int, int] | None = None

    def capture(self, hwnd, width: int, height: int) -> np.ndarray | None:
        if width <= 0 or height <= 0 or _DISABLED:
            return None
        if self._size != (width, height):
            if self._bitmap:
                _gdi32.DeleteObject(self._bitmap)
            self._bitmap = _gdi32.CreateCompatibleBitmap(self._screen_dc, width, height)
            self._size = (width, height)

        old = _gdi32.SelectObject(self._mem_dc, self._bitmap)
        ok = _user32.PrintWindow(hwnd, self._mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        _gdi32.SelectObject(self._mem_dc, old)
        if not ok:
            return None

        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = np.empty((height, width, 4), np.uint8)
        if not _gdi32.GetDIBits(self._mem_dc, self._bitmap, 0, height, buf.ctypes.data, ctypes.byref(header), 0):
            return None
        img = np.ascontiguousarray(buf[:, :, :3])
        if int(img.max()) < 8:  # tamamen siyah: uygulama PrintWindow'u desteklemiyor
            return None
        return img

    def close(self) -> None:
        if self._bitmap:
            _gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._mem_dc:
            _gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._screen_dc:
            _user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None
