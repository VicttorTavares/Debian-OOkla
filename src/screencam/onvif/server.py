"""Servidor ONVIF (Profile S + eventos): Device, Media e Events em SOAP 1.2."""
from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import quote

from ..config import APP_VERSION, Config
from . import auth
from . import xml as X
from .events import (EventBroker, event_properties_xml, parse_duration, parse_termination,
                     termination_after, utc_iso)

log = logging.getLogger(__name__)

NS_DEVICE = X.NS["tds"]
NS_MEDIA = X.NS["trt"]
NS_MEDIA2 = X.NS["tr2"]
NS_EVENTS = X.NS["tev"]
NS_WSNT = X.NS["wsnt"]
NS_WSA = X.NS["wsa"]

PROFILE = "Profile_1"
VS, VSC, VEC = "VS_1", "VSC_1", "VEC_1"
AS, ASC, AEC = "AS_1", "ASC_1", "AEC_1"

# Operações liberadas sem autenticação (ONVIF Core, "pre-auth")
PUBLIC_OPS = {"GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetServiceCapabilities",
              "GetWsdlUrl", "GetHostname", "GetEndpointReference"}


def device_uuid(cfg: Config) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"screencam-{socket.gethostname()}-{cfg.network.onvif_port}"))


def scopes(cfg: Config) -> list[str]:
    q = lambda s: quote(s, safe="")  # noqa: E731
    items = ["onvif://www.onvif.org/Profile/Streaming",
             "onvif://www.onvif.org/type/video_encoder",
             "onvif://www.onvif.org/type/Network_Video_Transmitter",
             f"onvif://www.onvif.org/hardware/{q(cfg.device.model)}",
             f"onvif://www.onvif.org/name/{q(cfg.device_name)}",
             f"onvif://www.onvif.org/location/{q(cfg.device.location)}"]
    if cfg.audio.enabled:
        items.insert(2, "onvif://www.onvif.org/type/audio_encoder")
    if cfg.video.codec == "h265":
        items.insert(1, "onvif://www.onvif.org/Profile/T")  # NVRs usam para habilitar Media2/H.265
    return items


class OnvifContext:
    """Informações que o servidor precisa sobre o "dispositivo"."""

    def __init__(self, cfg: Config, geometry_fn: Callable, snapshot_fn: Callable[[], bytes],
                 broker: EventBroker):
        self.cfg = cfg
        self.geometry_fn = geometry_fn
        self.snapshot_fn = snapshot_fn
        self.broker = broker
        self.uuid = device_uuid(cfg)
        self.started = time.time()


# ======================================================================
# Builders XML
# ======================================================================

def _vsc(ctx: OnvifContext, tag: str) -> str:
    g = ctx.geometry_fn()
    return (f'<{tag} token="{VSC}"><tt:Name>{VSC}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:SourceToken>{VS}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{g.out_w}" height="{g.out_h}"/></{tag}>')


def _asc(tag: str) -> str:
    return (f'<{tag} token="{ASC}"><tt:Name>{ASC}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:SourceToken>{AS}</tt:SourceToken></{tag}>")


_MULTICAST = ("<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address>"
              "</tt:Address><tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart>"
              "</tt:Multicast><tt:SessionTimeout>PT60S</tt:SessionTimeout>")


def _vec(ctx: OnvifContext, tag: str) -> str:
    g, v = ctx.geometry_fn(), ctx.cfg.video
    return (f'<{tag} token="{VEC}"><tt:Name>{VEC}</tt:Name><tt:UseCount>1</tt:UseCount>'
            # Media1 só conhece JPEG/MPEG4/H264 no esquema. Com H.265 ativo o NVR deve usar o
            # Media2 (que informa H265); NVRs que só falam Media1 leem o codec real do SDP do RTSP.
            f"<tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{g.out_w}</tt:Width><tt:Height>{g.out_h}</tt:Height></tt:Resolution>"
            f"<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{v.fps}</tt:FrameRateLimit>"
            f"<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{v.bitrate_kbps}</tt:BitrateLimit>"
            f"</tt:RateControl>"
            f"<tt:H264><tt:GovLength>{ctx.cfg.gop_frames}</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            f"{_MULTICAST}</{tag}>")


def _audio_params(cfg: Config) -> tuple[str, int, int]:
    if cfg.audio.codec == "g711":
        return "G711", 64, 8
    return "AAC", cfg.audio.bitrate_kbps, 48


def _aec(ctx: OnvifContext, tag: str) -> str:
    enc, br, sr = _audio_params(ctx.cfg)
    return (f'<{tag} token="{AEC}"><tt:Name>{AEC}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:Encoding>{enc}</tt:Encoding><tt:Bitrate>{br}</tt:Bitrate>"
            f"<tt:SampleRate>{sr}</tt:SampleRate>{_MULTICAST}</{tag}>")


def _profile(ctx: OnvifContext, tag: str) -> str:
    audio = ctx.cfg.audio.enabled
    return (f'<{tag} token="{PROFILE}" fixed="true"><tt:Name>MainStream</tt:Name>'
            + _vsc(ctx, "tt:VideoSourceConfiguration")
            + (_asc("tt:AudioSourceConfiguration") if audio else "")
            + _vec(ctx, "tt:VideoEncoderConfiguration")
            + (_aec(ctx, "tt:AudioEncoderConfiguration") if audio else "")
            + f"</{tag}>")


def _version(major=2, minor=5) -> str:
    return f"<tt:Major>{major}</tt:Major><tt:Minor>{minor}</tt:Minor>"


def _dt(dt: datetime) -> str:
    return (f"<tt:Time><tt:Hour>{dt.hour}</tt:Hour><tt:Minute>{dt.minute}</tt:Minute>"
            f"<tt:Second>{dt.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{dt.year}</tt:Year><tt:Month>{dt.month}</tt:Month>"
            f"<tt:Day>{dt.day}</tt:Day></tt:Date>")


def _posix_tz() -> str:
    offset = -time.timezone if not time.localtime().tm_isdst else -time.altzone
    h, m = divmod(abs(offset) // 60, 60)
    sign = "-" if offset > 0 else "+"  # POSIX: sinal invertido
    return f"UTC{sign}{h:02d}:{m:02d}" if offset else "UTC0"


_DEVICE_CAPS = ('<tds:Capabilities><tds:Network IPFilter="false" ZeroConfiguration="false" '
                'IPVersion6="false" DynDNS="false" Dot11Configuration="false" HostnameFromDHCP="false" NTP="0"/>'
                '<tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" OnboardKeyGeneration="false" '
                'AccessPolicyConfig="false" DefaultAccessPolicy="false" Dot1X="false" RemoteUserHandling="false" '
                'X.509Token="false" SAMLToken="false" KerberosToken="false" UsernameToken="true" '
                'HttpDigest="true" RELToken="false"/>'
                '<tds:System DiscoveryResolve="false" DiscoveryBye="true" RemoteDiscovery="false" '
                'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false" HttpFirmwareUpgrade="false" '
                'HttpSystemBackup="false" HttpSystemLogging="false" HttpSupportInformation="false"/>'
                "</tds:Capabilities>")
_MEDIA_CAPS = ('<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
               '<trt:ProfileCapabilities MaximumNumberOfProfiles="1"/>'
               '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" '
               'NonAggregateControl="false" NoRTSPStreaming="false"/></trt:Capabilities>')
_MEDIA2_CAPS = ('<tr2:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false" '
                'TemporaryOSDText="false" Mask="false" SourceMask="false">'
                '<tr2:ProfileCapabilities MaximumNumberOfProfiles="1" '
                'ConfigurationsSupported="VideoSource VideoEncoder AudioSource AudioEncoder"/>'
                '<tr2:StreamingCapabilities RTSPStreaming="true" RTPMulticast="false" RTP_RTSP_TCP="true" '
                'NonAggregateControl="false" AutoStartMulticast="false"/></tr2:Capabilities>')
_EVENTS_CAPS = ('<tev:Capabilities WSSubscriptionPolicySupport="false" WSPullPointSupport="true" '
                'WSPausableSubscriptionManagerInterfaceSupport="false" MaxNotificationProducers="10" '
                'MaxPullPoints="10" PersistentNotificationStorage="false"/>')


class SoapFault(Exception):
    def __init__(self, status: int, code: str, subcode: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.subcode, self.reason = status, code, subcode, reason


# ======================================================================
# HTTP handler
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenCamONVIF/" + APP_VERSION
    protocol_version = "HTTP/1.1"
    ctx: OnvifContext  # injetado

    # silencia o log padrão do http.server
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.client_address[0], fmt % args)

    # ------------------------------------------------------------ util
    @property
    def cfg(self) -> Config:
        return self.ctx.cfg

    def _host(self) -> str:
        adv = self.cfg.network.advertise_ip
        if adv and adv != "auto":
            return adv
        return self.connection.getsockname()[0]

    def base(self) -> str:
        return f"http://{self._host()}:{self.cfg.network.onvif_port}"

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _soap(self, status: int, text: str, extra: dict | None = None):
        self._send(status, text.encode("utf-8"), "application/soap+xml; charset=utf-8", extra)

    def _http_authorized(self) -> bool:
        a = self.cfg.auth
        return (not a.enabled) or auth.check_http_auth(self.headers.get("Authorization"), self.command,
                                                       a.username, a.password)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/onvif/snapshot.jpg":
            if not self._http_authorized():
                self._send(401, b"Unauthorized", "text/plain",
                           {"WWW-Authenticate": auth.digest_challenge()})
                return
            self._send(200, self.ctx.snapshot_fn(), "image/jpeg", {"Cache-Control": "no-cache"})
            return
        msg = (f"ScreenCam ONVIF {APP_VERSION}\nDevice service: {self.base()}/onvif/device_service\n"
               f"RTSP: rtsp://{self._host()}:{self.cfg.network.rtsp_port}/screen\n")
        self._send(200, msg.encode(), "text/plain; charset=utf-8")

    do_HEAD = do_GET

    # ------------------------------------------------------------ POST
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            root = ET.fromstring(raw)
            header = root.find(f"{{{X.NS['s']}}}Header")
            if header is None:  # SOAP 1.1 por algum cliente antigo
                header = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Header")
            body = root.find(f"{{{X.NS['s']}}}Body")
            if body is None:
                body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
            op = body[0]
        except Exception:
            self._soap(400, X.fault("Sender", "ter:WellFormed", "XML mal formado"))
            return
        ns, _, name = op.tag[1:].partition("}")
        msg_id = header.findtext(f"{{{NS_WSA}}}MessageID") if header is not None else None
        try:
            self._check_auth(name, header)
            fn = DISPATCH.get((ns, name))
            if fn is None:
                fn = _generic_handler(ns, name)
            if fn is None:
                log.info("Operacao ONVIF nao suportada: %s (%s)", name, ns)
                raise SoapFault(500, "Receiver", "ter:ActionNotSupported", f"{name} nao suportado")
            result = fn(self, op)
            if isinstance(result, tuple):
                body_xml, action = result
                self._soap(200, X.envelope(body_xml, action=action, relates_to=msg_id))
            else:
                self._soap(200, X.envelope(result))
            log.debug("ONVIF %s <- %s", name, self.client_address[0])
        except SoapFault as f:
            extra = {"WWW-Authenticate": auth.digest_challenge()} if f.status == 401 else None
            self._soap(f.status, X.fault(f.code, f.subcode, f.reason), extra)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Erro processando %s", name)
            self._soap(500, X.fault("Receiver", "ter:Action", "Erro interno"))

    def _check_auth(self, name: str, header):
        a = self.cfg.auth
        if not a.enabled or name in PUBLIC_OPS:
            return
        token = auth.ws_username_token(header)
        if token is not None:
            if auth.check_ws_token(token, a.username, a.password):
                return
            log.warning("Falha de autenticacao ONVIF (WS-Security) de %s", self.client_address[0])
            raise SoapFault(400, "Sender", "ter:NotAuthorized", "Usuario ou senha invalidos")
        if self.headers.get("Authorization"):
            if self._http_authorized():
                return
            log.warning("Falha de autenticacao ONVIF (HTTP) de %s", self.client_address[0])
        raise SoapFault(401, "Sender", "ter:NotAuthorized", "Autenticacao necessaria")


# ======================================================================
# Device service
# ======================================================================

def _txt(op, local: str) -> str | None:
    for el in op.iter():
        if el.tag.endswith("}" + local) or el.tag == local:
            return (el.text or "").strip()
    return None


def d_GetSystemDateAndTime(h: Handler, op):
    now = datetime.now(timezone.utc)
    local = datetime.now()
    return ("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            f"<tt:DaylightSavings>{'true' if time.localtime().tm_isdst > 0 else 'false'}</tt:DaylightSavings>"
            f"<tt:TimeZone><tt:TZ>{_posix_tz()}</tt:TZ></tt:TimeZone>"
            f"<tt:UTCDateTime>{_dt(now)}</tt:UTCDateTime><tt:LocalDateTime>{_dt(local)}</tt:LocalDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")


def d_GetDeviceInformation(h: Handler, op):
    c = h.cfg
    return ("<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{X.esc(c.device.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{X.esc(c.device.model)}</tds:Model>"
            f"<tds:FirmwareVersion>{APP_VERSION}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{h.ctx.uuid.split('-')[-1].upper()}</tds:SerialNumber>"
            f"<tds:HardwareId>{X.esc(socket.gethostname())}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>")


def d_GetCapabilities(h: Handler, op):
    b = h.base()
    return ("<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{b}/onvif/device_service</tt:XAddr>"
            "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
            "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
            f"<tt:SupportedVersions>{_version(2, 5)}</tt:SupportedVersions></tt:System>"
            "<tt:IO><tt:InputConnectors>0</tt:InputConnectors><tt:RelayOutputs>0</tt:RelayOutputs></tt:IO>"
            "<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>"
            "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration><tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>"
            "<tt:X.509Token>false</tt:X.509Token><tt:SAMLToken>false</tt:SAMLToken>"
            "<tt:KerberosToken>false</tt:KerberosToken><tt:RELToken>false</tt:RELToken></tt:Security>"
            "</tt:Device>"
            f"<tt:Events><tt:XAddr>{b}/onvif/events_service</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>"
            "</tt:Events>"
            f"<tt:Media><tt:XAddr>{b}/onvif/media_service</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>")


def d_GetServices(h: Handler, op):
    incl = (_txt(op, "IncludeCapability") or "").lower() == "true"
    b = h.base()
    out = []
    for ns, path, caps in ((NS_DEVICE, "device_service", _DEVICE_CAPS),
                           (NS_MEDIA, "media_service", _MEDIA_CAPS),
                           (NS_MEDIA2, "media2_service", _MEDIA2_CAPS),
                           (NS_EVENTS, "events_service", _EVENTS_CAPS)):
        cap = ""
        if incl:
            # dentro de tds:Capabilities o elemento de capacidade do serviço vai como filho
            cap = f"<tds:Capabilities>{caps}</tds:Capabilities>"
        out.append(f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{b}/onvif/{path}</tds:XAddr>"
                   f"{cap}<tds:Version>{_version(2, 5)}</tds:Version></tds:Service>")
    return f"<tds:GetServicesResponse>{''.join(out)}</tds:GetServicesResponse>"


def d_GetServiceCapabilities(h: Handler, op):
    return f"<tds:GetServiceCapabilitiesResponse>{_DEVICE_CAPS}</tds:GetServiceCapabilitiesResponse>"


def d_GetScopes(h: Handler, op):
    items = "".join(f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{X.esc(s)}</tt:ScopeItem>"
                    f"</tds:Scopes>" for s in scopes(h.cfg))
    return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"


def d_GetHostname(h: Handler, op):
    return ("<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP>"
            f"<tt:Name>{X.esc(socket.gethostname())}</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>")


def d_GetNetworkInterfaces(h: Handler, op):
    node = uuid.getnode()
    mac = ":".join(f"{(node >> s) & 0xff:02x}" for s in range(40, -1, -8))
    return ("<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token=\"eth0\">"
            "<tt:Enabled>true</tt:Enabled><tt:Info><tt:Name>eth0</tt:Name>"
            f"<tt:HwAddress>{mac}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>"
            "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>"
            f"<tt:Manual><tt:Address>{h._host()}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>"
            "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4>"
            "</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>")


def d_GetNetworkProtocols(h: Handler, op):
    n = h.cfg.network
    return ("<tds:GetNetworkProtocolsResponse>"
            f"<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{n.onvif_port}</tt:Port></tds:NetworkProtocols>"
            f"<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{n.rtsp_port}</tt:Port></tds:NetworkProtocols>"
            "</tds:GetNetworkProtocolsResponse>")


def d_GetDNS(h, op):
    return "<tds:GetDNSResponse><tds:DNSInformation><tt:FromDHCP>true</tt:FromDHCP></tds:DNSInformation></tds:GetDNSResponse>"


def d_GetNTP(h, op):
    return "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP></tds:NTPInformation></tds:GetNTPResponse>"


def d_GetNetworkDefaultGateway(h, op):
    return "<tds:GetNetworkDefaultGatewayResponse><tds:NetworkGateway/></tds:GetNetworkDefaultGatewayResponse>"


def d_GetDiscoveryMode(h, op):
    mode = "Discoverable" if h.cfg.network.discovery else "NonDiscoverable"
    return f"<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>{mode}</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>"


def d_GetUsers(h, op):
    return ("<tds:GetUsersResponse><tds:User>"
            f"<tt:Username>{X.esc(h.cfg.auth.username)}</tt:Username><tt:UserLevel>Administrator</tt:UserLevel>"
            "</tds:User></tds:GetUsersResponse>")


def d_GetWsdlUrl(h, op):
    return "<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/Documents/Specifications.aspx</tds:WsdlUrl></tds:GetWsdlUrlResponse>"


def d_GetEndpointReference(h, op):
    return f"<tds:GetEndpointReferenceResponse><tds:GUID>urn:uuid:{h.ctx.uuid}</tds:GUID></tds:GetEndpointReferenceResponse>"


def d_SystemReboot(h, op):
    return "<tds:SystemRebootResponse><tds:Message>Reinicio nao suportado; servico continua ativo</tds:Message></tds:SystemRebootResponse>"


# ======================================================================
# Media service
# ======================================================================

def _token(op) -> str | None:
    for local in ("ProfileToken", "ConfigurationToken"):
        t = _txt(op, local)
        if t:
            return t
    return None


def _need_profile(op):
    t = _txt(op, "ProfileToken")
    if t and t != PROFILE:
        raise SoapFault(400, "Sender", "ter:InvalidArgVal", "Perfil inexistente")


def m_GetProfiles(h, op):
    return f"<trt:GetProfilesResponse>{_profile(h.ctx, 'trt:Profiles')}</trt:GetProfilesResponse>"


def m_GetProfile(h, op):
    _need_profile(op)
    return f"<trt:GetProfileResponse>{_profile(h.ctx, 'trt:Profile')}</trt:GetProfileResponse>"


def m_GetServiceCapabilities(h, op):
    return f"<trt:GetServiceCapabilitiesResponse>{_MEDIA_CAPS}</trt:GetServiceCapabilitiesResponse>"


def m_GetVideoSources(h, op):
    g = h.ctx.geometry_fn()
    return (f'<trt:GetVideoSourcesResponse><trt:VideoSources token="{VS}">'
            f"<tt:Framerate>{h.cfg.video.fps}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{g.out_w}</tt:Width><tt:Height>{g.out_h}</tt:Height></tt:Resolution>"
            "</trt:VideoSources></trt:GetVideoSourcesResponse>")


def m_GetAudioSources(h, op):
    inner = f'<trt:AudioSources token="{AS}"><tt:Channels>2</tt:Channels></trt:AudioSources>' \
        if h.cfg.audio.enabled else ""
    return f"<trt:GetAudioSourcesResponse>{inner}</trt:GetAudioSourcesResponse>"


def _cfg_list(name: str, builder: Callable[[str], str], enabled=True):
    def list_fn(h, op):
        inner = builder(h, "trt:Configurations") if enabled is True or enabled(h) else ""
        return f"<trt:{name}Response>{inner}</trt:{name}Response>"
    return list_fn


def _cfg_single(name: str, builder):
    def single_fn(h, op):
        return f"<trt:{name}Response>{builder(h, 'trt:Configuration')}</trt:{name}Response>"
    return single_fn


_b_vsc = lambda h, tag: _vsc(h.ctx, tag)  # noqa: E731
_b_vec = lambda h, tag: _vec(h.ctx, tag)  # noqa: E731
_b_asc = lambda h, tag: _asc(tag)  # noqa: E731
_b_aec = lambda h, tag: _aec(h.ctx, tag)  # noqa: E731
_audio_on = lambda h: h.cfg.audio.enabled  # noqa: E731


def m_GetStreamUri(h, op):
    _need_profile(op)
    uri = f"rtsp://{h._host()}:{h.cfg.network.rtsp_port}/screen"
    return ("<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{uri}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>")


def m_GetSnapshotUri(h, op):
    return ("<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{h.base()}/onvif/snapshot.jpg</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>")


def m_GetVideoEncoderConfigurationOptions(h, op):
    g, v = h.ctx.geometry_fn(), h.cfg.video
    return ("<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            "<tt:QualityRange><tt:Min>0</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
            "<tt:H264>"
            f"<tt:ResolutionsAvailable><tt:Width>{g.out_w}</tt:Width><tt:Height>{g.out_h}</tt:Height></tt:ResolutionsAvailable>"
            f"<tt:GovLengthRange><tt:Min>{h.cfg.gop_frames}</tt:Min><tt:Max>{h.cfg.gop_frames}</tt:Max></tt:GovLengthRange>"
            f"<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{v.fps}</tt:Max></tt:FrameRateRange>"
            "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
            "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>"
            "</tt:H264></trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>")


def m_GetAudioEncoderConfigurationOptions(h, op):
    enc, br, sr = _audio_params(h.cfg)
    return ("<trt:GetAudioEncoderConfigurationOptionsResponse><trt:Options><tt:Options>"
            f"<tt:Encoding>{enc}</tt:Encoding><tt:BitrateList><tt:Items>{br}</tt:Items></tt:BitrateList>"
            f"<tt:SampleRateList><tt:Items>{sr}</tt:Items></tt:SampleRateList>"
            "</tt:Options></trt:Options></trt:GetAudioEncoderConfigurationOptionsResponse>")


def m_GetVideoSourceConfigurationOptions(h, op):
    g = h.ctx.geometry_fn()
    rng = lambda a, b: f"<tt:Min>{a}</tt:Min><tt:Max>{b}</tt:Max>"  # noqa: E731
    return ("<trt:GetVideoSourceConfigurationOptionsResponse><trt:Options><tt:BoundsRange>"
            f"<tt:XRange>{rng(0, 0)}</tt:XRange><tt:YRange>{rng(0, 0)}</tt:YRange>"
            f"<tt:WidthRange>{rng(g.out_w, g.out_w)}</tt:WidthRange>"
            f"<tt:HeightRange>{rng(g.out_h, g.out_h)}</tt:HeightRange></tt:BoundsRange>"
            f"<tt:VideoSourceTokensAvailable>{VS}</tt:VideoSourceTokensAvailable>"
            "</trt:Options></trt:GetVideoSourceConfigurationOptionsResponse>")


def m_GetAudioSourceConfigurationOptions(h, op):
    return ("<trt:GetAudioSourceConfigurationOptionsResponse><trt:Options>"
            f"<tt:InputTokensAvailable>{AS}</tt:InputTokensAvailable>"
            "</trt:Options></trt:GetAudioSourceConfigurationOptionsResponse>")


def m_GetGuaranteedNumberOfVideoEncoderInstances(h, op):
    return ("<trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse><trt:TotalNumber>1</trt:TotalNumber>"
            "<trt:H264>1</trt:H264></trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>")


# ======================================================================
# Media2 service (ver20) — necessário para anunciar H.265 ao NVR
# ======================================================================

def _codec_name(cfg: Config) -> str:
    return "H265" if cfg.video.codec == "h265" else "H264"


def _vec2(ctx: OnvifContext, tag: str) -> str:
    g, v = ctx.geometry_fn(), ctx.cfg.video
    cbr = "true" if v.rate_control == "cbr" else "false"
    return (f'<{tag} token="{VEC}" GovLength="{ctx.cfg.gop_frames}" Profile="Main">'
            f"<tt:Name>{VEC}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{_codec_name(ctx.cfg)}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{g.out_w}</tt:Width><tt:Height>{g.out_h}</tt:Height></tt:Resolution>"
            f'<tt:RateControl ConstantBitRate="{cbr}"><tt:FrameRateLimit>{v.fps}</tt:FrameRateLimit>'
            f"<tt:BitrateLimit>{v.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
            "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address>"
            "</tt:Address><tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            f"<tt:Quality>5</tt:Quality></{tag}>")


def _audio2_params(cfg: Config) -> tuple[str, int, int]:
    if cfg.audio.codec == "g711":
        return "PCMU", 64, 8
    return "mpeg4-generic", cfg.audio.bitrate_kbps, 48


def _aec2(ctx: OnvifContext, tag: str) -> str:
    enc, br, sr = _audio2_params(ctx.cfg)
    return (f'<{tag} token="{AEC}"><tt:Name>{AEC}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:Encoding>{enc}</tt:Encoding>"
            "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address>"
            "</tt:Address><tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            f"<tt:Bitrate>{br}</tt:Bitrate><tt:SampleRate>{sr}</tt:SampleRate></{tag}>")


_M2_TYPES = {"VideoSource", "AudioSource", "VideoEncoder", "AudioEncoder"}


def _requested_types(op) -> set[str]:
    types = set()
    for el in op:
        if el.tag.endswith("}Type") or el.tag == "Type":
            types.update((el.text or "").split())
    if not types or "All" in types:
        return set(_M2_TYPES)
    return types & _M2_TYPES


def _profile2(ctx: OnvifContext, types: set[str]) -> str:
    audio = ctx.cfg.audio.enabled
    parts = []
    if "VideoSource" in types:
        parts.append(_vsc(ctx, "tr2:VideoSource"))
    if "AudioSource" in types and audio:
        parts.append(_asc("tr2:AudioSource"))
    if "VideoEncoder" in types:
        parts.append(_vec2(ctx, "tr2:VideoEncoder"))
    if "AudioEncoder" in types and audio:
        parts.append(_aec2(ctx, "tr2:AudioEncoder"))
    conf = f"<tr2:Configurations>{''.join(parts)}</tr2:Configurations>" if parts else ""
    return f'<tr2:Profiles token="{PROFILE}" fixed="true"><tr2:Name>MainStream</tr2:Name>{conf}</tr2:Profiles>'


def m2_GetServiceCapabilities(h, op):
    return f"<tr2:GetServiceCapabilitiesResponse>{_MEDIA2_CAPS}</tr2:GetServiceCapabilitiesResponse>"


def m2_GetProfiles(h, op):
    token = _txt(op, "Token")
    if token and token != PROFILE:
        raise SoapFault(400, "Sender", "ter:InvalidArgVal", "Perfil inexistente")
    return f"<tr2:GetProfilesResponse>{_profile2(h.ctx, _requested_types(op))}</tr2:GetProfilesResponse>"


def _m2_configs(name: str, builder, needs_audio=False):
    def fn(h, op):
        _need_profile(op)
        t = _txt(op, "ConfigurationToken")
        body = "" if (needs_audio and not h.cfg.audio.enabled) else builder(h, "tr2:Configurations")
        if t and body and f'token="{t}"' not in body:
            raise SoapFault(400, "Sender", "ter:InvalidArgVal", "Configuracao inexistente")
        return f"<tr2:{name}Response>{body}</tr2:{name}Response>"
    return fn


def m2_GetStreamUri(h, op):
    _need_profile(op)
    uri = f"rtsp://{h._host()}:{h.cfg.network.rtsp_port}/screen"
    return f"<tr2:GetStreamUriResponse><tr2:Uri>{uri}</tr2:Uri></tr2:GetStreamUriResponse>"


def m2_GetSnapshotUri(h, op):
    _need_profile(op)
    return f"<tr2:GetSnapshotUriResponse><tr2:Uri>{h.base()}/onvif/snapshot.jpg</tr2:Uri></tr2:GetSnapshotUriResponse>"


def m2_GetVideoEncoderConfigurationOptions(h, op):
    g, v = h.ctx.geometry_fn(), h.cfg.video
    return ("<tr2:GetVideoEncoderConfigurationOptionsResponse>"
            f'<tr2:Options GovLengthRange="{h.cfg.gop_frames} {h.cfg.gop_frames}" FrameRatesSupported="{v.fps}" '
            f'ProfilesSupported="Main" ConstantBitRateSupported="true">'
            f"<tt:Encoding>{_codec_name(h.cfg)}</tt:Encoding>"
            "<tt:QualityRange><tt:Min>0</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
            f"<tt:ResolutionsAvailable><tt:Width>{g.out_w}</tt:Width><tt:Height>{g.out_h}</tt:Height></tt:ResolutionsAvailable>"
            f"<tt:BitrateRange><tt:Min>64</tt:Min><tt:Max>{max(64, v.bitrate_kbps)}</tt:Max></tt:BitrateRange>"
            "</tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>")


def m2_GetAudioEncoderConfigurationOptions(h, op):
    if not h.cfg.audio.enabled:
        return "<tr2:GetAudioEncoderConfigurationOptionsResponse/>"
    enc, br, sr = _audio2_params(h.cfg)
    return ("<tr2:GetAudioEncoderConfigurationOptionsResponse><tr2:Options>"
            f"<tt:Encoding>{enc}</tt:Encoding><tt:BitrateList><tt:Items>{br}</tt:Items></tt:BitrateList>"
            f"<tt:SampleRateList><tt:Items>{sr}</tt:Items></tt:SampleRateList>"
            "</tr2:Options></tr2:GetAudioEncoderConfigurationOptionsResponse>")


def m2_GetVideoSourceConfigurationOptions(h, op):
    g = h.ctx.geometry_fn()
    rng = lambda a, b: f"<tt:Min>{a}</tt:Min><tt:Max>{b}</tt:Max>"  # noqa: E731
    return ("<tr2:GetVideoSourceConfigurationOptionsResponse><tr2:Options><tt:BoundsRange>"
            f"<tt:XRange>{rng(0, 0)}</tt:XRange><tt:YRange>{rng(0, 0)}</tt:YRange>"
            f"<tt:WidthRange>{rng(g.out_w, g.out_w)}</tt:WidthRange>"
            f"<tt:HeightRange>{rng(g.out_h, g.out_h)}</tt:HeightRange></tt:BoundsRange>"
            f"<tt:VideoSourceTokensAvailable>{VS}</tt:VideoSourceTokensAvailable>"
            "</tr2:Options></tr2:GetVideoSourceConfigurationOptionsResponse>")


def m2_GetAudioSourceConfigurationOptions(h, op):
    return ("<tr2:GetAudioSourceConfigurationOptionsResponse><tr2:Options>"
            f"<tt:InputTokensAvailable>{AS}</tt:InputTokensAvailable>"
            "</tr2:Options></tr2:GetAudioSourceConfigurationOptionsResponse>")


def m2_GetVideoEncoderInstances(h, op):
    return ("<tr2:GetVideoEncoderInstancesResponse><tr2:Info>"
            f'<tr2:Codec><tr2:Encoding>{_codec_name(h.cfg)}</tr2:Encoding><tr2:Number>1</tr2:Number></tr2:Codec>'
            "<tr2:Total>1</tr2:Total></tr2:Info></tr2:GetVideoEncoderInstancesResponse>")


# ======================================================================
# Events service
# ======================================================================

def _sub_id_from_path(h: Handler) -> int | None:
    path = h.path.split("?")[0].rstrip("/")
    if "/onvif/events/sub/" in path:
        try:
            return int(path.rsplit("/", 1)[1])
        except ValueError:
            return None
    return None


def _sub_addr(h: Handler, sid: int) -> str:
    return f"{h.base()}/onvif/events/sub/{sid}"


def e_GetServiceCapabilities(h, op):
    return f"<tev:GetServiceCapabilitiesResponse>{_EVENTS_CAPS}</tev:GetServiceCapabilitiesResponse>"


def e_GetEventProperties(h, op):
    return event_properties_xml(), "http://www.onvif.org/ver10/events/wsdl/EventPortType/GetEventPropertiesResponse"


def e_CreatePullPointSubscription(h, op):
    ttl = parse_termination(_txt(op, "InitialTerminationTime"))
    sub = h.ctx.broker.create(ttl)
    return ("<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
            f"<wsa:Address>{_sub_addr(h, sub.id)}</wsa:Address></tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc_iso()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{termination_after(ttl)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>",
            "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionResponse")


def _require_sub(h) -> int:
    sid = _sub_id_from_path(h)
    if sid is None or h.ctx.broker.get(sid) is None:
        raise SoapFault(400, "Sender", "ter:InvalidArgVal", "Assinatura inexistente ou expirada")
    return sid


def e_PullMessages(h, op):
    sid = _require_sub(h)
    timeout = parse_duration(_txt(op, "Timeout"), 10.0)
    try:
        limit = max(1, int(_txt(op, "MessageLimit") or 100))
    except ValueError:
        limit = 100
    # cada evento gera 2 mensagens (dois tópicos), por isso limite // 2
    events = h.ctx.broker.pull(sid, timeout, max(1, limit // 2))
    if events is None:
        raise SoapFault(400, "Sender", "ter:InvalidArgVal", "Assinatura expirada")
    sub = h.ctx.broker.get(sid)
    remaining = (sub.expires - time.monotonic()) if sub else 0
    return ("<tev:PullMessagesResponse>"
            f"<tev:CurrentTime>{utc_iso()}</tev:CurrentTime>"
            f"<tev:TerminationTime>{termination_after(max(0, remaining))}</tev:TerminationTime>"
            f"{h.ctx.broker.messages_xml(events)}</tev:PullMessagesResponse>",
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesResponse")


def e_SetSynchronizationPoint(h, op):
    h.ctx.broker.sync(_sub_id_from_path(h))
    return ("<tev:SetSynchronizationPointResponse/>",
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/SetSynchronizationPointResponse")


def n_Renew(h, op):
    sid = _require_sub(h)
    ttl = parse_termination(_txt(op, "TerminationTime"))
    h.ctx.broker.renew(sid, ttl)
    return (f"<wsnt:RenewResponse><wsnt:TerminationTime>{termination_after(ttl)}</wsnt:TerminationTime>"
            f"<wsnt:CurrentTime>{utc_iso()}</wsnt:CurrentTime></wsnt:RenewResponse>",
            "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewResponse")


def n_Unsubscribe(h, op):
    sid = _sub_id_from_path(h)
    if sid is not None:
        h.ctx.broker.unsubscribe(sid)
    return ("<wsnt:UnsubscribeResponse/>",
            "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeResponse")


def n_Subscribe(h, op):
    consumer = None
    ref = next((el for el in op.iter() if el.tag.endswith("}ConsumerReference")), None)
    if ref is not None:
        consumer = _txt(ref, "Address")
    if not consumer:
        raise SoapFault(400, "Sender", "ter:InvalidArgVal", "ConsumerReference ausente")
    ttl = parse_termination(_txt(op, "InitialTerminationTime"))
    sub = h.ctx.broker.create(ttl, consumer=consumer)
    return ("<wsnt:SubscribeResponse><wsnt:SubscriptionReference>"
            f"<wsa:Address>{_sub_addr(h, sub.id)}</wsa:Address></wsnt:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc_iso()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{termination_after(ttl)}</wsnt:TerminationTime></wsnt:SubscribeResponse>",
            "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeResponse")


# ======================================================================
# Tabela de despacho
# ======================================================================

DISPATCH: dict[tuple[str, str], Callable] = {}


def _register(ns: str, prefix: str):
    for k, v in list(globals().items()):
        if k.startswith(prefix) and callable(v):
            DISPATCH[(ns, k[len(prefix):])] = v


_register(NS_DEVICE, "d_")
_register(NS_MEDIA, "m_")
_register(NS_MEDIA2, "m2_")
_register(NS_EVENTS, "e_")
_register(NS_WSNT, "n_")

_b_vec2 = lambda h, tag: _vec2(h.ctx, tag)  # noqa: E731
_b_aec2 = lambda h, tag: _aec2(h.ctx, tag)  # noqa: E731

DISPATCH.update({
    (NS_MEDIA2, "GetVideoSourceConfigurations"): _m2_configs("GetVideoSourceConfigurations", _b_vsc),
    (NS_MEDIA2, "GetVideoEncoderConfigurations"): _m2_configs("GetVideoEncoderConfigurations", _b_vec2),
    (NS_MEDIA2, "GetAudioSourceConfigurations"): _m2_configs("GetAudioSourceConfigurations", _b_asc, True),
    (NS_MEDIA2, "GetAudioEncoderConfigurations"): _m2_configs("GetAudioEncoderConfigurations", _b_aec2, True),
    (NS_MEDIA, "GetVideoSourceConfigurations"): _cfg_list("GetVideoSourceConfigurations", _b_vsc),
    (NS_MEDIA, "GetVideoEncoderConfigurations"): _cfg_list("GetVideoEncoderConfigurations", _b_vec),
    (NS_MEDIA, "GetAudioSourceConfigurations"): _cfg_list("GetAudioSourceConfigurations", _b_asc, _audio_on),
    (NS_MEDIA, "GetAudioEncoderConfigurations"): _cfg_list("GetAudioEncoderConfigurations", _b_aec, _audio_on),
    (NS_MEDIA, "GetCompatibleVideoSourceConfigurations"): _cfg_list("GetCompatibleVideoSourceConfigurations", _b_vsc),
    (NS_MEDIA, "GetCompatibleVideoEncoderConfigurations"): _cfg_list("GetCompatibleVideoEncoderConfigurations", _b_vec),
    (NS_MEDIA, "GetCompatibleAudioSourceConfigurations"): _cfg_list("GetCompatibleAudioSourceConfigurations", _b_asc, _audio_on),
    (NS_MEDIA, "GetCompatibleAudioEncoderConfigurations"): _cfg_list("GetCompatibleAudioEncoderConfigurations", _b_aec, _audio_on),
    (NS_MEDIA, "GetVideoSourceConfiguration"): _cfg_single("GetVideoSourceConfiguration", _b_vsc),
    (NS_MEDIA, "GetVideoEncoderConfiguration"): _cfg_single("GetVideoEncoderConfiguration", _b_vec),
    (NS_MEDIA, "GetAudioSourceConfiguration"): _cfg_single("GetAudioSourceConfiguration", _b_asc),
    (NS_MEDIA, "GetAudioEncoderConfiguration"): _cfg_single("GetAudioEncoderConfiguration", _b_aec),
})

_EMPTY_LIST_OPS = {
    NS_MEDIA: {"GetVideoAnalyticsConfigurations", "GetMetadataConfigurations", "GetAudioOutputs",
               "GetAudioOutputConfigurations", "GetAudioDecoderConfigurations",
               "GetCompatibleMetadataConfigurations", "GetCompatibleVideoAnalyticsConfigurations",
               "GetCompatibleAudioOutputConfigurations", "GetCompatibleAudioDecoderConfigurations", "GetOSDs"},
    NS_MEDIA2: {"GetMetadataConfigurations", "GetAnalyticsConfigurations", "GetAudioOutputConfigurations",
                "GetAudioDecoderConfigurations", "GetOSDs", "GetMasks", "GetVideoSourceModes"},
    NS_DEVICE: {"GetRelayOutputs", "GetDigitalInputs"},
}
_PREFIX = {NS_DEVICE: "tds", NS_MEDIA: "trt", NS_MEDIA2: "tr2"}


def _generic_handler(ns: str, name: str):
    """Respostas vazias para listas sem itens e para Set*/Add*/Remove* (aceitos e ignorados)."""
    prefix = _PREFIX.get(ns)
    if not prefix:
        return None
    if name in _EMPTY_LIST_OPS.get(ns, set()) or name.startswith(("Set", "Add", "Remove")):
        if name.startswith(("Set", "Add", "Remove")):
            log.info("Requisicao %s aceita e ignorada (configuracao fixa no config.ini)", name)
        return lambda h, op: f"<{prefix}:{name}Response/>"
    return None


# ======================================================================
# Servidor
# ======================================================================

class OnvifServer:
    def __init__(self, ctx: OnvifContext):
        self.ctx = ctx
        handler = type("BoundHandler", (Handler,), {"ctx": ctx})
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = ThreadingHTTPServer((ctx.cfg.network.bind_ip, ctx.cfg.network.onvif_port), handler)
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="onvif-http", daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self._thread.start()
        log.info("Servidor ONVIF ouvindo em %s:%s", self.ctx.cfg.network.bind_ip, self.port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
