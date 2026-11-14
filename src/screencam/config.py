"""Carregamento da configuração (config.ini)."""
from __future__ import annotations

import configparser
import getpass
import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "ScreenCamONVIF"
APP_VERSION = "1.5.0"


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
    # IP do NVR (gravador) ao qual este dispositivo pertence. Vazio = qualquer NVR
    # da rede local. Quando preenchido, o acesso de leitura (RTSP/ONVIF) e restrito
    # a esse IP e o firewall e liberado apenas para ele.
    nvr_ip: str = ""


@dataclass
class VideoCfg:
    source: str = "screen"          # screen | test
    monitor: str = "all"            # all | 1 | 2 ...
    fps: int = 15
    max_width: int = 1920
    max_height: int = 1080
    codec: str = "h265"             # h265 (menor armazenamento) | h264 (compatibilidade)
    encoder: str = "auto"           # auto | cpu | nvenc | qsv | amf
    rate_control: str = "vbr"       # vbr (qualidade constante, economiza disco) | cbr
    quality: int = 0                # CRF/CQ; 0 = automatico (28 em H.265, 25 em H.264)
    bitrate_kbps: int = 1500        # teto de bitrate (VBR) ou bitrate fixo (CBR)
    gop_seconds: int = 4            # intervalo entre quadros-chave; maior = menos disco
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
class AutoRegisterCfg:
    # Registro ativo no NVR (a camera conecta no gravador). Util quando a busca
    # automatica nao funciona - tipicamente quando o PC e o NVR estao em sub-redes
    # diferentes, ou ha NAT entre eles. EXPERIMENTAL: ver README (depende do
    # protocolo do seu NVR Intelbras/Hikvision).
    enabled: bool = False
    server_ip: str = ""          # "End. servidor": IP do NVR (gravador)
    server_port: int = 7000      # porta do "Registro Automatico" no NVR
    device_id: str = "1"         # ID do dispositivo configurado no NVR
    username: str = ""           # usuario de login no NVR (se o registro exigir)
    password: str = ""           # senha de login no NVR (cifrada em disco via DPAPI)
    target: str = "onvif"        # servico local exposto pelo tunel: onvif | rtsp


@dataclass
class ServiceCfg:
    # service  = serviço SYSTEM + agente por sessão (produção, usuário não fecha)
    # console  = tudo em um processo na sessão atual (dev / instalação simples)
    mode: str = "service"
    service_name: str = "ScreenCamONVIF"
    # active_console = grava a sessão ativa no monitor físico (troca de usuário segue o ativo)
    # all_sessions   = grava todas as sessões interativas simultâneas (uma câmera por sessão; RDP)
    capture_scope: str = "active_console"
    # Indicador visível (ícone na bandeja) na sessão do usuário. Desligado = gravação discreta
    visible_indicator: bool = False
    # Porta do canal de controle interno (apenas 127.0.0.1, serviço <-> agentes)
    control_port: int = 8099


@dataclass
class Config:
    device: DeviceCfg = field(default_factory=DeviceCfg)
    auth: AuthCfg = field(default_factory=AuthCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    video: VideoCfg = field(default_factory=VideoCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    activity: ActivityCfg = field(default_factory=ActivityCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
    service: ServiceCfg = field(default_factory=ServiceCfg)
    autoregister: AutoRegisterCfg = field(default_factory=AutoRegisterCfg)
    path: Path | None = None

    @property
    def device_name(self) -> str:
        return expand(self.device.name)

    @property
    def gop_frames(self) -> int:
        return self.video.fps * self.video.gop_seconds

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
    cfg.auth.password = decode_password(cfg.auth.password)
    cfg.autoregister.password = decode_password(cfg.autoregister.password)
    _validate(cfg)
    return cfg


# ---------------------------------------------------------------- senha (DPAPI)

PW_PREFIX = "enc:v1:"


def _default_cipher():
    from . import dpapi
    return dpapi


def is_encrypted(stored: str) -> bool:
    return stored.startswith(PW_PREFIX)


def encode_password(plaintext: str, cipher=None) -> str:
    """Cifra a senha (DPAPI) e devolve a forma ``enc:v1:<base64>``."""
    cipher = cipher or _default_cipher()
    return PW_PREFIX + cipher.protect(plaintext)


def decode_password(stored: str, cipher=None) -> str:
    """Decifra a senha se estiver na forma ``enc:v1:...``; senão devolve como está."""
    if not is_encrypted(stored):
        return stored
    cipher = cipher or _default_cipher()
    try:
        return cipher.unprotect(stored[len(PW_PREFIX):])
    except Exception as exc:
        log.error("Nao foi possivel decifrar a senha (config copiado de outra maquina?): %s", exc)
        return ""


# seções cujo campo "password" deve ser cifrado no disco
_PASSWORD_SECTIONS = ("auth", "autoregister")


def protect_config_file(path: str | Path | None = None, cipher=None) -> bool:
    """Reescreve o config.ini cifrando as senhas em texto claro ([auth] e
    [autoregister]). Preserva comentários e demais linhas. Retorna True se
    alterou o arquivo. Não faz nada se a DPAPI nao estiver disponivel ou se nao
    houver senha em texto claro a cifrar.
    """
    cipher = cipher or _default_cipher()
    p = Path(path) if path else default_config_path()
    if not p.exists() or not cipher.is_available():
        return False
    lines = p.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    section = ""
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section not in _PASSWORD_SECTIONS or "=" not in stripped or stripped.startswith((";", "#")):
            continue
        key, _, rest = line.partition("=")
        if key.strip().lower() != "password":
            continue
        # remove comentario inline (a senha nao pode conter ; nem #)
        value = rest
        for c in (";", "#"):
            idx = value.find(c)
            if idx != -1:
                value = value[:idx]
        value = value.strip()
        if not value or is_encrypted(value):
            continue  # esta senha ja esta ok; procura outras
        newline = "\n" if line.endswith("\n") else ""
        lines[i] = f"{key}= {encode_password(value, cipher)}{newline}"
        changed = True
    if changed:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.replace(tmp, p)
        log.info("Senha(s) cifrada(s) com DPAPI em %s", p)
    return changed


def _validate(cfg: Config) -> None:
    v = cfg.video
    v.fps = max(1, min(60, v.fps))
    v.max_width = max(320, v.max_width)
    v.max_height = max(240, v.max_height)
    v.codec = {"hevc": "h265", "h.265": "h265", "h.264": "h264", "avc": "h264"}.get(v.codec.lower(), v.codec.lower())
    if v.codec not in ("h264", "h265"):
        v.codec = "h265"
    # compatibilidade com a v1.0 (encoder = libx264 / h264_nvenc ...)
    legacy = {"libx264": "cpu", "libx265": "cpu", "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc",
              "h264_qsv": "qsv", "hevc_qsv": "qsv", "h264_amf": "amf", "hevc_amf": "amf"}
    v.encoder = legacy.get(v.encoder.lower(), v.encoder.lower())
    if v.encoder not in ("auto", "cpu", "nvenc", "qsv", "amf"):
        v.encoder = "auto"
    if v.rate_control not in ("vbr", "cbr"):
        v.rate_control = "vbr"
    v.gop_seconds = max(1, min(10, v.gop_seconds))
    if not 0 <= v.quality <= 51:
        v.quality = 0
    if cfg.audio.codec not in ("aac", "g711"):
        cfg.audio.codec = "aac"
    if cfg.activity.stream_mode not in ("continuous", "on_activity"):
        cfg.activity.stream_mode = "continuous"
    if not cfg.audio.system_audio and not cfg.audio.microphone:
        cfg.audio.enabled = False
    if cfg.service.mode not in ("service", "console"):
        cfg.service.mode = "service"
    if cfg.service.capture_scope not in ("active_console", "all_sessions"):
        cfg.service.capture_scope = "active_console"
    cfg.network.nvr_ip = cfg.network.nvr_ip.strip()
    if cfg.network.nvr_ip and not _valid_ip(cfg.network.nvr_ip):
        log.warning("nvr_ip invalido (%s); tratando como 'qualquer NVR'", cfg.network.nvr_ip)
        cfg.network.nvr_ip = ""
    if cfg.auth.enabled and 0 < len(cfg.auth.password) < 10:
        log.warning("A senha tem menos de 10 caracteres; alguns NVRs (Intelbras/Hikvision) "
                    "exigem 10. Se a camera nao autenticar, use uma senha de 10 caracteres.")
    ar = cfg.autoregister
    ar.server_ip = ar.server_ip.strip()
    if ar.target not in ("onvif", "rtsp"):
        ar.target = "onvif"
    ar.server_port = max(1, min(65535, ar.server_port))
    if ar.enabled and (not ar.server_ip or not _valid_ip(ar.server_ip)):
        log.warning("autoregister habilitado mas server_ip invalido/ausente (%r); desativando",
                    ar.server_ip)
        ar.enabled = False
    # o NVR do auto-registro tambem e o NVR autorizado para leitura, se nao houver outro
    if ar.server_ip and _valid_ip(ar.server_ip) and not cfg.network.nvr_ip:
        cfg.network.nvr_ip = ar.server_ip


def _valid_ip(text: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def same_subnet24(ip_a: str, ip_b: str) -> bool:
    """True se os dois IPv4 estão na mesma sub-rede /24 (ex.: 192.168.25.x)."""
    try:
        import ipaddress
        a = ipaddress.ip_address(ip_a)
        b = ipaddress.ip_address(ip_b)
        if a.version != 4 or b.version != 4:
            return False
        return int(a) >> 8 == int(b) >> 8
    except ValueError:
        return False


# ---------------------------------------------------------------- agente

# Seções entregues ao agente de captura. NUNCA inclui [auth] (senha do NVR) nem
# credenciais: o agente publica no MediaMTX local (loopback, sem senha) e não
# precisa de nada sigiloso. Assim o config.ini pode ficar legível só para admins.
_AGENT_SECTIONS = ("device", "video", "audio", "activity")


def agent_payload(cfg: Config) -> dict:
    from dataclasses import asdict
    data = {s: asdict(getattr(cfg, s)) for s in _AGENT_SECTIONS}
    data["rtsp_port"] = cfg.network.rtsp_port
    return data


def config_from_agent_payload(data: dict) -> Config:
    cfg = Config()
    for section in _AGENT_SECTIONS:
        obj = getattr(cfg, section)
        for key, value in (data.get(section) or {}).items():
            if hasattr(obj, key):
                setattr(obj, key, value)
    cfg.network.rtsp_port = int(data.get("rtsp_port", cfg.network.rtsp_port))
    cfg.auth.enabled = False  # o agente não autentica; isso é tarefa do serviço
    cfg.network.discovery = False
    cfg.recording.enabled = False
    cfg.service.mode = "console"
    _validate(cfg)
    return cfg
