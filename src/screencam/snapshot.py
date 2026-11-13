"""Captura de imagem estática (GetSnapshotUri do ONVIF)."""
from __future__ import annotations

import io
import logging
import threading
import time

from PIL import Image, ImageDraw

from . import winapi

log = logging.getLogger(__name__)


class SnapshotProvider:
    def __init__(self, cfg, geometry_fn):
        self.cfg = cfg
        self.geometry_fn = geometry_fn
        self._cache: tuple[float, bytes] | None = None
        self._lock = threading.Lock()

    def jpeg(self) -> bytes:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < 1.0:
                return self._cache[1]
            geo = self.geometry_fn()
            img = None
            if self.cfg.video.source != "test" and winapi.IS_WINDOWS:
                try:
                    from PIL import ImageGrab
                    img = ImageGrab.grab(bbox=(geo.src_x, geo.src_y, geo.src_x + geo.src_w,
                                               geo.src_y + geo.src_h), all_screens=True)
                except Exception as exc:  # tela bloqueada, sessão desconectada...
                    log.debug("Snapshot falhou: %s", exc)
            if img is None:
                img = Image.new("RGB", (geo.src_w, geo.src_h), (32, 32, 48))
                ImageDraw.Draw(img).text((20, 20), self.cfg.device_name, fill=(255, 255, 255))
            if img.size != (geo.out_w, geo.out_h):
                img = img.resize((geo.out_w, geo.out_h), Image.BILINEAR)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=80)
            data = buf.getvalue()
            self._cache = (time.monotonic(), data)
            return data
