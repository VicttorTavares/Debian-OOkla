"""Utilitários de rede: escolha de portas e identificação de quem as ocupa."""
from __future__ import annotations

import socket
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def port_free(port: int, host: str = "0.0.0.0") -> bool:
    """True se for possível abrir a porta TCP (sem SO_REUSEADDR no Windows, onde ele
    permitiria 'dividir' a porta com outro programa)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not IS_WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host if host not in ("", None) else "0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred: int, fallbacks: tuple[int, ...] = (), host: str = "0.0.0.0") -> int:
    """Primeira porta livre entre ``preferred`` e ``fallbacks``; devolve ``preferred`` se nenhuma servir."""
    seen = []
    for p in (preferred, *fallbacks):
        if p in seen:
            continue
        seen.append(p)
        if port_free(p, host):
            return p
    return preferred


def port_owner(port: int) -> str:
    """Nome do programa que escuta a porta TCP (só Windows; vazio se desconhecido)."""
    if not IS_WINDOWS:
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             timeout=15, creationflags=0x08000000).stdout
        pid = ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING" \
                    and parts[1].rsplit(":", 1)[-1] == str(port):
                pid = parts[4]
                break
        if not pid:
            return ""
        if pid == "4":
            return "Sistema (PID 4: http.sys - normalmente IIS/'Servico de Publicacao da World Wide Web', " \
                   "WinRM, SQL Reporting ou similar)"
        t = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=15, creationflags=0x08000000).stdout.strip()
        name = t.split(",")[0].strip('"') if t and not t.startswith("INFO") else "?"
        return f"{name} (PID {pid})"
    except Exception:
        return ""
