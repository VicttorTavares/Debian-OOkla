"""Agente de captura: roda na sessão interativa do usuário, lançado pelo serviço.

Lê um payload não-sensível (sem senhas), captura tela+áudio e publica no
MediaMTX local, reportando atividade ao serviço pelo canal de controle.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from pathlib import Path

from . import winapi
from .app import CaptureAgent, setup_logging
from .config import config_from_agent_payload
from .control import ControlClient

log = logging.getLogger("screencam.agent")

HEARTBEAT_SECONDS = 5.0


def run_agent(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="screencam agent")
    parser.add_argument("--control", required=True, help="endpoint do canal de controle")
    parser.add_argument("--token", required=True)
    parser.add_argument("--session", default="?")
    parser.add_argument("--payload", required=True, help="arquivo JSON com a config não-sensível")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose, component="agent")
    winapi.enable_dpi_awareness()

    try:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Nao foi possivel ler o payload %s: %s", args.payload, exc)
        return 2
    cfg = config_from_agent_payload(payload)

    client = ControlClient(args.control, args.token, args.session)
    agent = CaptureAgent(cfg, report_fn=client.report_activity, session_label=f"sessao {args.session}")

    indicator = None
    if payload.get("visible_indicator"):
        indicator = _start_indicator(agent)

    agent.start()
    log.info("Agente pronto na sessao %s", args.session)

    stop = threading.Event()

    def _hb():
        # reenvia o estado atual periodicamente (mantem a sessao viva no agregador)
        while not stop.wait(HEARTBEAT_SECONDS):
            client.report_activity(agent.activity.active)

    threading.Thread(target=_hb, name="agent-heartbeat", daemon=True).start()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):
                pass
    try:
        while not stop.wait(1):
            pass
    finally:
        agent.stop()
        client.goodbye()
        if indicator is not None:
            try:
                indicator.stop()
            except Exception:
                pass
    return 0


def _start_indicator(agent: CaptureAgent):
    """Ícone mínimo na bandeja (quando visible_indicator = true)."""
    try:
        import pystray
        from .tray import ICON_ACTIVE, ICON_IDLE, _icon
    except Exception:
        return None

    def image():
        return _icon(ICON_ACTIVE if agent.activity.active else ICON_IDLE)

    icon = pystray.Icon("ScreenCamONVIF", image(), "ScreenCam ONVIF (gravacao ativa)",
                        pystray.Menu(pystray.MenuItem(
                            lambda _i: "Gravando" if agent.activity.active else "Ocioso", None, enabled=False)))

    def refresh():
        while True:
            time.sleep(1)
            try:
                icon.icon = image()
            except Exception:
                return

    threading.Thread(target=refresh, daemon=True).start()
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


if __name__ == "__main__":
    raise SystemExit(run_agent())
