"""Autenticação ONVIF: WS-UsernameToken (digest/texto) e HTTP Basic/Digest."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import xml.etree.ElementTree as ET

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
REALM = "ScreenCamONVIF"
_SECRET = os.urandom(16)


def ws_username_token(header: ET.Element | None) -> tuple[str, str, str | None, str | None, bool] | None:
    """Retorna (usuario, senha_ou_digest, nonce_b64, created, is_digest) ou None."""
    if header is None:
        return None
    tok = header.find(f".//{{{WSSE}}}UsernameToken")
    if tok is None:
        return None
    user = (tok.findtext(f"{{{WSSE}}}Username") or "").strip()
    pw_el = tok.find(f"{{{WSSE}}}Password")
    password = (pw_el.text or "").strip() if pw_el is not None else ""
    ptype = pw_el.get("Type", "") if pw_el is not None else ""
    nonce = tok.findtext(f"{{{WSSE}}}Nonce")
    created = tok.findtext(f"{{{WSU}}}Created")
    return user, password, nonce, created, ptype.endswith("#PasswordDigest")


def check_ws_token(token, username: str, password: str) -> bool:
    user, pw, nonce, created, is_digest = token
    if user != username:
        return False
    if not is_digest:
        return hmac.compare_digest(pw, password)
    try:
        raw_nonce = base64.b64decode(nonce or "")
    except Exception:
        return False
    digest = base64.b64encode(
        hashlib.sha1(raw_nonce + (created or "").encode() + password.encode()).digest()).decode()
    return hmac.compare_digest(digest, pw)


# ---------------------------------------------------------------- HTTP auth

def _make_nonce() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:24]
    return base64.b64encode(f"{ts}:{sig}".encode()).decode()


def _nonce_ok(nonce: str, max_age: int = 600) -> bool:
    try:
        ts, sig = base64.b64decode(nonce).decode().split(":")
    except Exception:
        return False
    good = hmac.new(_SECRET, ts.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, good) and time.time() - int(ts) < max_age


def digest_challenge() -> str:
    return f'Digest realm="{REALM}", qop="auth", nonce="{_make_nonce()}", algorithm=MD5'


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def check_http_auth(header_value: str | None, method: str, username: str, password: str) -> bool:
    if not header_value:
        return False
    scheme, _, rest = header_value.partition(" ")
    if scheme.lower() == "basic":
        try:
            u, _, p = base64.b64decode(rest.strip()).decode().partition(":")
        except Exception:
            return False
        return u == username and hmac.compare_digest(p, password)
    if scheme.lower() == "digest":
        params = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', rest)}
        if params.get("username") != username or not _nonce_ok(params.get("nonce", "")):
            return False
        ha1 = _md5(f"{username}:{params.get('realm', REALM)}:{password}")
        ha2 = _md5(f"{method}:{params.get('uri', '')}")
        if params.get("qop"):
            expected = _md5(f"{ha1}:{params['nonce']}:{params.get('nc', '')}:"
                            f"{params.get('cnonce', '')}:{params['qop']}:{ha2}")
        else:
            expected = _md5(f"{ha1}:{params['nonce']}:{ha2}")
        return hmac.compare_digest(expected, params.get("response", ""))
    return False
