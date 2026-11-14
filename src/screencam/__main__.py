"""Ponto de entrada. Subcomandos:

    console           tudo em um processo (captura na sessão atual) — dev/uso simples
    service-run       executado pelo Gerenciador de Serviços do Windows (SCM)
    service-console   serviço + gerenciador de sessões em primeiro plano (depuração)
    agent ...         agente de captura de uma sessão (lançado pelo serviço)
    install-service   registra o serviço do Windows (requer admin)
    uninstall-service remove o serviço
    start | stop      inicia/para o serviço
    status            estado do serviço

Sem subcomando: inicia conforme ``[service] mode`` do config (service|console).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import winapi
from .config import APP_NAME, APP_VERSION, load_config


def _run_console(args) -> int:
    from .app import ConsoleApp, setup_logging
    setup_logging(args.verbose, component="console")
    log = logging.getLogger("screencam")

    if not winapi.single_instance(APP_NAME):
        log.error("Ja existe uma instancia em execucao.")
        return 1
    winapi.enable_dpi_awareness()

    cfg = load_config(args.config)
    log.info("Configuracao: %s%s", cfg.path, "" if cfg.path and cfg.path.exists() else " (padrao)")
    if cfg.auth.enabled and cfg.auth.password in ("admin", ""):
        log.warning("ATENCAO: senha padrao em uso. Altere [auth] password no config.ini.")

    app = ConsoleApp(cfg)
    try:
        app.start()
    except OSError as exc:
        log.error("Falha ao iniciar (porta ONVIF %s em uso?): %s", cfg.network.onvif_port, exc)
        return 2

    if winapi.IS_WINDOWS and cfg.service.visible_indicator and not args.headless:
        try:
            from .tray import run_tray
            run_tray(app)
            return 0
        except Exception:
            log.exception("Bandeja indisponivel; seguindo sem icone")

    _wait_forever(app.stop)
    return 0


def _run_service_console(args) -> int:
    from .app import setup_logging
    from .service import run_service_console
    setup_logging(args.verbose, component="service")
    cfg = load_config(args.config)
    return run_service_console(cfg)


def _wait_forever(on_stop) -> None:
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
        on_stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Tela+audio como camera ONVIF")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("-c", "--config", help="caminho do config.ini")
        p.add_argument("-v", "--verbose", action="store_true")

    p_console = sub.add_parser("console", help="processo unico (captura na sessao atual)")
    common(p_console)
    p_console.add_argument("--headless", action="store_true")

    for name in ("service-run", "service-console"):
        common(sub.add_parser(name))

    sub.add_parser("agent", add_help=False)  # argumentos próprios, tratados em agent.run_agent

    for name in ("install-service", "uninstall-service", "start", "stop", "status"):
        common(sub.add_parser(name))
    common(sub.add_parser("protect-config", help="cifra a senha do NVR no config.ini (DPAPI)"))
    common(sub.add_parser("doctor", help="diagnostico completo (config, portas, NVR, servico)"))

    # 'agent' precisa dos argumentos crus
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "agent":
        from .agent import run_agent
        return run_agent(argv[1:])

    args = parser.parse_args(argv)

    if args.cmd in (None, "console"):
        if args.cmd is None:
            args.headless = False
            args.config = getattr(args, "config", None)
            args.verbose = getattr(args, "verbose", False)
            cfg = load_config(args.config)
            if winapi.IS_WINDOWS and cfg.service.mode == "service":
                print("Modo 'service': use 'install-service' e 'start'. "
                      "Para rodar em primeiro plano use 'service-console' ou 'console'.")
                return 0
        return _run_console(args)

    if args.cmd == "service-console":
        return _run_service_console(args)

    if args.cmd == "service-run":
        from .service import service_dispatch
        service_dispatch()
        return 0

    if args.cmd == "protect-config":
        return _protect_config(args)

    if args.cmd == "doctor":
        from .doctor import run_doctor
        return run_doctor(args.config)

    return _service_control(args.cmd, args)


def _protect_config(args) -> int:
    from .config import default_config_path, protect_config_file
    from . import dpapi
    path = args.config or default_config_path()
    if not dpapi.is_available():
        print("DPAPI indisponivel (necessario Windows); senha mantida em texto.")
        return 1
    try:
        changed = protect_config_file(path)
    except Exception as exc:
        print(f"Falha ao cifrar a senha: {exc}")
        return 1
    if changed:
        print("Senha do NVR cifrada no config.ini.")
    else:
        print("Nenhuma alteracao (senha ja cifrada, vazia ou arquivo ausente).")
    return 0


def _service_control(cmd: str, args) -> int:
    if not winapi.IS_WINDOWS:
        print("Comandos de servico sao exclusivos do Windows.")
        return 1
    from . import service as svc
    try:
        if cmd == "install-service":
            svc.install_service(args.config)
            print("Agora inicie com: ScreenCamONVIF start")
            return 0
        import win32service
        import win32serviceutil
        name = svc.SERVICE_NAME
        if cmd == "uninstall-service":
            svc.remove_service()
            print("Servico removido.")
        elif cmd == "start":
            ok, msg = svc.start_service_wait()
            print(msg)
            if not ok:
                print("---- ultimas linhas do log do servico ----")
                print(svc.tail_service_log())
                print("---- rode 'ScreenCamONVIF.exe doctor' para um diagnostico completo ----")
                return 1
        elif cmd == "stop":
            svc.stop_service_quiet()
            print("Servico parado.")
        elif cmd == "status":
            state = win32serviceutil.QueryServiceStatus(name)[1]
            names = {win32service.SERVICE_RUNNING: "em execucao",
                     win32service.SERVICE_STOPPED: "parado",
                     win32service.SERVICE_START_PENDING: "iniciando",
                     win32service.SERVICE_STOP_PENDING: "parando"}
            print(f"Servico {name}: {names.get(state, state)}")
            st = svc.read_status_file()
            if st and state == win32service.SERVICE_RUNNING:
                print(f"  Porta ONVIF (gerenciamento no NVR): {st.get('onvif_port')}"
                      f"  |  RTSP: {st.get('rtsp_port')}  |  codec: {str(st.get('codec', '')).upper()}"
                      f"  |  NVR autorizado: {st.get('nvr_ip') or 'qualquer (sub-rede local)'}")
                w = st.get("wanted_ports", {})
                if w.get("onvif") and w.get("onvif") != st.get("onvif_port"):
                    print(f"  ATENCAO: a porta ONVIF {w['onvif']} estava ocupada; usando {st['onvif_port']}. "
                          f"Cadastre a camera no NVR com Porta = {st['onvif_port']}.")
            if state != win32service.SERVICE_RUNNING:
                print("---- ultimas linhas do log do servico ----")
                print(svc.tail_service_log(10))
                return 1
        return 0
    except Exception as exc:
        print(f"Falha no comando '{cmd}': {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
