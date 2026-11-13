"""Seleção do encoder de vídeo (H.265/H.264, CPU ou GPU) e seus parâmetros.

Com ``encoder = auto`` o programa testa, na ordem, NVIDIA (NVENC), Intel
(Quick Sync) e AMD (AMF) e usa a CPU (x265/x264) se nenhuma GPU funcionar.
Codificar H.265 na CPU é pesado; a GPU reduz drasticamente o consumo.
"""
from __future__ import annotations

import logging
import subprocess

from . import winapi
from .config import Config

log = logging.getLogger(__name__)

ENCODERS = {
    ("h265", "cpu"): "libx265", ("h265", "nvenc"): "hevc_nvenc",
    ("h265", "qsv"): "hevc_qsv", ("h265", "amf"): "hevc_amf",
    ("h264", "cpu"): "libx264", ("h264", "nvenc"): "h264_nvenc",
    ("h264", "qsv"): "h264_qsv", ("h264", "amf"): "h264_amf",
}
HW_ORDER = ("nvenc", "qsv", "amf")
DEFAULT_QUALITY = {"h265": 28, "h264": 25}


def probe_encoder(ffmpeg: str, name: str) -> bool:
    """Codifica alguns quadros de teste para saber se o encoder funciona nesta máquina."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=gray:s=640x360:r=5", "-frames:v", "3", "-pix_fmt", "yuv420p",
           "-c:v", name, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=20, creationflags=winapi.no_window_flags())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def resolve_encoder(cfg: Config, ffmpeg: str, probe=probe_encoder) -> str:
    """Define ``cfg.video.codec``/``cfg.video.encoder`` efetivos e retorna o nome do encoder FFmpeg."""
    v = cfg.video
    codecs = [v.codec] + (["h264"] if v.codec == "h265" else [])
    for codec in codecs:
        if v.encoder == "auto":
            kinds = list(HW_ORDER if winapi.IS_WINDOWS else ()) + ["cpu"]
        else:
            kinds = [v.encoder, "cpu"] if v.encoder != "cpu" else ["cpu"]
        for kind in kinds:
            name = ENCODERS[(codec, kind)]
            if probe(ffmpeg, name):
                if codec != v.codec:
                    log.warning("H.265 indisponivel neste FFmpeg; usando H.264 (%s)", name)
                elif v.encoder not in ("auto", kind):
                    log.warning("Encoder '%s' indisponivel; usando %s", v.encoder, name)
                v.codec, v.encoder = codec, kind
                log.info("Encoder de video: %s (%s)", name, "GPU" if kind != "cpu" else "CPU")
                return name
    raise RuntimeError("Nenhum encoder de video funcionou (FFmpeg sem libx264/libx265?)")


def encoder_args(cfg: Config) -> list[str]:
    """Parâmetros ajustados para conteúdo de tela (texto nítido, muita área parada).

    Medido em tela 1080p com texto: GOP de 4 s + lookahead + H.265 reduziu ~54% o
    bitrate em relação à v1.0 (H.264, GOP de 2 s, zerolatency) com a mesma qualidade (SSIM).
    O lookahead acrescenta ~1 s de atraso no ao vivo, irrelevante para gravação.
    """
    v = cfg.video
    name = ENCODERS[(v.codec, v.encoder if v.encoder != "auto" else "cpu")]
    gop = cfg.gop_frames
    la = max(1, min(v.fps, 20))  # lookahead de ~1 s
    q = v.quality or DEFAULT_QUALITY[v.codec]
    br, buf = v.bitrate_kbps, v.bitrate_kbps * 2
    vbr = v.rate_control == "vbr"
    args = ["-c:v", name]

    if name == "libx265":
        params = [f"keyint={gop}", f"min-keyint={gop}", "scenecut=0", "bframes=0", f"rc-lookahead={la}",
                  "repeat-headers=1", "log-level=error", f"vbv-maxrate={br}", f"vbv-bufsize={buf}"]
        params.append(f"crf={q}" if vbr else f"bitrate={br}")
        args += ["-preset", "faster", "-profile:v", "main", "-x265-params", ":".join(params)]
    elif name == "libx264":
        args += ["-preset", "veryfast", "-profile:v", "main", "-rc-lookahead", str(la),
                 "-sc_threshold", "0", "-bf", "0"]
        args += ["-crf", str(q)] if vbr else ["-b:v", f"{br}k"]
        args += ["-maxrate", f"{br}k", "-bufsize", f"{buf}k"]
    elif name.endswith("_nvenc"):
        args += ["-preset", "p5", "-tune", "hq", "-profile:v", "main", "-bf", "0",
                 "-rc-lookahead", str(la), "-no-scenecut", "1", "-forced-idr", "1"]
        if vbr:
            args += ["-rc", "vbr", "-cq", str(q), "-b:v", "0", "-maxrate", f"{br}k", "-bufsize", f"{buf}k"]
        else:
            args += ["-rc", "cbr", "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{buf}k"]
    elif name.endswith("_qsv"):
        args += ["-preset", "veryfast", "-profile:v", "main", "-bf", "0"]
        args += ["-b:v", f"{br // 2 if vbr else br}k", "-maxrate", f"{br}k", "-bufsize", f"{buf}k"]
    elif name.endswith("_amf"):
        args += ["-usage", "transcoding", "-quality", "balanced", "-bf", "0"]
        if vbr:
            args += ["-rc", "vbr_peak", "-b:v", f"{br // 2}k", "-maxrate", f"{br}k", "-bufsize", f"{buf}k"]
        else:
            args += ["-rc", "cbr", "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{buf}k"]

    args += ["-g", str(gop), "-keyint_min", str(gop), "-r", str(v.fps)]
    return args
