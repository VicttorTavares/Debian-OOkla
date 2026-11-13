"""Orquestrador: liga captura, RTSP, ONVIF, eventos e gravação local."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from . import winapi
from .activity import ActivityMonitor
from .audio import AudioMixer
from .config import APP_NAME, APP_VERSION, Config, user_runtime_dir
from .onvif.discovery import Discovery
from .onvif.events import EventBroker
from .onvif.server import VS, VSC, OnvifContext, OnvifServer, scopes
from .recorder import LocalRecorder
from .snapshot import SnapshotProvider
from .streamer import FFmpegPublisher, MediaMTX

log = logging.getLogger("screencam")


def setup_logging(verbose: bool = False) -> None:
    log_dir = user_runtime_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "screencam.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


class ScreenCamApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.paused = False
        self.listeners: list = []  # callbacks de mudança de status (bandeja)

        audio_sources = [] if (cfg.video.source == "test" or not winapi.IS_WINDOWS) else None
        self.mixer = AudioMixer(cfg.audio.system_audio, cfg.audio.microphone, cfg.audio.system_gain,
                                cfg.audio.mic_gain, sources=audio_sources) if cfg.audio.enabled else None
        self.mediamtx = MediaMTX(cfg)
        self.publisher = FFmpegPublisher(cfg, self.mixer)
        self.broker = EventBroker(VS, VSC)
        self.snapshot = SnapshotProvider(cfg, lambda: self.publisher.geometry)
        self.ctx = OnvifContext(cfg, lambda: self.publisher.geometry, self.snapshot.jpeg, self.broker)
        self.onvif = OnvifServer(self.ctx)
        self.discovery = Discovery(self.ctx.uuid, cfg.network.onvif_port, scopes(cfg),
                                   cfg.network.advertise_ip, cfg.network.bind_ip) \
            if cfg.network.discovery else None
        self.recorder = LocalRecorder(cfg, self.publisher.local_url) if cfg.recording.enabled else None
        self.activity = ActivityMonitor(cfg.activity.idle_timeout_seconds,
                                        cfg.activity.poll_interval_ms / 1000, self._on_activity)

    # --------------------------------------------------------------
    @property
    def on_activity_mode(self) -> bool:
        return self.cfg.activity.stream_mode == "on_activity"

    def _on_activity(self, active: bool):
        self.broker.set_motion(active)
        if self.on_activity_mode:
            if active:
                self.publisher.start()
            else:
                self.publisher.pause()
        if self.recorder:
            if active and self.on_activity_mode:
                # aguarda o FFmpeg publicar o stream antes de gravar
                threading.Timer(3.0, lambda: self.activity.active and self.recorder.set_active(True)).start()
            else:
                self.recorder.set_active(active)
        self._notify()

    def _notify(self):
        for cb in self.listeners:
            try:
                cb()
            except Exception:
                log.exception("Erro no listener de status")

    def status_text(self) -> str:
        if self.paused:
            return "Pausado"
        state = "Usuario ativo (gravando)" if self.activity.active else "Ocioso"
        stream = "transmitindo" if self.publisher.running else "stream parado"
        return f"{state} - {stream}"

    def set_paused(self, paused: bool):
        self.paused = paused
        self.activity.paused = paused
        if paused:
            self.publisher.pause()
        elif not self.on_activity_mode:
            self.publisher.start()
        self._notify()

    # --------------------------------------------------------------
    def start(self):
        g = self.publisher.geometry
        log.info("%s %s iniciando - captura %dx%d -> %dx%d @ %d fps, audio=%s",
                 APP_NAME, APP_VERSION, g.src_w, g.src_h, g.out_w, g.out_h, self.cfg.video.fps,
                 self.cfg.audio.codec if self.cfg.audio.enabled else "desativado")
        self.mediamtx.start()
        if self.mixer:
            self.mixer.start()
        if not self.on_activity_mode:
            self.publisher.start()
        self.onvif.start()
        if self.discovery:
            try:
                self.discovery.start()
            except OSError as exc:
                log.error("WS-Discovery indisponivel (porta 3702 em uso?): %s", exc)
                self.discovery = None
        if self.recorder:
            self.recorder.start()
        self.activity.start()

    def stop(self):
        log.info("Encerrando...")
        self.activity.stop()
        self.broker.set_motion(False)
        if self.recorder:
            self.recorder.stop()
        if self.discovery:
            self.discovery.stop()
        self.onvif.stop()
        self.publisher.shutdown()
        if self.mixer:
            self.mixer.stop()
        self.mediamtx.shutdown()
