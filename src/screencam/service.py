"""Serviço Windows (SYSTEM) + gerenciamento das sessões de usuário.

O serviço roda o ServerCore (RTSP/ONVIF/descoberta/gravação/controle) e um
SessionManager que injeta o agente de captura em cada sessão que deve ser
gravada, reiniciando-o caso o usuário tente encerrá-lo.

Em outras plataformas (Linux), apenas ``run_service_console`` existe para
exercitar o ServerCore; o gerenciamento de sessões é específico do Windows.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

from .app import ServerCore, setup_logging
from .config import APP_NAME, Config, agent_payload, load_config, program_data_dir

log = logging.getLogger("screencam.service")


def agent_base_argv() -> list[str]:
    """Começo da linha de comando para lançar o agente (frozen ou python -m)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "agent"]
    return [sys.executable, "-m", "screencam", "agent"]


def write_agent_payload(cfg: Config) -> Path:
    """Grava o payload não-sensível (sem senhas) num local legível pelos usuários."""
    runtime = program_data_dir() / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    payload = agent_payload(cfg)
    payload["visible_indicator"] = cfg.service.visible_indicator
    path = runtime / "agent.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ServiceRunner:
    """Une ServerCore + SessionManager. Reutilizável pelo serviço e pelo modo console do serviço."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.core = ServerCore(cfg)
        self.manager = None

    def start(self) -> None:
        # cifra a senha no disco (DPAPI) caso tenha sido deixada em texto claro
        try:
            from .config import protect_config_file
            if self.cfg.path:
                protect_config_file(self.cfg.path)
        except Exception:
            log.debug("Falha ao cifrar a senha no config", exc_info=True)
        self.core.start()
        payload_path = write_agent_payload(self.cfg)
        endpoint = f"http://127.0.0.1:{self.core.control.port}"
        token = self.core.control_token

        def agent_argv(sid: int) -> list[str]:
            return agent_base_argv() + ["--control", endpoint, "--token", token,
                                        "--session", str(sid), "--payload", str(payload_path)]

        if sys.platform == "win32":
            from .sessions import SessionManager, make_windows_backend
            backend = make_windows_backend(agent_argv)
            self.manager = SessionManager(backend, self.cfg.service.capture_scope)
            self.manager.start()
            log.info("Gerenciador de sessoes ativo (scope=%s)", self.cfg.service.capture_scope)
        else:
            log.warning("Gerenciamento de sessoes indisponivel fora do Windows; "
                        "nenhum agente sera injetado (use o modo console para capturar).")

    def stop(self) -> None:
        if self.manager:
            self.manager.stop()
        self.core.stop()


def run_service_console(cfg: Config) -> int:
    """Executa o serviço em primeiro plano (depuração no Windows; no Linux sem captura)."""
    import signal
    runner = ServiceRunner(cfg)
    runner.start()
    done = threading.Event()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: done.set())
            except (ValueError, OSError):
                pass
    try:
        while not done.wait(1):
            pass
    finally:
        runner.stop()
    return 0


# ======================================================================
# Integração com o Gerenciador de Serviços do Windows (pywin32)
# ======================================================================

def _load_pywin32():
    import servicemanager  # noqa: F401
    import win32service
    import win32serviceutil
    return win32serviceutil, win32service, servicemanager


def build_service_class(config_path: str | None = None):
    win32serviceutil, win32service, servicemanager = _load_pywin32()

    class ScreenCamService(win32serviceutil.ServiceFramework):
        _svc_name_ = "ScreenCamONVIF"
        _svc_display_name_ = "ScreenCam ONVIF"
        _svc_description_ = ("Transmite a tela e o audio deste computador como camera IP ONVIF "
                             "para gravacao em NVR, com um agente de captura por sessao de usuario.")

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            import win32event
            self._stop_evt = win32event.CreateEvent(None, 0, 0, None)
            self.runner = None

        def SvcStop(self):
            import win32event
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.runner:
                self.runner.stop()
            win32event.SetEvent(self._stop_evt)

        def SvcDoRun(self):
            import win32event
            setup_logging(component="service", to_programdata=True)
            try:
                cfg = load_config(config_path)
                if cfg.auth.enabled and cfg.auth.password in ("admin", ""):
                    log.warning("ATENCAO: senha padrao em uso; altere [auth] password.")
                self.runner = ServiceRunner(cfg)
                self.runner.start()
                servicemanager.LogInfoMsg(f"{APP_NAME} iniciado.")
                win32event.WaitForSingleObject(self._stop_evt, win32event.INFINITE)
            except Exception:
                log.exception("Erro fatal no servico")
                servicemanager.LogErrorMsg("Falha ao iniciar ScreenCam ONVIF; veja o log.")
                raise

    return ScreenCamService, win32serviceutil


def install_service(config_path: str | None = None) -> int:
    cls, win32serviceutil = build_service_class(config_path)
    import win32service
    frozen = getattr(sys, "frozen", False)
    # Em executável empacotado (PyInstaller), o SCM roda "<exe> service-run";
    # fora dele, o PythonService.exe do pywin32 localiza a classe pelo módulo.
    kwargs = dict(startType=win32service.SERVICE_AUTO_START, description=cls._svc_description_)
    if frozen:
        kwargs["exeName"] = sys.executable
        kwargs["exeArgs"] = "service-run"
    win32serviceutil.InstallService(
        f"{cls.__module__}.{cls.__name__}", cls._svc_name_, cls._svc_display_name_, **kwargs)
    # reinicia sozinho caso o processo caia
    import subprocess
    subprocess.run(["sc", "failure", cls._svc_name_, "reset=", "86400",
                    "actions=", "restart/5000/restart/5000/restart/10000"], check=False)
    print(f"Servico '{cls._svc_name_}' instalado.")
    return 0


def service_dispatch() -> None:
    """Ponto de entrada quando o SCM inicia o executável do serviço."""
    cls, _ = build_service_class()
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(cls)
    servicemanager.StartServiceCtrlDispatcher()
