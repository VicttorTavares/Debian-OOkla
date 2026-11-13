"""Utilitários SOAP/XML."""
from __future__ import annotations

import uuid
from xml.sax.saxutils import escape

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "tns1": "http://www.onvif.org/ver10/topics",
    "ter": "http://www.onvif.org/ver10/error",
    "xs": "http://www.w3.org/2001/XMLSchema",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

_NS_DECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())


def esc(text) -> str:
    return escape(str(text), {'"': "&quot;"})


def envelope(body: str, action: str | None = None, to: str | None = None,
             relates_to: str | None = None) -> str:
    header = ""
    if action:
        header = (f"<s:Header><wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
                  + (f"<wsa:RelatesTo>{esc(relates_to)}</wsa:RelatesTo>" if relates_to else "")
                  + (f"<wsa:To>{esc(to)}</wsa:To>" if to else "")
                  + f"<wsa:Action>{esc(action)}</wsa:Action></s:Header>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<s:Envelope {_NS_DECL}>'
            f"{header}<s:Body>{body}</s:Body></s:Envelope>")


def fault(code: str, subcode: str, reason: str) -> str:
    return envelope(
        "<s:Fault>"
        f"<s:Code><s:Value>s:{code}</s:Value><s:Subcode><s:Value>{subcode}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{esc(reason)}</s:Text></s:Reason>'
        "</s:Fault>")
