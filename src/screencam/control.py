"""Canal de controle interno entre o serviço (SYSTEM) e os agentes de captura.

Só escuta em 127.0.0.1. Os agentes rodam na sessão do usuário e reportam
atividade (mouse/teclado) e um heartbeat; o serviço agrega tudo em um único
estado de "movimento" para o NVR. Um token simples evita que outro processo
local injete eventos por engano (não é segredo forte: o agente roda como o
usuário e a pior consequência de uma falsificação é gravar um pouco a mais).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)

SESSION_TTL = 15.0  # sem heartbeat por este tempo => sessão considerada encerrada


class ActivityAggregator:
    """Junta a atividade de várias sessões: há movimento se qualquer uma está ativa."""

    def __init__(self, on_change: Callable[[bool], None]):
        self.on_change = on_change
        self._sessions: dict[str, tuple[bool, float]] = {}  # sid -> (ativo, último heartbeat)
        self._active = False
        self._lock = threading.Lock()

    def report(self, sid: str, active: bool) -> None:
        with self._lock:
            self._sessions[sid] = (active, time.monotonic())
            self._recompute()

    def drop(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
            self._recompute()

    def sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [s for s, (_, t) in self._sessions.items() if now - t > SESSION_TTL]
            for s in stale:
                del self._sessions[s]
            if stale:
                self._recompute()

    def active_sessions(self) -> list[str]:
        with self._lock:
            return [s for s, (a, _) in self._sessions.items() if a]

    def _recompute(self) -> None:
        active = any(a for a, _ in self._sessions.values())
        if active != self._active:
            self._active = active
            try:
                self.on_change(active)
            except Exception:
                log.exception("Erro no callback de atividade agregada")


class ControlServer:
    def __init__(self, port: int, token: str, aggregator: ActivityAggregator,
                 status_fn: Callable[[], dict] | None = None, host: str = "127.0.0.1"):
        self.token = token
        self.aggregator = aggregator
        self.status_fn = status_fn or (lambda: {})
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self) -> bool:
                return self.headers.get("X-Token") == srv.token

            def do_GET(self):
                if not self._auth_ok():
                    self._json(403, {"error": "forbidden"})
                    return
                if self.path == "/status":
                    self._json(200, srv.status_fn())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth_ok():
                    self._json(403, {"error": "forbidden"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._json(400, {"error": "bad json"})
                    return
                sid = str(data.get("session", "?"))
                if self.path == "/activity":
                    srv.aggregator.report(sid, bool(data.get("active")))
                    self._json(200, {"ok": True})
                elif self.path == "/bye":
                    srv.aggregator.drop(sid)
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})

        ThreadingHTTPServer.daemon_threads = True
        self.httpd = ThreadingHTTPServer((host, port), H)
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="control", daemon=True)
        self._sweeper = threading.Thread(target=self._sweep_loop, name="control-sweep", daemon=True)
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self._thread.start()
        self._sweeper.start()
        log.info("Canal de controle em 127.0.0.1:%s", self.port)

    def _sweep_loop(self):
        while not self._stop.wait(5):
            self.aggregator.sweep()

    def stop(self):
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class ControlClient:
    """Usado pelo agente para reportar atividade ao serviço."""

    def __init__(self, endpoint: str, token: str, session_id: str, timeout: float = 3.0):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> bool:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.endpoint + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "X-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status == 200
        except Exception as exc:
            log.debug("Falha ao reportar ao serviço (%s): %s", path, exc)
            return False

    def report_activity(self, active: bool) -> bool:
        return self._post("/activity", {"session": self.session_id, "active": active})

    def goodbye(self) -> bool:
        return self._post("/bye", {"session": self.session_id})
