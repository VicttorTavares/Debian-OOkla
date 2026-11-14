"""Registro ativo no NVR ("Registro Automático" / End. servidor).

EXPERIMENTAL. Serve para o caso em que a busca automática (WS-Discovery) não
encontra a câmera — tipicamente quando o PC e o NVR estão em **sub-redes
diferentes** ou há NAT entre eles. Em vez de esperar o NVR conectar, o programa
**conecta ao NVR** (como a tela "End. servidor / ID dispositivo" das câmeras
Intelbras/Dahua) e abre um **túnel reverso**: tudo que o NVR enviar por essa
conexão é repassado ao serviço ONVIF (ou RTSP) local, e as respostas voltam.
Assim o NVR alcança a "câmera" mesmo sem rota direta até o PC.

Observações honestas:
* O enquadramento exato do handshake de registro é proprietário e varia por
  fabricante/modelo. Aqui enviamos, opcionalmente, um preâmbulo simples com o
  ID do dispositivo e então fazemos a ponte dos bytes. Pode ser necessário
  ajustar o preâmbulo para o seu NVR (uma captura de pacotes resolve).
* A camada de transporte (conectar, reconectar, repassar bytes) é testável e
  está coberta por testes; o que depende do NVR é só o formato do preâmbulo.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

RECONNECT_MIN = 3.0
RECONNECT_MAX = 30.0
BUF = 65536


def build_preamble(device_id: str) -> bytes:
    """Preâmbulo enviado ao NVR logo após conectar. Formato genérico, ajustável.
    Vazio quando device_id não é informado."""
    device_id = (device_id or "").strip()
    if not device_id:
        return b""
    return f"deviceID:{device_id}\r\n".encode()


def _pump(src: socket.socket, dst: socket.socket, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        stop.set()


def bridge(nvr_sock: socket.socket, local_addr: tuple[str, int],
           preamble: bytes = b"", connect_timeout: float = 5.0) -> None:
    """Liga a conexão do NVR a um serviço local (ONVIF/RTSP), repassando bytes
    nos dois sentidos até qualquer lado fechar."""
    local = socket.create_connection(local_addr, timeout=connect_timeout)
    stop = threading.Event()
    try:
        if preamble:
            nvr_sock.sendall(preamble)
        t1 = threading.Thread(target=_pump, args=(nvr_sock, local, stop), daemon=True)
        t2 = threading.Thread(target=_pump, args=(local, nvr_sock, stop), daemon=True)
        t1.start()
        t2.start()
        while not stop.wait(0.5):
            pass
    finally:
        for s in (local, nvr_sock):
            try:
                s.close()
            except OSError:
                pass


class AutoRegister:
    def __init__(self, cfg, local_onvif_port: int, local_rtsp_port: int):
        self.cfg = cfg.autoregister
        self.local_port = local_onvif_port if self.cfg.target == "onvif" else local_rtsp_port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.warning("Registro ativo no NVR ATIVADO (experimental): conectando a %s:%s, "
                    "device_id=%s, encaminhando para o servico %s local (porta %s).",
                    self.cfg.server_ip, self.cfg.server_port, self.cfg.device_id,
                    self.cfg.target.upper(), self.local_port)
        self._thread = threading.Thread(target=self._run, name="autoregister", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        preamble = build_preamble(self.cfg.device_id)
        while not self._stop.is_set():
            try:
                log.info("Conectando ao NVR %s:%s...", self.cfg.server_ip, self.cfg.server_port)
                nvr = socket.create_connection((self.cfg.server_ip, self.cfg.server_port), timeout=10)
                log.info("Conectado ao NVR; aguardando o gravador puxar o stream (tunel ativo).")
                backoff = RECONNECT_MIN
                bridge(nvr, ("127.0.0.1", self.local_port), preamble=preamble)
                log.info("Conexao com o NVR encerrada; tentando novamente.")
            except OSError as exc:
                log.warning("Falha ao conectar ao NVR %s:%s: %s (nova tentativa em %.0fs)",
                            self.cfg.server_ip, self.cfg.server_port, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
