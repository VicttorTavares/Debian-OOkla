"""Serviço de eventos ONVIF: a atividade do usuário vira "detecção de movimento".

Suporta PullPoint (CreatePullPointSubscription/PullMessages — usado pela
maioria dos NVRs) e Base Notification (Subscribe + Notify via HTTP POST).
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import xml as X

log = logging.getLogger(__name__)

DEFAULT_TTL = 60.0
MAX_QUEUE = 100


def utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: str | None, default: float) -> float:
    if not text:
        return default
    text = text.strip()
    m = _DUR.match(text)
    if m and text != "P" and text != "PT":
        d, h, mi, s = m.groups()
        return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    return default


def parse_termination(text: str | None, default: float = DEFAULT_TTL) -> float:
    """Aceita duração (PT60S) ou data absoluta; retorna segundos a partir de agora."""
    if not text:
        return default
    text = text.strip()
    if text.startswith("P"):
        return max(10.0, parse_duration(text, default))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(10.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return default


@dataclass
class Event:
    utc: float
    motion: bool
    operation: str  # Initialized | Changed


@dataclass
class Subscription:
    id: int
    expires: float
    consumer: str | None = None  # push (Base Notification)
    queue: list[Event] = field(default_factory=list)


class EventBroker:
    def __init__(self, video_source_token: str, vsc_token: str):
        self.vs_token = video_source_token
        self.vsc_token = vsc_token
        self.motion = False
        self._subs: dict[int, Subscription] = {}
        self._ids = itertools.count(1)
        self._cond = threading.Condition()

    # ------------------------------------------------------------- estado
    def set_motion(self, active: bool) -> None:
        with self._cond:
            if active == self.motion:
                return
            self.motion = active
            ev = Event(time.time(), active, "Changed")
            push = []
            for sub in self._subs.values():
                if sub.consumer:
                    push.append(sub)
                else:
                    sub.queue.append(ev)
                    del sub.queue[:-MAX_QUEUE]
            self._cond.notify_all()
        for sub in push:
            threading.Thread(target=self._notify, args=(sub, [ev]), daemon=True).start()

    # ------------------------------------------------------- assinaturas
    def _cleanup(self) -> None:
        now = time.monotonic()
        for sid in [s.id for s in self._subs.values() if s.expires < now]:
            del self._subs[sid]

    def create(self, ttl: float, consumer: str | None = None) -> Subscription:
        with self._cond:
            self._cleanup()
            sub = Subscription(next(self._ids), time.monotonic() + ttl, consumer)
            initial = Event(time.time(), self.motion, "Initialized")
            if not consumer:
                sub.queue.append(initial)
            self._subs[sub.id] = sub
        if consumer:
            threading.Thread(target=self._notify, args=(sub, [initial]), daemon=True).start()
        log.info("Nova assinatura de eventos #%s (%s)", sub.id, consumer or "pull-point")
        return sub

    def get(self, sid: int) -> Subscription | None:
        with self._cond:
            self._cleanup()
            return self._subs.get(sid)

    def renew(self, sid: int, ttl: float) -> Subscription | None:
        with self._cond:
            sub = self._subs.get(sid)
            if sub:
                sub.expires = time.monotonic() + ttl
            return sub

    def unsubscribe(self, sid: int) -> bool:
        with self._cond:
            return self._subs.pop(sid, None) is not None

    def sync(self, sid: int | None) -> None:
        """SetSynchronizationPoint: reenvia o estado atual."""
        with self._cond:
            subs = [self._subs[sid]] if sid in self._subs else list(self._subs.values())
            ev = Event(time.time(), self.motion, "Initialized")
            for s in subs:
                if not s.consumer:
                    s.queue.append(ev)
            self._cond.notify_all()

    def pull(self, sid: int, timeout: float, limit: int) -> list[Event] | None:
        deadline = time.monotonic() + min(timeout, 60)
        with self._cond:
            while True:
                sub = self._subs.get(sid)
                if sub is None:
                    return None
                if sub.queue:
                    out, sub.queue[:] = sub.queue[:limit], sub.queue[limit:]
                    return out
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    # ------------------------------------------------------------ XML
    def messages_xml(self, events: list[Event], sub_ref: str | None = None) -> str:
        parts = []
        for ev in events:
            t = utc_iso(ev.utc)
            val = "true" if ev.motion else "false"
            ref = (f"<wsnt:SubscriptionReference><wsa:Address>{X.esc(sub_ref)}</wsa:Address>"
                   f"</wsnt:SubscriptionReference>") if sub_ref else ""
            parts.append(
                f"<wsnt:NotificationMessage>{ref}"
                f'<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
                f"tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>"
                f'<wsnt:Message><tt:Message UtcTime="{t}" PropertyOperation="{ev.operation}">'
                f"<tt:Source>"
                f'<tt:SimpleItem Name="VideoSourceConfigurationToken" Value="{self.vsc_token}"/>'
                f'<tt:SimpleItem Name="VideoAnalyticsConfigurationToken" Value="VAC_1"/>'
                f'<tt:SimpleItem Name="Rule" Value="MyMotionDetectorRule"/>'
                f"</tt:Source>"
                f'<tt:Data><tt:SimpleItem Name="IsMotion" Value="{val}"/></tt:Data>'
                f"</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
            )
            parts.append(
                f"<wsnt:NotificationMessage>{ref}"
                f'<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
                f"tns1:VideoSource/MotionAlarm</wsnt:Topic>"
                f'<wsnt:Message><tt:Message UtcTime="{t}" PropertyOperation="{ev.operation}">'
                f'<tt:Source><tt:SimpleItem Name="Source" Value="{self.vs_token}"/></tt:Source>'
                f'<tt:Data><tt:SimpleItem Name="State" Value="{val}"/></tt:Data>'
                f"</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
            )
        return "".join(parts)

    def _notify(self, sub: Subscription, events: list[Event]) -> None:
        body = f"<wsnt:Notify>{self.messages_xml(events)}</wsnt:Notify>"
        data = X.envelope(body, action="http://docs.oasis-open.org/wsn/bw-2/NotificationConsumer/Notify",
                          to=sub.consumer).encode()
        req = urllib.request.Request(sub.consumer, data=data, method="POST",
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:
            log.warning("Falha ao enviar Notify para %s: %s", sub.consumer, exc)


def event_properties_xml() -> str:
    return (
        "<tev:GetEventPropertiesResponse>"
        "<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>"
        "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>"
        "<wstop:TopicSet>"
        '<tns1:RuleEngine><CellMotionDetector><Motion wstop:topic="true">'
        '<tt:MessageDescription IsProperty="true"><tt:Source>'
        '<tt:SimpleItemDescription Name="VideoSourceConfigurationToken" Type="tt:ReferenceToken"/>'
        '<tt:SimpleItemDescription Name="VideoAnalyticsConfigurationToken" Type="tt:ReferenceToken"/>'
        '<tt:SimpleItemDescription Name="Rule" Type="xs:string"/>'
        "</tt:Source><tt:Data>"
        '<tt:SimpleItemDescription Name="IsMotion" Type="xs:boolean"/>'
        "</tt:Data></tt:MessageDescription>"
        "</Motion></CellMotionDetector></tns1:RuleEngine>"
        '<tns1:VideoSource><MotionAlarm wstop:topic="true">'
        '<tt:MessageDescription IsProperty="true"><tt:Source>'
        '<tt:SimpleItemDescription Name="Source" Type="tt:ReferenceToken"/>'
        "</tt:Source><tt:Data>"
        '<tt:SimpleItemDescription Name="State" Type="xs:boolean"/>'
        "</tt:Data></tt:MessageDescription>"
        "</MotionAlarm></tns1:VideoSource>"
        "</wstop:TopicSet>"
        "<wsnt:TopicExpressionDialect>http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet</wsnt:TopicExpressionDialect>"
        "<wsnt:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>"
        "<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>"
        "<tev:MessageContentSchemaLocation>http://www.onvif.org/onvif/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>"
        "</tev:GetEventPropertiesResponse>"
    )


def termination_after(seconds: float) -> str:
    return utc_iso(time.time() + seconds)


__all__ = ["EventBroker", "parse_duration", "parse_termination", "utc_iso",
           "event_properties_xml", "termination_after", "timedelta"]
