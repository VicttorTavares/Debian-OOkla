"""Captura de áudio: som do sistema (WASAPI loopback) + microfone, mixados.

O resultado é PCM s16le, 48 kHz, estéreo, entregue em blocos de 20 ms com
ritmo de relógio real (assim o FFmpeg recebe um fluxo contínuo mesmo quando
nenhum som está tocando).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK = 960  # 20 ms
MAX_BUFFER = SAMPLE_RATE // 2  # 500 ms; acima disso descarta (evita atraso crescente)


class _Source:
    def __init__(self, name: str, gain: float, opener: Callable):
        self.name = name
        self.gain = gain
        self._opener = opener
        self._buf: deque[np.ndarray] = deque()
        self._frames = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"audio-{name}", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _push(self, data: np.ndarray):
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > 2:
            data = data[:, :2]
        with self._lock:
            self._buf.append(data.astype(np.float32) * self.gain)
            self._frames += len(data)
            while self._frames > MAX_BUFFER and self._buf:
                self._frames -= len(self._buf.popleft())

    def take(self, n: int) -> np.ndarray:
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        pos = 0
        with self._lock:
            while pos < n and self._buf:
                chunk = self._buf[0]
                k = min(n - pos, len(chunk))
                out[pos:pos + k] = chunk[:k]
                pos += k
                if k == len(chunk):
                    self._buf.popleft()
                else:
                    self._buf[0] = chunk[k:]
                self._frames -= k
        return out

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._opener() as rec:
                    log.info("Captura de audio iniciada: %s", self.name)
                    backoff = 1.0
                    while not self._stop.is_set():
                        self._push(rec.record(numframes=BLOCK))
            except Exception as exc:
                log.warning("Falha na captura de audio '%s': %s (nova tentativa em %.0fs)",
                            self.name, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)


def _com_init():
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass


def _system_opener():
    import soundcard as sc
    _com_init()
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    return mic.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK)


def _mic_opener():
    import soundcard as sc
    _com_init()
    return sc.default_microphone().recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK)


class AudioMixer:
    """Mixa as fontes e escreve o PCM resultante em ``sink(bytes)``."""

    def __init__(self, system_audio: bool, microphone: bool,
                 system_gain: float = 1.0, mic_gain: float = 1.0,
                 sources: list[_Source] | None = None):
        if sources is None:
            sources = []
            if system_audio:
                sources.append(_Source("sistema", system_gain, _system_opener))
            if microphone:
                sources.append(_Source("microfone", mic_gain, _mic_opener))
        self.sources = sources
        self._sink: Callable[[bytes], None] | None = None
        self._sink_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio-mixer", daemon=True)

    def start(self):
        for s in self.sources:
            s.start()
        self._thread.start()

    def stop(self):
        self._stop.set()
        for s in self.sources:
            s.stop()

    def set_sink(self, sink: Callable[[bytes], None] | None):
        with self._sink_lock:
            self._sink = sink

    def _run(self):
        period = BLOCK / SAMPLE_RATE
        next_t = time.monotonic()
        while not self._stop.is_set():
            mix = np.zeros((BLOCK, CHANNELS), dtype=np.float32)
            for s in self.sources:
                mix += s.take(BLOCK)
            pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            with self._sink_lock:
                sink = self._sink
            if sink is not None:
                try:
                    sink(pcm)
                except Exception:
                    with self._sink_lock:
                        if self._sink is sink:
                            self._sink = None
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:  # atrasou demais (sistema suspenso etc.)
                next_t = time.monotonic()
