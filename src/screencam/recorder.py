"""Gravação local opcional (backup do NVR): grava enquanto houver atividade."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import winapi
from .config import Config
from .streamer import find_binary

log = logging.getLogger(__name__)


class LocalRecorder:
    def __init__(self, cfg: Config, source_url: str):
        self.cfg = cfg
        self.source_url = source_url
        self.dir: Path = cfg.recording_dir
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._housekeeping, name="rec-retention", daemon=True).start()

    def set_active(self, active: bool):
        if active:
            self._begin()
        else:
            self._end()

    def _begin(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            day_dir = self.dir / stamp
            day_dir.mkdir(parents=True, exist_ok=True)
            pattern = str(day_dir / "%Y-%m-%d_%H-%M-%S.mkv")
            cmd = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
                   "-rtsp_transport", "tcp", "-i", self.source_url, "-map", "0", "-c", "copy",
                   "-f", "segment", "-segment_time", str(self.cfg.recording.segment_minutes * 60),
                   "-reset_timestamps", "1", "-strftime", "1", pattern]
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL,
                                             creationflags=winapi.no_window_flags())
                log.info("Gravacao local iniciada em %s", day_dir)
            except Exception as exc:
                log.error("Falha ao iniciar gravacao local: %s", exc)

    def _end(self):
        with self._lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")  # encerra o arquivo corretamente
                p.stdin.flush()
                p.wait(timeout=10)
            except Exception:
                p.kill()
            log.info("Gravacao local finalizada")

    def stop(self):
        self._stop.set()
        self._end()

    # ----------------------------------------------------- retenção
    def _housekeeping(self):
        while not self._stop.wait(5):
            try:
                self.apply_retention()
            except Exception:
                log.exception("Erro na limpeza de gravacoes")
            self._stop.wait(3600)

    def apply_retention(self):
        files = sorted((p for p in self.dir.rglob("*.mkv") if p.is_file()), key=lambda p: p.stat().st_mtime)
        limit_age = time.time() - self.cfg.recording.retention_days * 86400
        max_bytes = self.cfg.recording.max_size_gb * 1024 ** 3
        total = sum(p.stat().st_size for p in files)
        recent = time.time() - 120  # nunca apaga o arquivo que está sendo gravado
        for p in files:
            if p.stat().st_mtime > recent:
                continue
            if p.stat().st_mtime < limit_age or total > max_bytes:
                total -= p.stat().st_size
                try:
                    p.unlink()
                    log.info("Gravacao removida pela politica de retencao: %s", p.name)
                except OSError:
                    pass
        for d in self.dir.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
