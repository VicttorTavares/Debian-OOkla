"""Chamadas à API do Windows via ctypes (sem dependências extras).

Em outras plataformas as funções retornam valores neutros, o que permite
executar os testes automatizados no Linux.
"""
from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"


@dataclass
class Rect:
    left: int
    top: int
    width: int
    height: int


def enable_dpi_awareness() -> None:
    """Faz o processo enxergar a resolução física (sem escala de DPI)."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_tick() -> int:
    """Tick (ms) do último evento de mouse/teclado na sessão.

    Usa GetLastInputInfo: detecta qualquer movimento do mouse ou tecla
    pressionada SEM registrar o conteúdo digitado (não é keylogger).
    """
    if not IS_WINDOWS:
        return 0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return int(info.dwTime)


def tick_count() -> int:
    if not IS_WINDOWS:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF
    return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF


def virtual_screen() -> Rect:
    if not IS_WINDOWS:
        return Rect(0, 0, 1920, 1080)
    u = ctypes.windll.user32
    # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
    return Rect(u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))


def monitors() -> list[Rect]:
    """Lista os monitores; o primário vem primeiro."""
    if not IS_WINDOWS:
        return [Rect(0, 0, 1920, 1080)]
    from ctypes import wintypes

    result: list[tuple[bool, Rect]] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(hmon, _hdc, _rect, _lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        result.append((bool(mi.dwFlags & 1), Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(_cb), 0)
    result.sort(key=lambda item: (not item[0], item[1].left, item[1].top))
    return [r for _, r in result] or [virtual_screen()]


def capture_rect(monitor: str) -> Rect:
    if str(monitor).strip().lower() in ("all", "todos", "0", ""):
        return virtual_screen()
    mons = monitors()
    idx = int(monitor) - 1
    if 0 <= idx < len(mons):
        return mons[idx]
    return mons[0]


_mutex_handle = None


def single_instance(name: str) -> bool:
    """Retorna False se já existe outra instância rodando nesta máquina."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, f"Global\\{name}")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def no_window_flags() -> int:
    return 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
