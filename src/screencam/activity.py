"""Detecção de atividade do usuário (movimento do mouse / tecla pressionada)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import winapi

log = logging.getLogger(__name__)


class ActivityMonitor:
    """Gera eventos ``on_change(True)`` no primeiro movimento/tecla e
    ``on_change(False)`` após ``idle_timeout`` segundos sem entrada."""

    def __init__(self, idle_timeout: float, poll_interval: float,
                 on_change: Callable[[bool], None],
                 input_source: Callable[[], int] = winapi.last_input_tick):
        self.idle_timeout = idle_timeout
        self.poll_interval = poll_interval
        self.on_change = on_change
        self._input_source = input_source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active = False
        self.last_activity = 0.0
        self.paused = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="activity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def notify_input(self) -> None:
        """Permite injetar atividade manualmente (testes / menu)."""
        self.last_activity = time.monotonic()
        self._set(True)

    def _set(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        log.info("Atividade do usuario: %s", "INICIO" if active else "FIM (ocioso)")
        try:
            self.on_change(active)
        except Exception:  # pragma: no cover
            log.exception("Erro no callback de atividade")

    def _run(self) -> None:
        last_tick = self._input_source()
        while not self._stop.wait(self.poll_interval):
            if self.paused:
                self._set(False)
                continue
            tick = self._input_source()
            now = time.monotonic()
            if tick != last_tick:
                last_tick = tick
                self.last_activity = now
                self._set(True)
            elif self.active and now - self.last_activity >= self.idle_timeout:
                self._set(False)
