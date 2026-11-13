"""Gerência das sessões de usuário: garante um agente de captura rodando em
cada sessão que deve ser gravada e o reinicia se o usuário o encerrar.

A lógica (SessionManager) é separada do Windows por meio de um "backend", o
que permite testá-la no Linux com um backend falso. O backend real
(WindowsSessionBackend) usa as APIs WTS/CreateProcessAsUser.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionInfo:
    sid: int
    user: str
    active: bool          # sessão conectada a um console/RDP (visível)
    is_console: bool


class AgentHandle(Protocol):
    def is_alive(self) -> bool: ...
    def terminate(self) -> None: ...


class SessionBackend(Protocol):
    def list_sessions(self) -> list[SessionInfo]: ...
    def launch_agent(self, sid: int) -> AgentHandle | None: ...


class SessionManager:
    """Observa as sessões e mantém agentes conforme ``capture_scope``.

    Canal único compartilhado: um único agente captura por vez, na sessão ativa.
    Em troca de usuário (console) ou múltiplas sessões RDP, o canal segue a sessão
    ativa — prioriza o console físico e, entre sessões RDP, a de menor ID (estável).
    Todos os usuários acabam gravados no mesmo canal do NVR, um de cada vez.
    """

    def __init__(self, backend: SessionBackend, capture_scope: str = "active_console",
                 poll_interval: float = 2.0, respawn_backoff: float = 3.0,
                 clock=time.monotonic):
        self.backend = backend
        self.capture_scope = capture_scope  # mantido para compatibilidade/registro
        self.poll_interval = poll_interval
        self.respawn_backoff = respawn_backoff
        self._clock = clock
        self._agents: dict[int, AgentHandle] = {}
        self._next_allowed: dict[int, float] = {}  # sid -> quando pode relançar (backoff)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def _targets(self, sessions: list[SessionInfo]) -> set[int]:
        """Escolhe no máximo UMA sessão (canal único): console ativo tem prioridade;
        entre sessões RDP ativas, a de menor ID, para a escolha ser estável."""
        usable = [s for s in sessions if s.user and s.active]
        console = [s for s in usable if s.is_console]
        pool = console or usable
        chosen = min(pool, key=lambda s: s.sid) if pool else None
        return {chosen.sid} if chosen else set()

    def reconcile(self) -> None:
        """Um passo: lança o que falta, mata o que sobra, respawn com backoff."""
        try:
            sessions = self.backend.list_sessions()
        except Exception:
            log.exception("Falha ao listar sessões")
            return
        targets = self._targets(sessions)

        # encerra agentes de sessões que não são mais alvo
        for sid in list(self._agents):
            if sid not in targets:
                self._kill(sid)

        now = self._clock()
        for sid in targets:
            handle = self._agents.get(sid)
            if handle is not None and handle.is_alive():
                continue
            if handle is not None:  # morreu (usuário encerrou?) -> respawn com backoff
                self._agents.pop(sid, None)
                log.warning("Agente da sessão %s nao esta mais ativo; sera reiniciado", sid)
            if now < self._next_allowed.get(sid, 0):
                continue
            self._next_allowed[sid] = now + self.respawn_backoff
            try:
                new = self.backend.launch_agent(sid)
            except Exception:
                log.exception("Falha ao lançar agente na sessão %s", sid)
                new = None
            if new is not None:
                self._agents[sid] = new
                log.info("Agente de captura iniciado na sessão %s", sid)

    def _kill(self, sid: int) -> None:
        handle = self._agents.pop(sid, None)
        self._next_allowed.pop(sid, None)
        if handle is not None:
            try:
                handle.terminate()
            except Exception:
                log.debug("Falha ao encerrar agente da sessão %s", sid, exc_info=True)
            log.info("Agente de captura encerrado na sessão %s", sid)

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="session-mgr", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.reconcile()
            self._stop.wait(self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        for sid in list(self._agents):
            self._kill(sid)
        if self._thread:
            self._thread.join(timeout=3)


# ======================================================================
# Backend real do Windows
# ======================================================================

def make_windows_backend(agent_argv_fn):
    """Cria o backend WTS. ``agent_argv_fn(sid) -> list[str]`` monta a linha de
    comando do agente (executável + argumentos) para a sessão informada."""
    import ctypes
    from ctypes import wintypes

    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    WTS_CURRENT_SERVER_HANDLE = 0
    WTSActive = 0  # WTS_CONNECTSTATE_CLASS.WTSActive

    class WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD),
                    ("pWinStationName", wintypes.LPWSTR),
                    ("State", ctypes.c_int)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_CONSOLE = 0x00000010
    STILL_ACTIVE = 259

    class _WinAgent:
        def __init__(self, hProcess, hThread, pid):
            self._h = hProcess
            self._pid = pid
            kernel32.CloseHandle(hThread)

        def is_alive(self) -> bool:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(self._h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE

        def terminate(self) -> None:
            kernel32.TerminateProcess(self._h, 0)
            kernel32.CloseHandle(self._h)

    class WindowsSessionBackend:
        def list_sessions(self) -> list[SessionInfo]:
            p = ctypes.POINTER(WTS_SESSION_INFOW)()
            count = wintypes.DWORD()
            if not wtsapi32.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1,
                                                  ctypes.byref(p), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            out = []
            try:
                for i in range(count.value):
                    s = p[i]
                    user = self._session_user(s.SessionId)
                    if not user:
                        continue
                    out.append(SessionInfo(sid=int(s.SessionId), user=user,
                                           active=(s.State == WTSActive),
                                           is_console=(s.pWinStationName or "").lower() == "console"))
            finally:
                wtsapi32.WTSFreeMemory(p)
            return out

        def _session_user(self, sid: int) -> str:
            buf = wintypes.LPWSTR()
            n = wintypes.DWORD()
            # WTSUserName = 5
            if wtsapi32.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, sid, 5,
                                                    ctypes.byref(buf), ctypes.byref(n)):
                try:
                    return buf.value or ""
                finally:
                    wtsapi32.WTSFreeMemory(buf)
            return ""

        def launch_agent(self, sid: int):
            htok = wintypes.HANDLE()
            if not wtsapi32.WTSQueryUserToken(sid, ctypes.byref(htok)):
                raise ctypes.WinError(ctypes.get_last_error())
            env = ctypes.c_void_p()
            created = False
            try:
                if not userenv.CreateEnvironmentBlock(ctypes.byref(env), htok, False):
                    env = ctypes.c_void_p()  # segue sem bloco de ambiente
                si = STARTUPINFOW()
                si.cb = ctypes.sizeof(si)
                si.lpDesktop = "winsta0\\default"  # área de trabalho interativa do usuário
                pi = PROCESS_INFORMATION()
                cmd = subprocess_list2cmdline(agent_argv_fn(sid))
                flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
                ok = advapi32.CreateProcessAsUserW(
                    htok, None, ctypes.create_unicode_buffer(cmd), None, None, False,
                    flags, env, None, ctypes.byref(si), ctypes.byref(pi))
                if not ok:
                    raise ctypes.WinError(ctypes.get_last_error())
                created = True
                return _WinAgent(pi.hProcess, pi.hThread, pi.dwProcessId)
            finally:
                if env:
                    userenv.DestroyEnvironmentBlock(env)
                kernel32.CloseHandle(htok)
                if not created:
                    pass

    return WindowsSessionBackend()


def subprocess_list2cmdline(args: list[str]) -> str:
    import subprocess
    return subprocess.list2cmdline(args)
