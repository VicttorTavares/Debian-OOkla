"""Testes de integração do servidor ONVIF usando um cliente ONVIF real
(onvif-zeep-async, a mesma biblioteca usada pelo Home Assistant).

Rodam em Linux/Windows sem FFmpeg/MediaMTX: usam geometria e snapshot falsos.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
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
