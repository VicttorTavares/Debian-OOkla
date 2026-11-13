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
