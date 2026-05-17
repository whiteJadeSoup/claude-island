"""Grab a screenshot of the live island capsule + expanded panel.

Walks all visible Qt windows owned by ``python.exe -m claude_island``,
captures each via Win32 PrintWindow (works even when off-screen), saves
PNGs to the user's image cache directory. Useful for verifying that a
fix landed in the actually-running UI rather than only in pytest land.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from pathlib import Path

import psutil
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wt.DWORD * 3),
    ]


def _find_island_windows() -> list[tuple[int, int, int]]:
    """Return ``(hwnd, w, h)`` for every visible window owned by a
    ``python.exe -m claude_island`` process."""
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    out: list[tuple[int, int, int]] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value)
            if proc.name() == "python.exe" and any(
                "claude_island" in a for a in proc.cmdline()
            ):
                rect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                out.append(
                    (hwnd, rect.right - rect.left, rect.bottom - rect.top)
                )
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def _capture(hwnd: int, w: int, h: int) -> Image.Image:
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    # PW_RENDERFULLCONTENT — works for layered / composited windows.
    user32.PrintWindow(hwnd, hdc_mem, 0x00000002)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    bits = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, bits, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBA", (w, h), bytes(bits), "raw", "BGRA", 0, 1)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    return img


def main() -> None:
    out_dir = (
        Path.home() / ".claude" / "image-cache"
        / "D--coding-projects-claude-island"
        / "44953ebc-dbda-4213-b33e-31caad74b008"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    wins = _find_island_windows()
    if not wins:
        raise SystemExit("no live claude_island windows found")
    for i, (hwnd, w, h) in enumerate(wins):
        img = _capture(hwnd, w, h)
        path = out_dir / f"island_live_{i}_hwnd{hwnd}_{w}x{h}.png"
        img.save(path)
        print(f"saved: {path}  ({w}x{h})")


if __name__ == "__main__":
    main()
