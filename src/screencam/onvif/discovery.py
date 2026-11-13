"""WS-Discovery: permite que o NVR encontre o computador como câmera ONVIF."""
from __future__ import annotations

import logging
import socket
import struct
import threading
import uuid
import xml.etree.ElementTree as ET

from . import xml as X

log = logging.getLogger(__name__)

MCAST_GRP = "239.255.255.250"
PORT = 3702
NS_D = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NS_A = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

_HEAD = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{X.NS["s"]}" xmlns:a="{NS_A}" '
         f'xmlns:d="{NS_D}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="{X.NS["tds"]}">')


def local_ip_for(peer: str) -> str:
    """IP local usado para alcançar ``peer`` (resolve máquinas com várias placas de rede)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Discovery:
    def __init__(self, device_uuid: str, port: int, scopes: list[str], advertise_ip: str = "auto",
                 bind_ip: str = "0.0.0.0"):
        self.urn = f"urn:uuid:{device_uuid}"
        self.port = port
        self.scopes = " ".join(scopes)
        self.advertise_ip = advertise_ip
        self.bind_ip = bind_ip
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._seq = 1

    def _ip(self, peer: str) -> str:
        if self.advertise_ip and self.advertise_ip != "auto":
            return self.advertise_ip
        return local_ip_for(peer if peer else "8.8.8.8")

    def _xaddr(self, peer: str) -> str:
        return f"http://{self._ip(peer)}:{self.port}/onvif/device_service"

    def _match_body(self, peer: str) -> str:
        return (f"<a:EndpointReference><a:Address>{self.urn}</a:Address></a:EndpointReference>"
                "<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>"
                f"<d:Scopes>{X.esc(self.scopes)}</d:Scopes>"
                f"<d:XAddrs>{self._xaddr(peer)}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>")

    def probe_match(self, relates_to: str, peer: str) -> bytes:
        self._seq += 1
        return (_HEAD + "<s:Header>"
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
                f"<a:RelatesTo>{X.esc(relates_to)}</a:RelatesTo>"
                "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
                "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action>"
                f'<d:AppSequence InstanceId="1" MessageNumber="{self._seq}"/>'
                "</s:Header><s:Body><d:ProbeMatches><d:ProbeMatch>"
                + self._match_body(peer) +
                "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>").encode()

    def _announce(self, kind: str) -> bytes:
        body = self._match_body("") if kind == "Hello" else \
            f"<a:EndpointReference><a:Address>{self.urn}</a:Address></a:EndpointReference>"
        return (_HEAD + "<s:Header>"
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
                "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
                f"<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/{kind}</a:Action>"
                "</s:Header><s:Body>"
                f"<d:{kind}>{body}</d:{kind}></s:Body></s:Envelope>").encode()

    @staticmethod
    def wants_us(data: bytes) -> str | None:
        """Retorna o MessageID se for um Probe que deve ser respondido."""
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return None
        probe = next((el for el in root.iter() if el.tag.endswith("}Probe")), None)
        if probe is None:
            return None
        msg_id = next((el.text for el in root.iter() if el.tag.endswith("}MessageID")), None) or ""
        types_el = next((el for el in probe if el.tag.endswith("}Types")), None)
        types = (types_el.text or "").strip() if types_el is not None else ""
        if types:
            locals_ = {t.split(":")[-1] for t in types.split()}
            if not locals_ & {"NetworkVideoTransmitter", "Device"}:
                return None
        return msg_id

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", PORT))
        iface = socket.inet_aton(self.bind_ip if self.bind_ip not in ("", "0.0.0.0") else "0.0.0.0")
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), iface)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(1.0)
        self._sock = s
        threading.Thread(target=self._run, name="ws-discovery", daemon=True).start()
        self._send_multicast("Hello")
        log.info("WS-Discovery ativo (UDP %s)", PORT)

    def _send_multicast(self, kind: str):
        try:
            if self._sock:
                self._sock.sendto(self._announce(kind), (MCAST_GRP, PORT))
        except OSError as exc:
            log.debug("Falha ao enviar %s: %s", kind, exc)

    def stop(self):
        self._send_multicast("Bye")
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            msg_id = self.wants_us(data)
            if msg_id is None:
                continue
            try:
                self._sock.sendto(self.probe_match(msg_id, addr[0]), addr)
                log.debug("ProbeMatch enviado para %s", addr[0])
            except OSError as exc:
                log.debug("Falha ao responder Probe: %s", exc)
        try:
            self._sock.close()
        except OSError:
            pass
