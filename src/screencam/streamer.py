"""Gerencia os processos externos: MediaMTX (servidor RTSP) e FFmpeg (captura + encode)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import winapi
from .config import Config, user_runtime_dir
from .encoders import encoder_args, resolve_encoder

log = logging.getLogger(__name__)

STREAM_PATH = "screen"


def find_binary(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    candidates = []
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        candidates += [base / exe, base / "bin" / exe]
    root = Path(__file__).resolve().parents[2]
    candidates += [root / "vendor" / exe, root / "bin" / exe]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{exe} nao encontrado (procurado em {', '.join(map(str, candidates))} e no PATH)")


@dataclass
class VideoGeometry:
    src_x: int
    src_y: int
    src_w: int
    src_h: int
    out_w: int
    out_h: int


def compute_geometry(cfg: Config) -> VideoGeometry:
    if cfg.video.source == "test":
        r = winapi.Rect(0, 0, 1920, 1080)
    else:
        try:
            r = winapi.capture_rect(cfg.video.monitor)
        except Exception:  # sem sessao interativa (ex.: servico na sessao 0)
            r = winapi.Rect(0, 0, 0, 0)
    # O servico roda na sessao 0, que nao tem monitor: o Windows devolve 0x0.
    # Usa um tamanho padrao (o agente de captura define o real na sessao do usuario).
    if r.width < 16 or r.height < 16:
        r = winapi.Rect(0, 0, 1920, 1080)
    w, h = r.width - r.width % 2, r.height - r.height % 2
    f = min(1.0, cfg.video.max_width / w, cfg.video.max_height / h)
    ow = int(w * f) // 2 * 2
    oh = int(h * f) // 2 * 2
    return VideoGeometry(r.left, r.top, w, h, ow, oh)


def _sanitize_overlay(text: str) -> str:
    # remove caracteres com significado especial no filtro drawtext
    return re.sub(r"[\\':%,;\[\]=]", "", text)


def build_ffmpeg_cmd(cfg: Config, geo: VideoGeometry, rtsp_url: str, ffmpeg: str = "ffmpeg") -> list[str]:
    v, a = cfg.video, cfg.audio
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats"]

    # ---- entrada de vídeo
    if v.source == "test":
        cmd += ["-re", "-f", "lavfi", "-i",
                f"testsrc2=size={geo.src_w}x{geo.src_h}:rate={v.fps}"]
    else:
        # Sem -thread_queue_size: nas versoes novas do FFmpeg ele passou a ser opcao de SAIDA e
        # fazia o gdigrab falhar ("cannot be applied to input url desktop").
        cmd += ["-f", "gdigrab",
                "-framerate", str(v.fps),
                "-draw_mouse", "1" if v.draw_mouse else "0",
                "-offset_x", str(geo.src_x), "-offset_y", str(geo.src_y),
                "-video_size", f"{geo.src_w}x{geo.src_h}",
                "-i", "desktop"]

    # ---- entrada de áudio (PCM vindo do mixer via stdin)
    if a.enabled:
        cmd += ["-f", "s16le", "-ar", "48000", "-ch_layout", "stereo", "-i", "pipe:0"]

    # ---- filtros de vídeo
    filters = []
    if (geo.out_w, geo.out_h) != (geo.src_w, geo.src_h):
        filters.append(f"scale={geo.out_w}:{geo.out_h}:flags=bicubic")
    if v.overlay:
        label = _sanitize_overlay(cfg.overlay_text)
        size = max(14, geo.out_h // 40)
        filters.append(
            f"drawtext=font='{v.font}':text='{label}  %{{localtime\\:%d/%m/%Y %H\\\\\\:%M\\\\\\:%S}}'"
            f":x=10:y=10:fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6"
        )
    filters.append("format=yuv420p")
    cmd += ["-map", "0:v"]
    if a.enabled:
        cmd += ["-map", "1:a"]
    cmd += ["-vf", ",".join(filters)]

    # ---- encoder de vídeo (H.265/H.264, GOP de 2 s para o NVR sincronizar rápido)
    cmd += encoder_args(cfg)

    # ---- encoder de áudio
    if a.enabled:
        if a.codec == "g711":
            cmd += ["-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{a.bitrate_kbps}k", "-ar", "48000", "-ac", "2"]

    cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", rtsp_url]
    return cmd


def build_mediamtx_config(cfg: Config) -> str:
    """Gera o mediamtx.yml (compatível com MediaMTX v1.21.x)."""
    n = cfg.network
    users = [
        "  - user: any",
        "    pass:",
        "    ips: ['127.0.0.1', '::1']",
        "    permissions:",
        "      - action: publish",
        "        path:",
        "      - action: read",
        "        path:",
    ]
    # quando o IP do NVR é conhecido, só ele pode ler o stream (alem do localhost)
    read_ips = f"['{cfg.network.nvr_ip}']" if cfg.network.nvr_ip else "[]"
    if cfg.auth.enabled:
        users += [
            f"  - user: {_yaml_str(cfg.auth.username)}",
            f"    pass: {_yaml_str(cfg.auth.password)}",
            f"    ips: {read_ips}",
            "    permissions:",
            "      - action: read",
            "        path:",
        ]
    else:
        users += [
            "  - user: any",
            "    pass:",
            f"    ips: {read_ips}",
            "    permissions:",
            "      - action: read",
            "        path:",
        ]
    lines = [
        "logLevel: warn",
        "logDestinations: [stdout]",
        "readTimeout: 10s",
        "writeTimeout: 10s",
        "authMethod: internal",
        "authInternalUsers:",
        *users,
        "api: false",
        "metrics: false",
        "pprof: false",
        "playback: false",
        "rtsp: true",
        "rtspTransports: [tcp, udp]",
        f"rtspAddress: {_listen(n.bind_ip, n.rtsp_port)}",
        f"rtpAddress: {_listen(n.bind_ip, n.rtsp_port + 2446)}",
        f"rtcpAddress: {_listen(n.bind_ip, n.rtsp_port + 2447)}",
        "rtspAuthMethods: [basic, digest]",
        "rtmp: false",
        "hls: false",
        "webrtc: false",
        "srt: false",
        "moq: false",
        "paths:",
        f"  {STREAM_PATH}:",
        "    source: publisher",
    ]
    return "\n".join(lines) + "\n"


def _listen(ip: str, port: int) -> str:
    return f"{'' if ip in ('0.0.0.0', '') else ip}:{port}"


def _yaml_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class _ManagedProcess:
    """Mantém um processo externo vivo, reiniciando com backoff."""

    def __init__(self, name: str):
        self.name = name
        self.proc: subprocess.Popen | None = None
        self._want = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    # sobrescrever
    def _spawn(self) -> subprocess.Popen:  # pragma: no cover
        raise NotImplementedError

    def _on_started(self, proc: subprocess.Popen) -> None:
        pass

    def _on_stopped(self) -> None:
        pass

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self._want = True
        if not self._thread:
            self._thread = threading.Thread(target=self._supervise, name=f"sup-{self.name}", daemon=True)
            self._thread.start()

    def pause(self):
        """Para o processo mas mantém o supervisor (usado no modo on_activity)."""
        self._want = False
        self._kill()

    def shutdown(self):
        self._want = False
        self._stop.set()
        self._kill()

    def _kill(self):
        with self._lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                if p.stdin:
                    try:
                        p.stdin.close()
                    except OSError:
                        pass
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
        if p:
            self._on_stopped()

    def _pipe_log(self, stream, level=logging.INFO):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.last_error = line
                log.log(level, "[%s] %s", self.name, line)

    def _supervise(self):
        backoff = 1.0
        while not self._stop.is_set():
            if not self._want:
                self._stop.wait(0.2)
                continue
            try:
                proc = self._spawn()
            except Exception as exc:
                self.last_error = str(exc)
                log.error("Nao foi possivel iniciar %s: %s", self.name, exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            with self._lock:
                self.proc = proc
            threading.Thread(target=self._pipe_log, args=(proc.stderr, logging.WARNING),
                             daemon=True).start()
            if proc.stdout:
                threading.Thread(target=self._pipe_log, args=(proc.stdout,), daemon=True).start()
            self._on_started(proc)
            started = time.monotonic()
            code = proc.wait()
            self._on_stopped()
            with self._lock:
                if self.proc is proc:
                    self.proc = None
            if self._stop.is_set() or not self._want:
                continue
            if time.monotonic() - started > 30:
                backoff = 1.0
            log.warning("%s terminou (codigo %s); reiniciando em %.0fs", self.name, code, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)


class MediaMTX(_ManagedProcess):
    def __init__(self, cfg: Config):
        super().__init__("mediamtx")
        self.cfg = cfg
        self.conf_path = user_runtime_dir() / "mediamtx.yml"

    def _spawn(self):
        self.conf_path.write_text(build_mediamtx_config(self.cfg), encoding="utf-8")
        exe = find_binary("mediamtx")
        log.info("Iniciando MediaMTX (RTSP na porta %s)", self.cfg.network.rtsp_port)
        return subprocess.Popen([exe, str(self.conf_path)], cwd=str(user_runtime_dir()),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=winapi.no_window_flags())


class FFmpegPublisher(_ManagedProcess):
    def __init__(self, cfg: Config, mixer=None):
        super().__init__("ffmpeg")
        self.cfg = cfg
        self.mixer = mixer
        self.geometry = compute_geometry(cfg)
        self._encoder_resolved = False

    def resolve_encoder(self) -> str:
        """Escolhe o encoder (GPU/CPU, H.265/H.264) uma única vez, antes de publicar."""
        name = resolve_encoder(self.cfg, find_binary("ffmpeg"))
        self._encoder_resolved = True
        return name

    @property
    def local_url(self) -> str:
        return f"rtsp://127.0.0.1:{self.cfg.network.rtsp_port}/{STREAM_PATH}"

    def _spawn(self):
        err = self.last_error.lower()
        if self.cfg.video.overlay and any(k in err for k in ("drawtext", "fontconfig", "font ")):
            log.warning("Sobreposicao de texto desativada (fonte indisponivel no FFmpeg): %s", self.last_error)
            self.cfg.video.overlay = False
        self.last_error = ""
        if not self._encoder_resolved:
            self.resolve_encoder()
        self.geometry = compute_geometry(self.cfg)
        cmd = build_ffmpeg_cmd(self.cfg, self.geometry, self.local_url, find_binary("ffmpeg"))
        log.info("Iniciando FFmpeg: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdin=subprocess.PIPE if self.cfg.audio.enabled else subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=winapi.no_window_flags())

    def _on_started(self, proc):
        if self.mixer and proc.stdin:
            stdin = proc.stdin

            def sink(data: bytes, _s=stdin):
                _s.write(data)
                _s.flush()
            self.mixer.set_sink(sink)

    def _on_stopped(self):
        if self.mixer:
            self.mixer.set_sink(None)


def env_info() -> str:  # pragma: no cover - diagnóstico
    return f"python={sys.version.split()[0]} platform={sys.platform} cwd={os.getcwd()}"
