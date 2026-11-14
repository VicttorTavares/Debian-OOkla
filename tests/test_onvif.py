"""Testes de integração do servidor ONVIF usando um cliente ONVIF real
(onvif-zeep-async, a mesma biblioteca usada pelo Home Assistant).

Rodam em Linux/Windows sem FFmpeg/MediaMTX: usam geometria e snapshot falsos.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from screencam.config import Config  # noqa: E402
from screencam.onvif.discovery import Discovery  # noqa: E402
from screencam.onvif.events import EventBroker, parse_duration, parse_termination  # noqa: E402
from screencam.onvif.server import VS, VSC, OnvifContext, OnvifServer, scopes  # noqa: E402
from screencam.streamer import VideoGeometry, build_ffmpeg_cmd, build_mediamtx_config  # noqa: E402

onvif = pytest.importorskip("onvif")

USER, PASS = "admin", "S3nha!forte"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def server():
    cfg = Config()
    cfg.auth.username, cfg.auth.password = USER, PASS
    cfg.network.bind_ip = "127.0.0.1"
    cfg.network.onvif_port = _free_port()
    geo = VideoGeometry(0, 0, 2560, 1440, 1920, 1080)
    broker = EventBroker(VS, VSC)
    ctx = OnvifContext(cfg, lambda: geo, lambda: b"\xff\xd8fakejpeg\xff\xd9", broker)
    srv = OnvifServer(ctx)
    srv.start()
    yield srv
    srv.stop()


def _cam(srv, user=USER, pw=PASS):
    from onvif import ONVIFCamera
    return ONVIFCamera("127.0.0.1", srv.port, user, pw)


def test_device_media_profile(server):
    async def run():
        cam = _cam(server)
        await cam.update_xaddrs()
        dev = await cam.create_devicemgmt_service()
        info = await dev.GetDeviceInformation()
        assert info.Manufacturer == "ScreenCam"
        media = await cam.create_media_service()
        profiles = await media.GetProfiles()
        assert len(profiles) == 1
        p = profiles[0]
        assert p.VideoEncoderConfiguration.Encoding == "H264"
        assert p.VideoEncoderConfiguration.Resolution.Width == 1920
        assert p.AudioEncoderConfiguration.Encoding == "AAC"
        uri = await media.GetStreamUri({"ProfileToken": p.token,
                                        "StreamSetup": {"Stream": "RTP-Unicast",
                                                        "Transport": {"Protocol": "RTSP"}}})
        assert uri.Uri == f"rtsp://127.0.0.1:{server.ctx.cfg.network.rtsp_port}/screen"
        snap = await media.GetSnapshotUri({"ProfileToken": p.token})
        assert snap.Uri.endswith("/onvif/snapshot.jpg")
        for op in ("GetVideoSources", "GetAudioSources", "GetVideoEncoderConfigurations",
                   "GetAudioEncoderConfigurations", "GetVideoSourceConfigurations"):
            assert await getattr(media, op)() is not None
        opts = await media.GetVideoEncoderConfigurationOptions({"ProfileToken": p.token})
        assert opts.H264.ResolutionsAvailable[0].Width == 1920
        await dev.GetScopes()
        await dev.GetNetworkInterfaces()
        await dev.GetHostname()
        caps = await dev.GetCapabilities({"Category": "All"})
        assert caps.Events.WSPullPointSupport is True
        await cam.close()
    asyncio.run(run())


def test_wrong_password_rejected(server):
    async def run():
        cam = _cam(server, pw="errada")
        await cam.update_xaddrs()  # operações pré-auth funcionam
        dev = await cam.create_devicemgmt_service()
        with pytest.raises(Exception):
            await dev.GetDeviceInformation()
        await cam.close()
    asyncio.run(run())


def test_pullpoint_motion_events(server):
    async def run():
        cam = _cam(server)
        await cam.update_xaddrs()
        events = await cam.create_events_service()
        props = await events.GetEventProperties()
        assert props.TopicSet is not None
        mgr = await cam.create_pullpoint_manager(dt.timedelta(seconds=60), lambda: None)
        svc = mgr.get_service()
        # primeiro pull: estado inicial (sem movimento)
        r = await svc.PullMessages({"MessageLimit": 10, "Timeout": dt.timedelta(seconds=1)})
        assert len(r.NotificationMessage) == 2
        # usuário mexe o mouse -> evento de movimento
        threading.Timer(0.3, lambda: server.ctx.broker.set_motion(True)).start()
        t0 = time.monotonic()
        r = await svc.PullMessages({"MessageLimit": 10, "Timeout": dt.timedelta(seconds=5)})
        assert time.monotonic() - t0 < 3, "long-poll deveria retornar ao chegar o evento"
        topics = {m.Topic._value_1 for m in r.NotificationMessage}
        assert topics == {"tns1:RuleEngine/CellMotionDetector/Motion", "tns1:VideoSource/MotionAlarm"}
        data = r.NotificationMessage[0].Message._value_1.Data.SimpleItem[0]
        assert data.Name == "IsMotion" and data.Value == "true"
        await mgr.shutdown()
        await cam.close()
    asyncio.run(run())


def test_snapshot_http_digest_and_basic(server):
    url = f"http://127.0.0.1:{server.port}/onvif/snapshot.jpg"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url)
    assert e.value.code == 401 and "Digest" in e.value.headers["WWW-Authenticate"]
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, USER, PASS)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    assert opener.open(url).read().startswith(b"\xff\xd8")
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()})
    assert urllib.request.urlopen(req).status == 200


def test_unknown_op_fault(server):
    body = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            b'<s:Body><GetFoo xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')
    nonce = os.urandom(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASS.encode()).digest()).decode()
    sec = (f'<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
           f'<UsernameToken><Username>{USER}</Username><Password Type="http://docs.oasis-open.org/wss/2004/01/'
           f'oasis-200401-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
           f'<Nonce>{base64.b64encode(nonce).decode()}</Nonce>'
           f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
           f'{created}</Created></UsernameToken></Security></s:Header>').encode()
    body = body.replace(b"<s:Body>", sec + b"<s:Body>")
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/onvif/device_service", data=body,
                                 headers={"Content-Type": "application/soap+xml"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 500 and b"ActionNotSupported" in e.value.read()


def test_discovery_probe_match():
    d = Discovery("1234", 8000, scopes(Config()))
    probe = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
             b'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
             b'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             b'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header>'
             b'<a:MessageID>uuid:abc</a:MessageID></s:Header><s:Body><d:Probe>'
             b'<d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>')
    assert d.wants_us(probe) == "uuid:abc"
    assert d.wants_us(probe.replace(b"dn:NetworkVideoTransmitter", b"x:Printer")) is None
    resp = d.probe_match("uuid:abc", "127.0.0.1").decode()
    assert "ProbeMatches" in resp and "http://127.0.0.1:8000/onvif/device_service" in resp
    import xml.etree.ElementTree as ET
    ET.fromstring(resp)


def test_durations():
    assert parse_duration("PT5S", 0) == 5
    assert parse_duration("PT1M30S", 0) == 90
    assert parse_duration("P1DT1H", 0) == 90000
    assert 55 < parse_termination("PT60S") <= 60
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert 100 < parse_termination(future) <= 120


def test_ffmpeg_and_mediamtx_config():
    cfg = Config()
    cfg.auth.password = "it's"
    geo = VideoGeometry(-1920, 0, 1920, 1080, 1920, 1080)
    cmd = build_ffmpeg_cmd(cfg, geo, "rtsp://127.0.0.1:8554/screen")
    assert cmd[cmd.index("-offset_x") + 1] == "-1920"
    assert "pipe:0" in cmd and "aac" in cmd
    cfg.audio.codec = "g711"
    assert "pcm_mulaw" in build_ffmpeg_cmd(cfg, geo, "x")
    yml = build_mediamtx_config(cfg)
    assert "pass: 'it''s'" in yml and "rtspAddress: :8554" in yml


def test_activity_monitor_idle_cycle():
    from screencam.activity import ActivityMonitor
    tick = [0]
    changes = []
    mon = ActivityMonitor(0.3, 0.05, changes.append, input_source=lambda: tick[0])
    mon.start()
    time.sleep(0.15)
    tick[0] += 1  # "mouse mexeu"
    time.sleep(0.15)
    assert changes == [True]
    time.sleep(0.5)
    assert changes == [True, False]
    mon.stop()


# ---------------------------------------------------------------- H.265 / Media2

def _soap(port, ns, body_xml, path="/onvif/media2_service"):
    import xml.etree.ElementTree as ET
    nonce = os.urandom(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASS.encode()).digest()).decode()
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:m="{ns}">'
           f'<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
           f'<UsernameToken><Username>{USER}</Username><Password Type="http://docs.oasis-open.org/wss/2004/01/'
           f'oasis-200401-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
           f'<Nonce>{base64.b64encode(nonce).decode()}</Nonce>'
           f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
           f'{created}</Created></UsernameToken></Security></s:Header><s:Body>{body_xml}</s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=env.encode(),
                                 headers={"Content-Type": "application/soap+xml"})
    return ET.fromstring(urllib.request.urlopen(req).read())


TR2 = "http://www.onvif.org/ver20/media/wsdl"
TT = "{http://www.onvif.org/ver10/schema}"


def test_media2_reports_h265(server):
    server.ctx.cfg.video.codec = "h265"
    root = _soap(server.port, TR2, "<m:GetProfiles><m:Type>All</m:Type></m:GetProfiles>")
    enc = root.find(f".//{{{TR2}}}VideoEncoder")
    assert enc.findtext(f"{TT}Encoding") == "H265"
    assert enc.get("GovLength") == str(server.ctx.cfg.gop_frames)
    assert enc.find(f"{TT}Resolution/{TT}Width").text == "1920"
    assert root.find(f".//{{{TR2}}}AudioEncoder/{TT}Encoding").text == "mpeg4-generic"
    # filtro por tipo
    root = _soap(server.port, TR2, "<m:GetProfiles><m:Type>VideoEncoder</m:Type></m:GetProfiles>")
    assert root.find(f".//{{{TR2}}}VideoSource") is None and root.find(f".//{{{TR2}}}VideoEncoder") is not None
    uri = _soap(server.port, TR2, "<m:GetStreamUri><m:Protocol>RtspUnicast</m:Protocol>"
                                  "<m:ProfileToken>Profile_1</m:ProfileToken></m:GetStreamUri>")
    assert uri.findtext(f".//{{{TR2}}}Uri").endswith(":8554/screen")
    opts = _soap(server.port, TR2, "<m:GetVideoEncoderConfigurationOptions/>")
    assert opts.findtext(f".//{TT}Encoding") == "H265"
    for op in ("GetVideoEncoderConfigurations", "GetVideoSourceConfigurations", "GetAudioEncoderConfigurations",
               "GetAudioSourceConfigurations", "GetServiceCapabilities", "GetSnapshotUri",
               "GetVideoEncoderInstances", "GetAudioEncoderConfigurationOptions", "GetMetadataConfigurations"):
        _soap(server.port, TR2, f"<m:{op}/>")
    # Media1 continua respondendo (NVRs antigos), com valor válido no esquema ver10
    m1 = _soap(server.port, "http://www.onvif.org/ver10/media/wsdl", "<m:GetProfiles/>", "/onvif/media_service")
    assert m1.find(f".//{TT}VideoEncoderConfiguration/{TT}Encoding").text == "H264"


def test_get_services_lists_media2(server):
    async def run():
        cam = _cam(server)
        dev = await _devmgmt(cam)
        services = await dev.GetServices({"IncludeCapability": False})
        assert TR2 in {s.Namespace for s in services}
        await cam.close()

    async def _devmgmt(cam):
        await cam.update_xaddrs()
        return await cam.create_devicemgmt_service()
    asyncio.run(run())


def test_scopes_profile_t_only_with_h265():
    cfg = Config()
    cfg.video.codec = "h265"
    assert "onvif://www.onvif.org/Profile/T" in scopes(cfg)
    cfg.video.codec = "h264"
    assert "onvif://www.onvif.org/Profile/T" not in scopes(cfg)


def test_encoder_resolution_and_fallback(monkeypatch):
    from screencam import encoders, winapi
    monkeypatch.setattr(winapi, "IS_WINDOWS", True)
    # GPU NVIDIA disponível -> hevc_nvenc
    cfg = Config()
    assert encoders.resolve_encoder(cfg, "ffmpeg", probe=lambda f, n: n in ("hevc_nvenc", "libx265")) == "hevc_nvenc"
    assert (cfg.video.codec, cfg.video.encoder) == ("h265", "nvenc")
    # sem GPU -> x265 na CPU
    cfg = Config()
    assert encoders.resolve_encoder(cfg, "ffmpeg", probe=lambda f, n: n.startswith("lib")) == "libx265"
    # FFmpeg sem HEVC -> cai para H.264 e o ONVIF passa a informar H264
    cfg = Config()
    assert encoders.resolve_encoder(cfg, "ffmpeg", probe=lambda f, n: n == "libx264") == "libx264"
    assert cfg.video.codec == "h264"
    assert "onvif://www.onvif.org/Profile/T" not in scopes(cfg)


def test_encoder_args_h265():
    from screencam.config import _validate
    from screencam.encoders import encoder_args
    cfg = Config()
    _validate(cfg)
    cfg.video.encoder = "cpu"
    a = encoder_args(cfg)
    params = a[a.index("-x265-params") + 1]
    assert a[a.index("-c:v") + 1] == "libx265"
    assert "keyint=60" in params and "crf=28" in params and "repeat-headers=1" in params
    cfg.video.encoder, cfg.video.rate_control = "nvenc", "cbr"
    a = encoder_args(cfg)
    assert a[a.index("-c:v") + 1] == "hevc_nvenc" and a[a.index("-rc") + 1] == "cbr"
    # compatibilidade com config.ini da v1.0
    old = Config()
    old.video.encoder, old.video.codec = "h264_nvenc", "H.264"
    _validate(old)
    assert (old.video.codec, old.video.encoder) == ("h264", "nvenc")


# ---------------------------------------------------------------- serviço/agente

def test_agent_payload_has_no_secrets():
    from screencam.config import agent_payload, config_from_agent_payload
    cfg = Config()
    cfg.auth.username, cfg.auth.password = "admin", "senha-secreta-123"
    cfg.video.codec, cfg.video.encoder = "h265", "nvenc"
    cfg.network.rtsp_port = 9999
    payload = agent_payload(cfg)
    blob = json.dumps(payload)
    assert "senha-secreta-123" not in blob and "auth" not in payload and "network" not in payload
    assert payload["rtsp_port"] == 9999
    agent_cfg = config_from_agent_payload(payload)
    assert agent_cfg.video.codec == "h265" and agent_cfg.video.encoder == "nvenc"
    assert agent_cfg.network.rtsp_port == 9999
    assert agent_cfg.auth.enabled is False  # agente nunca autentica
    assert agent_cfg.recording.enabled is False and agent_cfg.network.discovery is False


def test_control_channel_aggregates_sessions():
    from screencam.control import ActivityAggregator, ControlClient, ControlServer
    changes = []
    agg = ActivityAggregator(changes.append)
    srv = ControlServer(0, "tok", agg, status_fn=lambda: {"ok": 1})
    srv.start()
    try:
        ep = f"http://127.0.0.1:{srv.port}"
        a = ControlClient(ep, "tok", "s1")
        b = ControlClient(ep, "tok", "s2")
        assert a.report_activity(True)
        time.sleep(0.05)
        assert changes == [True]
        assert b.report_activity(True)
        assert a.report_activity(False)
        time.sleep(0.05)
        assert changes == [True]  # s2 ainda ativo
        assert b.report_activity(False)
        time.sleep(0.05)
        assert changes == [True, False]
        assert set(agg.active_sessions()) == set()
        # token inválido é rejeitado
        assert ControlClient(ep, "errado", "s3").report_activity(True) is False
    finally:
        srv.stop()


def test_control_session_ttl_sweep():
    from screencam.control import ActivityAggregator
    changes = []
    clock = [1000.0]
    agg = ActivityAggregator(changes.append)
    # injeta heartbeat antigo manualmente
    agg._sessions["morta"] = (True, 0.0)
    agg._active = True
    agg.sweep()  # time.monotonic() >> 0 + TTL -> remove e recomputa
    assert changes == [False]
    assert agg.active_sessions() == []


def test_session_manager_active_console_follows_user():
    from screencam.sessions import SessionInfo, SessionManager

    class FakeAgent:
        def __init__(self):
            self.alive = True
            self.killed = False

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            self.killed = True

    class Backend:
        def __init__(self):
            self.sessions = []
            self.launched = []

        def list_sessions(self):
            return list(self.sessions)

        def launch_agent(self, sid):
            a = FakeAgent()
            self.launched.append((sid, a))
            return a

    clock = [0.0]
    b = Backend()
    m = SessionManager(b, "active_console", respawn_backoff=3.0, clock=lambda: clock[0])

    b.sessions = [SessionInfo(1, "joao", True, True)]
    m.reconcile()
    assert set(m._agents) == {1}

    # troca de usuário: console passa para a sessão 2
    b.sessions = [SessionInfo(1, "joao", False, True), SessionInfo(2, "maria", True, True)]
    m.reconcile()
    assert set(m._agents) == {2}
    assert b.launched[0][1].killed is True

    # usuário mata o agente -> só respawna após o backoff
    m._agents[2].alive = False
    m.reconcile()
    assert len([s for s, _ in b.launched if s == 2]) == 1
    clock[0] = 10.0
    m.reconcile()
    assert len([s for s, _ in b.launched if s == 2]) == 2
    assert m._agents[2].is_alive()


def test_session_manager_single_shared_channel():
    # Canal único: mesmo com várias sessões RDP ativas, apenas UM agente captura.
    from screencam.sessions import SessionInfo, SessionManager

    class A:
        def is_alive(self):
            return True

        def terminate(self):
            pass

    class Backend:
        def __init__(self):
            self.sessions = []

        def list_sessions(self):
            return list(self.sessions)

        def launch_agent(self, sid):
            return A()

    b = Backend()
    m = SessionManager(b, "all_sessions")
    # duas RDP ativas (sem console) + uma sem usuário -> escolhe a de menor ID (3)
    b.sessions = [SessionInfo(4, "b", True, False), SessionInfo(3, "a", True, False),
                  SessionInfo(5, "", True, True)]
    m.reconcile()
    assert set(m._agents) == {3}
    # console físico aparece -> tem prioridade sobre as RDP
    b.sessions.append(SessionInfo(1, "console", True, True))
    m.reconcile()
    assert set(m._agents) == {1}


def test_server_core_status_and_pause():
    cfg = Config()
    cfg.network.onvif_port = _free_port()
    cfg.network.rtsp_port = _free_port()
    cfg.network.discovery = False
    cfg.service.control_port = 0
    from screencam.app import ServerCore
    core = ServerCore(cfg)
    try:
        # simula atividade reportada por uma sessão
        core.aggregator.report("s1", True)
        time.sleep(0.02)
        assert core.broker.motion is True
        st = core.status()
        assert st["motion"] is True and st["active_sessions"] == ["s1"]
        # pausa derruba o movimento mesmo com sessão ativa
        core.set_paused(True)
        assert core.broker.motion is False
        core.set_paused(False)
        assert core.broker.motion is True
    finally:
        core.broker.set_motion(False)


# ---------------------------------------------------------------- DPAPI / senha

class FakeCipher:
    """Simula a DPAPI: 'cifra' = base64 invertido ligado a uma maquina."""
    def __init__(self, machine="pc-1", available=True):
        self.machine = machine
        self._avail = available

    def is_available(self):
        return self._avail

    def protect(self, plaintext, entropy=b""):
        raw = f"{self.machine}|{plaintext}".encode()
        return base64.b64encode(raw).decode()

    def unprotect(self, b64, entropy=b""):
        machine, _, text = base64.b64decode(b64).decode().partition("|")
        if machine != self.machine:
            raise ValueError("blob de outra maquina")
        return text


def test_password_encode_decode_roundtrip():
    from screencam.config import PW_PREFIX, decode_password, encode_password, is_encrypted
    c = FakeCipher()
    enc = encode_password("S3nha!forte", cipher=c)
    assert enc.startswith(PW_PREFIX) and "S3nha" not in enc
    assert is_encrypted(enc) and not is_encrypted("texto")
    assert decode_password(enc, cipher=c) == "S3nha!forte"
    assert decode_password("texto-claro", cipher=c) == "texto-claro"  # sem prefixo = mantem


def test_password_from_other_machine_fails_safe():
    from screencam.config import decode_password, encode_password
    enc = encode_password("abc", cipher=FakeCipher(machine="pc-origem"))
    # outro PC nao decifra -> senha vazia (nao vaza, nao autentica)
    assert decode_password(enc, cipher=FakeCipher(machine="pc-destino")) == ""


def test_protect_config_file_preserves_comments(tmp_path):
    from screencam.config import is_encrypted, protect_config_file
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[auth]\n"
        "; comentario importante\n"
        "enabled = true\n"
        "username = admin\n"
        "password = MinhaSenha123   ; senha do NVR\n"
        "\n"
        "[video]\n"
        "codec = h265\n", encoding="utf-8")
    c = FakeCipher()
    assert protect_config_file(cfg, cipher=c) is True
    text = cfg.read_text(encoding="utf-8")
    assert "; comentario importante" in text and "[video]" in text and "codec = h265" in text
    assert "MinhaSenha123" not in text
    # a linha da senha agora esta cifrada e e relida corretamente
    pw_line = [l for l in text.splitlines() if l.startswith("password")][0]
    value = pw_line.split("=", 1)[1].strip()
    assert is_encrypted(value)
    # idempotente: rodar de novo nao altera
    assert protect_config_file(cfg, cipher=c) is False


def test_protect_config_noop_when_unavailable(tmp_path):
    from screencam.config import protect_config_file
    cfg = tmp_path / "config.ini"
    cfg.write_text("[auth]\npassword = claro\n", encoding="utf-8")
    assert protect_config_file(cfg, cipher=FakeCipher(available=False)) is False
    assert "claro" in cfg.read_text(encoding="utf-8")  # inalterado


# ---------------------------------------------------------------- IP do NVR

def test_mediamtx_restricts_read_to_nvr_ip():
    from screencam.streamer import build_mediamtx_config
    cfg = Config()
    cfg.auth.username, cfg.auth.password = "admin", "DezCarac10"
    cfg.network.nvr_ip = "192.168.1.50"
    yml = build_mediamtx_config(cfg)
    # o usuario de leitura (NVR) so e aceito a partir do IP do NVR
    assert "ips: ['192.168.1.50']" in yml
    # localhost continua liberado (recorder local)
    assert "ips: ['127.0.0.1', '::1']" in yml
    # sem nvr_ip -> leitura de qualquer IP
    cfg.network.nvr_ip = ""
    assert "ips: []" in build_mediamtx_config(cfg)


def test_invalid_nvr_ip_is_cleared():
    from screencam.config import _validate
    cfg = Config()
    cfg.network.nvr_ip = "nao-e-um-ip"
    _validate(cfg)
    assert cfg.network.nvr_ip == ""
    cfg.network.nvr_ip = "10.0.0.7"
    _validate(cfg)
    assert cfg.network.nvr_ip == "10.0.0.7"


# ---------------------------------------------------------------- registro ativo no NVR

def test_autoregister_tunnel_bridges_both_directions():
    import socket
    import threading
    import time
    from screencam.autoregister import bridge, build_preamble

    captured = {}

    def fake_nvr(listener):
        conn, _ = listener.accept()
        conn.settimeout(3)
        captured["preamble"] = conn.recv(100)
        conn.sendall(b"GET /onvif/device_service HTTP/1.0\r\n\r\n")
        captured["reply"] = conn.recv(100)
        conn.close()

    def fake_local(listener):
        conn, _ = listener.accept()
        conn.settimeout(3)
        captured["local_req"] = conn.recv(100)
        conn.sendall(b"HTTP/1.0 200 OK\r\nONVIF-OK\r\n")
        conn.close()

    nvr_l = socket.socket(); nvr_l.bind(("127.0.0.1", 0)); nvr_l.listen(1)
    loc_l = socket.socket(); loc_l.bind(("127.0.0.1", 0)); loc_l.listen(1)
    threading.Thread(target=fake_nvr, args=(nvr_l,), daemon=True).start()
    threading.Thread(target=fake_local, args=(loc_l,), daemon=True).start()

    nvr_sock = socket.create_connection(("127.0.0.1", nvr_l.getsockname()[1]))
    bridge(nvr_sock, ("127.0.0.1", loc_l.getsockname()[1]), preamble=build_preamble("7"))
    time.sleep(0.2)
    assert captured["preamble"] == b"deviceID:7\r\n"
    assert b"device_service" in captured["local_req"]
    assert b"ONVIF-OK" in captured["reply"]


def test_build_preamble_empty_when_no_device_id():
    from screencam.autoregister import build_preamble
    assert build_preamble("") == b""
    assert build_preamble("  ") == b""
    assert build_preamble("5") == b"deviceID:5\r\n"


def test_autoregister_disabled_when_ip_invalid():
    from screencam.config import Config, _validate
    cfg = Config()
    cfg.autoregister.enabled = True
    cfg.autoregister.server_ip = "sem-ip"
    _validate(cfg)
    assert cfg.autoregister.enabled is False


def test_autoregister_server_ip_fills_nvr_ip():
    from screencam.config import Config, _validate
    cfg = Config()
    cfg.autoregister.server_ip = "192.168.25.200"
    _validate(cfg)
    assert cfg.network.nvr_ip == "192.168.25.200"  # mesmo NVR para leitura/firewall


def test_dpapi_protects_both_passwords(tmp_path):
    from screencam.config import protect_config_file, is_encrypted
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[auth]\npassword = CamSenha123\n\n"
        "[autoregister]\nserver_ip = 192.168.0.9\npassword = NvrSenha456\n",
        encoding="utf-8")
    c = FakeCipher()
    assert protect_config_file(cfg, cipher=c) is True
    text = cfg.read_text(encoding="utf-8")
    assert "CamSenha123" not in text and "NvrSenha456" not in text
    lines = [l for l in text.splitlines() if l.startswith("password")]
    assert len(lines) == 2 and all(is_encrypted(l.split("=", 1)[1].strip()) for l in lines)
    assert protect_config_file(cfg, cipher=c) is False  # idempotente


def test_same_subnet24():
    from screencam.config import same_subnet24
    assert same_subnet24("192.168.25.197", "192.168.25.200") is True
    assert same_subnet24("192.168.26.197", "192.168.25.200") is False  # erro de digitacao 26 vs 25
    assert same_subnet24("10.0.0.5", "10.0.1.5") is False
    assert same_subnet24("lixo", "192.168.25.1") is False


def test_servercore_warns_on_wrong_subnet_nvr(caplog):
    import logging as _logging
    cfg = Config()
    cfg.network.onvif_port = _free_port()
    cfg.network.rtsp_port = _free_port()
    cfg.network.discovery = False
    cfg.service.control_port = 0
    cfg.network.nvr_ip = "8.8.8.8"  # claramente fora da sub-rede local
    from screencam.app import ServerCore
    core = ServerCore(cfg)
    with caplog.at_level(_logging.WARNING):
        core._diagnose_network()
    assert any("NAO esta na mesma sub-rede" in r.message for r in caplog.records)


def test_geometry_survives_session0_without_monitor(monkeypatch):
    """Servico na sessao 0 do Windows nao tem monitor (0x0): nao pode quebrar o inicio."""
    from screencam import winapi
    from screencam.streamer import compute_geometry
    monkeypatch.setattr(winapi, "capture_rect", lambda monitor: winapi.Rect(0, 0, 0, 0))
    g = compute_geometry(Config())
    assert (g.out_w, g.out_h) == (1920, 1080)

    def boom(monitor):
        raise OSError("sem desktop")
    monkeypatch.setattr(winapi, "capture_rect", boom)
    assert compute_geometry(Config()).out_w == 1920


def test_server_core_builds_without_monitor(monkeypatch):
    from screencam import winapi
    from screencam.app import ServerCore
    monkeypatch.setattr(winapi, "capture_rect", lambda monitor: winapi.Rect(0, 0, 0, 0))
    cfg = Config()
    cfg.network.onvif_port = _free_port()
    cfg.network.rtsp_port = _free_port()
    cfg.network.discovery = False
    cfg.service.control_port = 0
    ServerCore(cfg)  # antes: ZeroDivisionError


def test_doctor_runs_and_reports(tmp_path, capsys):
    from screencam.doctor import run_doctor
    ini = tmp_path / "config.ini"
    ini.write_text("[auth]\nusername = admin\npassword = Curta1\n[network]\nnvr_ip = 192.168.25.200\n"
                   f"onvif_port = {_free_port()}\nrtsp_port = {_free_port()}\n", encoding="utf-8")
    run_doctor(str(ini))
    out = capsys.readouterr().out
    assert "diagnostico" in out and "[AVISO] Senha" in out  # senha < 10 e sinalizada
    assert "Porta ONVIF" in out and "resumo" in out


def test_servercore_falls_back_when_onvif_port_busy(tmp_path, monkeypatch):
    """Reproduz o Windows com IIS/http.sys na porta 80: o servico NAO pode morrer."""
    import socket
    from screencam import app as app_mod
    monkeypatch.setattr(app_mod, "program_data_dir", lambda: tmp_path)
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]
    try:
        cfg = Config()
        cfg.network.onvif_port = busy_port           # "porta 80" ocupada
        cfg.network.rtsp_port = _free_port()
        cfg.network.discovery = False
        cfg.service.control_port = 0
        core = app_mod.ServerCore(cfg)               # antes: PermissionError/OSError derrubava o servico
        assert core.cfg.network.onvif_port != busy_port
        assert core.wanted_ports["onvif"] == busy_port
        core.write_status_file()
        import json
        st = json.loads((tmp_path / "runtime" / "status.json").read_text(encoding="utf-8"))
        assert st["onvif_port"] == core.cfg.network.onvif_port and st["wanted_ports"]["onvif"] == busy_port
        core.onvif.stop()
    finally:
        busy.close()


def test_servercore_falls_back_when_rtsp_port_busy():
    import socket
    from screencam.app import ServerCore
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    try:
        cfg = Config()
        cfg.network.rtsp_port = busy.getsockname()[1]
        cfg.network.onvif_port = _free_port()
        cfg.network.discovery = False
        cfg.service.control_port = 0
        core = ServerCore(cfg)
        assert core.cfg.network.rtsp_port != busy.getsockname()[1]
        core.onvif.stop()
    finally:
        busy.close()


def test_pick_port_and_port_free():
    import socket
    from screencam.netutil import pick_port, port_free
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    taken = s.getsockname()[1]
    assert port_free(taken) is False
    assert pick_port(taken, (_free_port(),)) != taken
    s.close()


def test_session_manager_exponential_backoff_and_exit_code(caplog):
    import logging as _logging
    from screencam.sessions import SessionInfo, SessionManager

    class Dying:
        exit_code = 0xC0000142

        def is_alive(self):
            return False

        def terminate(self):
            pass

    class Backend:
        def __init__(self):
            self.launches = 0

        def list_sessions(self):
            return [SessionInfo(2, "u", True, True)]

        def launch_agent(self, sid):
            self.launches += 1
            return Dying()

    clock = [0.0]
    b = Backend()
    m = SessionManager(b, respawn_backoff=3.0, clock=lambda: clock[0])
    with caplog.at_level(_logging.WARNING):
        for t in (0, 1, 3.1, 4, 9.2, 10, 21.5):   # morre sempre na hora
            clock[0] = t
            m.reconcile()
    # backoff dobra (3s, 6s, 12s...): bem menos lancamentos que um a cada ciclo
    assert b.launches == 3, b.launches
    assert any("0xC0000142" in r.message for r in caplog.records)
