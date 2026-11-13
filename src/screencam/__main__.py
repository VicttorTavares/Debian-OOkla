"""Ponto de entrada: ``python -m screencam`` ou ScreenCamONVIF.exe."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import winapi
from .config import APP_NAME, APP_VERSION, load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Transmite a tela como camera ONVIF")
    parser.add_argument("-c", "--config", help="caminho do config.ini")
    parser.add_argument("--headless", action="store_true", help="sem icone na bandeja")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args = parser.parse_args(argv)

    from .app import ScreenCamApp, setup_logging
    setup_logging(args.verbose)
    log = logging.getLogger("screencam")

    if not winapi.single_instance(APP_NAME):
        log.error("Ja existe uma instancia em execucao.")
        return 1
    winapi.enable_dpi_awareness()

    cfg = load_config(args.config)
    log.info("Configuracao: %s%s", cfg.path, "" if cfg.path and cfg.path.exists() else " (padrao)")
    if cfg.auth.enabled and cfg.auth.password in ("admin", ""):
        log.warning("ATENCAO: senha padrao em uso. Altere [auth] password no config.ini.")

    app = ScreenCamApp(cfg)
    try:
        app.start()
    except OSError as exc:
        log.error("Falha ao iniciar (porta ONVIF %s em uso?): %s", cfg.network.onvif_port, exc)
        return 2

    if winapi.IS_WINDOWS and not args.headless:
        try:
            from .tray import run_tray
            run_tray(app)  # bloqueia até "Sair"
            return 0
        except Exception:
            log.exception("Bandeja indisponivel; seguindo sem icone")

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: done.set())
        except (ValueError, OSError):
            pass
    try:
        while not done.wait(1):
            pass
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
