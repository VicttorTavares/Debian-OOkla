"""Carregamento da configuração (config.ini)."""
from __future__ import annotations

import configparser
import getpass
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "ScreenCamONVIF"
APP_VERSION = "1.0.0"


def program_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP_NAME


def user_runtime_dir() -> Path:
    """Diretório gravável pelo usuário (logs, mediamtx.yml gerado)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_path() -> Path:
    return program_data_dir() / "config.ini"


def _placeholders() -> dict[str, str]:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover
        user = "usuario"
    return {
        "computer": os.environ.get("COMPUTERNAME") or socket.gethostname(),
        "user": user,
    }


def expand(text: str) -> str:
    try:
        return text.format(**_placeholders())
    except (KeyError, IndexError, ValueError):
        return text


@dataclass
class DeviceCfg:
    name: str = "ScreenCam-{computer}"
    manufacturer: str = "ScreenCam"
    model: str = "ScreenCam ONVIF"
    location: str = "Escritorio"


@dataclass
class AuthCfg:
    enabled: bool = True
    username: str = "admin"
    password: str = "admin"


@dataclass
class NetworkCfg:
    bind_ip: str = "0.0.0.0"
    advertise_ip: str = "auto"
    onvif_port: int = 8000
    rtsp_port: int = 8554
    discovery: bool = True


@dataclass
class VideoCfg:
    source: str = "screen"          # screen | test
    monitor: str = "all"            # all | 1 | 2 ...
    fps: int = 15
    max_width: int = 1920
    max_height: int = 1080
    bitrate_kbps: int = 2000
    encoder: str = "libx264"        # libx264 | h264_nvenc | h264_qsv | h264_amf
    draw_mouse: bool = True
    overlay: bool = True
    overlay_text: str = "{computer} - {user}"
    font: str = "Arial"


@dataclass
class AudioCfg:
    enabled: bool = True
    system_audio: bool = True
    microphone: bool = True
    system_gain: float = 1.0
    mic_gain: float = 1.0
    codec: str = "aac"              # aac | g711
    bitrate_kbps: int = 96


@dataclass
class ActivityCfg:
    idle_timeout_seconds: float = 30.0
    stream_mode: str = "continuous"  # continuous | on_activity
    poll_interval_ms: int = 250


@dataclass
class RecordingCfg:
    enabled: bool = False
    directory: str = ""
    segment_minutes: int = 10
    retention_days: int = 30
    max_size_gb: float = 50.0


@dataclass
class Config:
    device: DeviceCfg = field(default_factory=DeviceCfg)
    auth: AuthCfg = field(default_factory=AuthCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    video: VideoCfg = field(default_factory=VideoCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    activity: ActivityCfg = field(default_factory=ActivityCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
    path: Path | None = None

    @property
    def device_name(self) -> str:
        return expand(self.device.name)

    @property
    def overlay_text(self) -> str:
        return expand(self.video.overlay_text)

    @property
    def recording_dir(self) -> Path:
        if self.recording.directory:
            return Path(os.path.expandvars(self.recording.directory))
        return program_data_dir() / "recordings"


def _coerce(value: str, current):
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "sim", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value.strip()


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else default_config_path()
    cfg.path = p
    if not p.exists():
        return cfg
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    parser.read(p, encoding="utf-8-sig")
    for section in parser.sections():
        obj = getattr(cfg, section, None)
        if obj is None or section == "path":
            continue
        for key, value in parser.items(section):
            if hasattr(obj, key):
                setattr(obj, key, _coerce(value, getattr(obj, key)))
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    v = cfg.video
    v.fps = max(1, min(60, v.fps))
    v.max_width = max(320, v.max_width)
    v.max_height = max(240, v.max_height)
    if cfg.audio.codec not in ("aac", "g711"):
        cfg.audio.codec = "aac"
    if cfg.activity.stream_mode not in ("continuous", "on_activity"):
        cfg.activity.stream_mode = "continuous"
    if not cfg.audio.system_audio and not cfg.audio.microphone:
        cfg.audio.enabled = False
