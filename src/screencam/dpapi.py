"""Cifragem da senha do NVR com a DPAPI do Windows (CryptProtectData).

Escopo de máquina (CRYPTPROTECT_LOCAL_MACHINE): o serviço (SYSTEM) e o
instalador (administrador) são contas diferentes, então o escopo de usuário
não serviria — ambos precisam decifrar. O escopo de máquina permite isso e,
somado à ACL do config.ini (só administradores leem), garante que a senha:

* não fique em texto claro no disco;
* não possa ser decifrada se o arquivo for copiado para outro computador.

Uma "entropy" fixa do aplicativo é somada como camada extra (defesa em
profundidade; não é um segredo forte, pois está no binário).

Fora do Windows (ou sem a DLL), ``is_available()`` é False e as funções de
cifra não são usadas — o texto é mantido como está.
"""
from __future__ import annotations

import base64
import sys

IS_WINDOWS = sys.platform == "win32"
ENTROPY = b"ScreenCamONVIF/dpapi/v1"
CRYPTPROTECT_LOCAL_MACHINE = 0x4


def is_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        ctypes.WinDLL("crypt32")
        return True
    except OSError:
        return False


def _run(func_name: str, data: bytes, entropy: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b)) if b else ctypes.create_string_buffer(0)
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob = blob(data)
    ent_blob = blob(entropy)
    out_blob = DATA_BLOB()
    func = getattr(crypt32, func_name)
    ok = func(ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
              CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect(plaintext: str, entropy: bytes = ENTROPY) -> str:
    """Cifra e devolve base64 (texto)."""
    blob = _run("CryptProtectData", plaintext.encode("utf-8"), entropy)
    return base64.b64encode(blob).decode("ascii")


def unprotect(b64: str, entropy: bytes = ENTROPY) -> str:
    """Decifra o base64 produzido por :func:`protect`."""
    blob = _run("CryptUnprotectData", base64.b64decode(b64), entropy)
    return blob.decode("utf-8")
