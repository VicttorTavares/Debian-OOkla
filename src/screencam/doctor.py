"""Diagnóstico: ``ScreenCamONVIF.exe doctor``.

Verifica, em ordem, tudo que costuma impedir a câmera de aparecer no NVR e diz
o que fazer. Saída em linhas ``[OK]``, ``[AVISO]`` e ``[FALHA]``; o código de
saída é 1 se houver alguma FALHA.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import winapi
from .config import APP_NAME, APP_VERSION, default_config_path, is_encrypted, load_config, same_subnet24
from .onvif.discovery import local_ip_for

OK, WARN, FAIL = "OK", "AVISO", "FALHA"


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.items.append((level, title, detail))
        print(f"[{level}] {title}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(level == FAIL for level, _, _ in self.items)


def _port_state(host: str, port: int) -> str:
    """'livre' | 'servico' (responde como o ScreenCam) | 'ocupada'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host if host not in ("", "0.0.0.0") else "0.0.0.0", port))
        return "livre"
    except OSError:
        pass
    finally:
        s.close()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            if "ScreenCamONVIF" in (r.headers.get("Server") or ""):
                return "servico"
    except Exception as exc:
        server = getattr(getattr(exc, "headers", None), "get", lambda *_: "")("Server") or ""
        if "ScreenCamONVIF" in server:
            return "servico"
    return "ocupada"


def _tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_doctor(config_path: str | None = None) -> int:
    r = Report()
    print(f"== {APP_NAME} {APP_VERSION} - diagnostico ==")
    frozen = "empacotado" if getattr(sys, "frozen", False) else "codigo-fonte"
    r.add(OK, "Versao", f"{APP_VERSION} ({frozen}, Python {sys.version.split()[0]}, {sys.platform})")

    # ---------------------------------------------------------- configuracao
    path = Path(config_path) if config_path else default_config_path()
    if not path.exists():
        r.add(FAIL, "config.ini", f"nao encontrado em {path} (reinstale ou rode reparar.ps1)")
        cfg = load_config(path)
    else:
        try:
            cfg = load_config(path)
            r.add(OK, "config.ini", str(path))
        except Exception as exc:
            r.add(FAIL, "config.ini invalido", f"{path}: {exc}")
            return 1
    raw_pw = ""
    if path.exists():
        import configparser
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
        cp.read(path, encoding="utf-8-sig")
        raw_pw = cp.get("auth", "password", fallback="").strip()
    if cfg.auth.enabled:
        if is_encrypted(raw_pw) and not cfg.auth.password:
            r.add(FAIL, "Senha", "esta cifrada (DPAPI) mas nao foi possivel decifrar; o arquivo veio de outra "
                                 "maquina? Regrave a senha em texto e rode 'protect-config'")
        elif not cfg.auth.password:
            r.add(FAIL, "Senha", "vazia: o NVR nao conseguira autenticar")
        elif len(cfg.auth.password) < 10:
            r.add(WARN, "Senha", f"tem {len(cfg.auth.password)} caracteres; o gravador exige 10")
        else:
            r.add(OK, "Senha", f"{len(cfg.auth.password)} caracteres, usuario '{cfg.auth.username}'")
        if raw_pw and not is_encrypted(raw_pw) and winapi.IS_WINDOWS:
            r.add(WARN, "Senha em texto no config.ini", "rode 'ScreenCamONVIF.exe protect-config' como administrador")

    # ---------------------------------------------------------- binarios
    from .streamer import find_binary
    ffmpeg = None
    for name in ("ffmpeg", "mediamtx"):
        try:
            exe = find_binary(name)
            r.add(OK, f"{name}", exe)
            if name == "ffmpeg":
                ffmpeg = exe
        except FileNotFoundError as exc:
            r.add(FAIL, f"{name} ausente", str(exc))
    if ffmpeg:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=15,
                                 creationflags=winapi.no_window_flags()).stdout.splitlines()[0]
            r.add(OK, "ffmpeg executa", out[:80])
            from .encoders import resolve_encoder
            name = resolve_encoder(cfg, ffmpeg)
            r.add(OK, "Encoder de video", f"{name} ({cfg.video.codec.upper()})")
        except Exception as exc:
            r.add(FAIL, "ffmpeg/encoder", str(exc))

    # ---------------------------------------------------------- portas locais
    n = cfg.network
    for label, port in (("ONVIF", n.onvif_port), ("RTSP", n.rtsp_port), ("controle interno", cfg.service.control_port)):
        state = _port_state(n.bind_ip, port)
        if state == "livre":
            r.add(OK, f"Porta {label} {port}", "livre")
        elif state == "servico":
            r.add(OK, f"Porta {label} {port}", "em uso pelo proprio ScreenCam (servico ativo)")
        else:
            r.add(FAIL, f"Porta {label} {port} ocupada por outro programa",
                  "feche o programa (no Windows: 'netstat -ano | findstr :%d') ou mude a porta no config.ini" % port)

    # ---------------------------------------------------------- rede / NVR
    nvr = n.nvr_ip
    if not nvr:
        r.add(WARN, "IP do NVR", "nao informado: so a sub-rede local do PC tem acesso")
    else:
        local = local_ip_for(nvr)
        r.add(OK, "IP deste PC", f"{local} (rota para o NVR)")
        if not same_subnet24(local, nvr):
            r.add(WARN, "NVR em outra sub-rede", f"PC={local} NVR={nvr}; confira se o IP do NVR esta correto "
                                                  "(erro de digitacao?) e se ha roteamento")
        ports = sorted({80, 8000})
        reach = {p: _tcp(nvr, p) for p in ports}
        for p, ok in reach.items():
            r.add(OK if ok else WARN, f"NVR {nvr}:{p}", "acessivel" if ok else
                  "sem resposta (pode ser normal se essa porta nao for usada pelo seu NVR)")
        if not any(reach.values()):
            r.add(FAIL, "NVR inacessivel", f"nenhuma das portas {ports} respondeu em {nvr}: "
                                           "verifique cabo/IP/firewall do NVR")

    # ---------------------------------------------------------- servico (Windows)
    if winapi.IS_WINDOWS:
        try:
            import win32service
            import win32serviceutil
            state = win32serviceutil.QueryServiceStatus("ScreenCamONVIF")[1]
            names = {win32service.SERVICE_RUNNING: "em execucao", win32service.SERVICE_STOPPED: "PARADO",
                     win32service.SERVICE_START_PENDING: "iniciando"}
            level = OK if state == win32service.SERVICE_RUNNING else FAIL
            r.add(level, "Servico do Windows", names.get(state, str(state)))
        except Exception as exc:
            r.add(FAIL, "Servico do Windows", f"nao instalado ({exc}); rode 'ScreenCamONVIF.exe install-service'")
        from .service import tail_service_log
        r.add(OK, "Log do servico (ultimas linhas)", "\n" + tail_service_log(8))
    else:
        r.add(WARN, "Servico do Windows", "verificacao disponivel apenas no Windows")

    print("== resumo: " + ("HA FALHAS - corrija os itens [FALHA]" if r.failed else "sem falhas") + " ==")
    return 1 if r.failed else 0
