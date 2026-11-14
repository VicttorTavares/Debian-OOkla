"""Orquestração.

Dois lados, para o modelo serviço + agente por sessão:

* ServerCore  -> roda no serviço SYSTEM: MediaMTX (RTSP), ONVIF, WS-Discovery,
                 gravação local, canal de controle e o gerenciador de sessões.
                 Não captura a tela (a sessão 0 não a enxerga).
* CaptureAgent -> roda na sessão de cada usuário: captura tela+áudio (FFmpeg) e
                 publica no MediaMTX local, reportando atividade ao serviço.

ConsoleApp junta os dois no mesmo processo (modo "console": dev, Linux e
instalações simples de usuário único).
"""
from __future__ import annotations

import logging
import logging.handlers
import secrets
import sys
import threading

from . import winapi
from .activity import ActivityMonitor
from .audio import AudioMixer
from .config import APP_NAME, APP_VERSION, Config, program_data_dir, user_runtime_dir
from .control import ActivityAggregator, ControlClient, ControlServer
from .onvif.discovery import Discovery
from .onvif.events import EventBroker
from .onvif.server import VS, VSC, OnvifContext, OnvifServer, scopes
from .recorder import LocalRecorder
from .snapshot import SnapshotProvider
from .streamer import FFmpegPublisher, MediaMTX, compute_geometry

log = logging.getLogger("screencam")


def setup_logging(verbose: bool = False, component: str = "screencam", to_programdata: bool = False) -> None:
    base = program_data_dir() if to_programdata else user_runtime_dir()
    log_dir = base / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = user_runtime_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / f"{component}.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ======================================================================
# Lado serviço
# ======================================================================

class ServerCore:
    """RTSP + ONVIF + descoberta + gravação + controle. Não captura a tela."""

    def __init__(self, cfg: Config, control_token: str | None = None):
        self.cfg = cfg
        self.control_token = control_token or secrets.token_urlsafe(16)
        self.paused = False
        self._geometry = compute_geometry(cfg)

        self.mediamtx = MediaMTX(cfg)
        self.broker = EventBroker(VS, VSC)
        self.aggregator = ActivityAggregator(self._on_motion)
        self.control = ControlServer(cfg.service.control_port, self.control_token,
                                     self.aggregator, status_fn=self.status)
        self.snapshot = SnapshotProvider(cfg, lambda: self._geometry)
        self.ctx = OnvifContext(cfg, lambda: self._geometry, self.snapshot.jpeg, self.broker)
        self.onvif = OnvifServer(self.ctx)
        self.discovery = Discovery(self.ctx.uuid, cfg.network.onvif_port, scopes(cfg),
                                   cfg.network.advertise_ip, cfg.network.bind_ip) \
            if cfg.network.discovery else None
        self.recorder = LocalRecorder(cfg, self._local_rtsp()) if cfg.recording.enabled else None
        self.autoregister = None
        if cfg.autoregister.enabled:
            from .autoregister import AutoRegister
            self.autoregister = AutoRegister(cfg, cfg.network.onvif_port, cfg.network.rtsp_port)
        self.active = False

    def _local_rtsp(self) -> str:
        from .streamer import STREAM_PATH
        return f"rtsp://127.0.0.1:{self.cfg.network.rtsp_port}/{STREAM_PATH}"

    @property
    def on_activity_mode(self) -> bool:
        return self.cfg.activity.stream_mode == "on_activity"

    def _on_motion(self, active: bool) -> None:
        """Chamado pelo agregador quando qualquer sessão fica ativa/ociosa."""
        self.active = active
        if self.paused:
            active = False
        self.broker.set_motion(active)
        if self.recorder:
            if active and self.on_activity_mode:
                threading.Timer(3.0, lambda: self.active and self.recorder.set_active(True)).start()
            else:
                self.recorder.set_active(active)

    def resolve_codec(self) -> None:
        """Define o codec efetivo (GPU/CPU, H.265 ou H.264) para o ONVIF anunciar.

        É a mesma máquina do agente, então a capacidade do encoder é válida para
        ambos; o valor resolvido segue para o agente no payload.
        """
        try:
            name = FFmpegPublisher(self.cfg).resolve_encoder()
            log.info("Codec anunciado ao NVR: %s (%s)", self.cfg.video.codec.upper(), name)
        except Exception as exc:
            log.error("Falha ao selecionar encoder; mantendo %s: %s", self.cfg.video.codec, exc)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._on_motion(self.active)

    def status(self) -> dict:
        return {
            "app": f"{APP_NAME} {APP_VERSION}",
            "paused": self.paused,
            "motion": self.broker.motion,
            "active_sessions": self.aggregator.active_sessions(),
            "codec": self.cfg.video.codec,
            "onvif_port": self.cfg.network.onvif_port,
            "rtsp_port": self.cfg.network.rtsp_port,
            "nvr_ip": self.cfg.network.nvr_ip or "qualquer (rede local)",
            "recording": bool(self.recorder),
            "autoregister": (f"{self.cfg.autoregister.server_ip}:{self.cfg.autoregister.server_port}"
                             if self.autoregister else "desativado"),
        }

    def start(self) -> None:
        self.resolve_codec()
        if self.discovery:
            self.discovery.scopes = " ".join(scopes(self.cfg))
        log.info("%s %s (serviço) - ONVIF %s / RTSP %s, NVR=%s, video=%s/%s, audio=%s",
                 APP_NAME, APP_VERSION, self.cfg.network.onvif_port, self.cfg.network.rtsp_port,
                 self.cfg.network.nvr_ip or "qualquer (rede local)",
                 self.cfg.video.codec.upper(), self.cfg.video.encoder,
                 self.cfg.audio.codec if self.cfg.audio.enabled else "desativado")
        self._diagnose_network()
        self.mediamtx.start()
        self.control.start()
        self.onvif.start()
        if self.discovery:
            try:
                self.discovery.start()
            except OSError as exc:
                log.error("WS-Discovery indisponivel (porta 3702 em uso?): %s", exc)
                self.discovery = None
        if self.recorder:
            self.recorder.start()
        if self.autoregister:
            self.autoregister.start()

    def _diagnose_network(self) -> None:
        """Loga o IP local e avisa se o IP do NVR parece errado (sub-rede diferente)."""
        from .config import same_subnet24
        from .onvif.discovery import local_ip_for
        nvr = self.cfg.network.nvr_ip
        local = local_ip_for(nvr or "8.8.8.8")
        log.info("Rede: IP local deste PC = %s; NVR autorizado = %s",
                 local, nvr or "qualquer (sub-rede local)")
        if nvr and not same_subnet24(local, nvr):
            log.warning("ATENCAO: o IP do NVR (%s) NAO esta na mesma sub-rede /24 do PC (%s). "
                        "Se foi erro de digitacao, o firewall e a leitura RTSP bloquearao o NVR. "
                        "Corrija 'nvr_ip' em [network] (ou deixe em branco) e reinicie o servico.",
                        nvr, local)

    def stop(self) -> None:
        log.info("Encerrando serviço...")
        self.broker.set_motion(False)
        if self.autoregister:
            self.autoregister.stop()
        if self.recorder:
            self.recorder.stop()
        if self.discovery:
            self.discovery.stop()
        self.onvif.stop()
        self.control.stop()
        self.mediamtx.shutdown()


# ======================================================================
# Lado captura
# ======================================================================

class CaptureAgent:
    """Captura tela+áudio e publica no MediaMTX. Reporta atividade via ``report_fn``."""

    def __init__(self, cfg: Config, report_fn=None, session_label: str = "local"):
        self.cfg = cfg
        self.report_fn = report_fn or (lambda active: None)
        self.session_label = session_label

        audio_sources = [] if (cfg.video.source == "test" or not winapi.IS_WINDOWS) else None
        self.mixer = AudioMixer(cfg.audio.system_audio, cfg.audio.microphone, cfg.audio.system_gain,
                                cfg.audio.mic_gain, sources=audio_sources) if cfg.audio.enabled else None
        self.publisher = FFmpegPublisher(cfg, self.mixer)
        # codec/encoder já vêm resolvidos pelo serviço; não reprobar
        if cfg.video.encoder != "auto":
            self.publisher._encoder_resolved = True
        self.activity = ActivityMonitor(cfg.activity.idle_timeout_seconds,
                                        cfg.activity.poll_interval_ms / 1000, self._on_activity)

    @property
    def on_activity_mode(self) -> bool:
        return self.cfg.activity.stream_mode == "on_activity"

    def _on_activity(self, active: bool) -> None:
        if self.on_activity_mode:
            if active:
                self.publisher.start()
            else:
                self.publisher.pause()
        try:
            self.report_fn(active)
        except Exception:
            log.exception("Falha ao reportar atividade")

    def start(self) -> None:
        g = self.publisher.geometry
        log.info("Agente de captura (%s): %dx%d -> %dx%d @ %d fps, video=%s/%s, audio=%s",
                 self.session_label, g.src_w, g.src_h, g.out_w, g.out_h, self.cfg.video.fps,
                 self.cfg.video.codec.upper(), self.cfg.video.encoder,
                 self.cfg.audio.codec if self.cfg.audio.enabled else "desativado")
        if self.mixer:
            self.mixer.start()
        if not self.on_activity_mode:
            self.publisher.start()
        self.activity.start()

    def stop(self) -> None:
        self.activity.stop()
        self.publisher.shutdown()
        if self.mixer:
            self.mixer.stop()
        try:
            self.report_fn(False)
        except Exception:
            pass


# ======================================================================
# Modo console (serviço + captura no mesmo processo)
# ======================================================================

class ConsoleApp:
    """Processo único: ServerCore + CaptureAgent local. Usado em dev/Linux e
    em instalações simples de um só usuário (sem isolamento de sessão)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.core = ServerCore(cfg)
        self.agent: CaptureAgent | None = None
        self.listeners: list = []

    def _report(self, active: bool) -> None:
        self.core.aggregator.report(self.core.cfg.service.service_name + ":local", active)
        self._notify()

    def _notify(self):
        for cb in self.listeners:
            try:
                cb()
            except Exception:
                log.exception("Erro no listener de status")

    # compatibilidade com a bandeja
    @property
    def paused(self) -> bool:
        return self.core.paused

    def set_paused(self, paused: bool) -> None:
        self.core.set_paused(paused)
        if self.agent:
            self.agent.activity.paused = paused
            if paused:
                self.agent.publisher.pause()
            elif not self.agent.on_activity_mode:
                self.agent.publisher.start()
        self._notify()

    def status_text(self) -> str:
        if self.core.paused:
            return "Pausado"
        state = "Usuario ativo (gravando)" if self.core.active else "Ocioso"
        stream = "transmitindo" if (self.agent and self.agent.publisher.running) else "stream parado"
        return f"{state} - {stream}"

    @property
    def activity(self):  # usado pela bandeja
        return self.agent.activity if self.agent else None

    def start(self) -> None:
        self.core.start()
        self.agent = CaptureAgent(self.cfg, report_fn=self._report, session_label="console")
        self.agent.start()

    def stop(self) -> None:
        if self.agent:
            self.agent.stop()
        self.core.stop()


# compatibilidade retroativa
ScreenCamApp = ConsoleApp
