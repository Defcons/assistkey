"""Windows DPAPI helpers to keep the Home Assistant token off disk in plaintext.

`CryptProtectData` ties the ciphertext to the current Windows user account (no
password prompt, no key to manage). We store the result base64-encoded behind a
``dpapi:`` marker so load can tell an encrypted value from a legacy plaintext one
and migrate it on the next save. Everything degrades gracefully: on non-Windows,
or if the API call fails, the value is returned unchanged (plaintext fallback).
"""

from __future__ import annotations

import base64
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("assistkey.dpapi")

PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, int(blob.cbData))


def is_protected(value: str) -> bool:
    return bool(value) and value.startswith(PREFIX)


def protect(secret: str) -> str:
    """Return the secret encrypted as ``dpapi:<base64>`` — or unchanged on failure."""
    if not secret or is_protected(secret):
        return secret
    try:
        blob_in = _to_blob(secret.encode("utf-8"))
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            log.warning("DPAPI encrypt failed; token stored in PLAINTEXT")
            return secret
        try:
            enc = _from_blob(blob_out)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return PREFIX + base64.b64encode(enc).decode("ascii")
    except Exception:  # noqa: BLE001 - non-Windows / missing API: keep plaintext
        log.warning("DPAPI unavailable; token stored in PLAINTEXT")
        return secret


def unprotect(stored: str) -> str:
    """Return the plaintext for a ``dpapi:`` value — or the input unchanged."""
    if not is_protected(stored):
        return stored
    try:
        enc = base64.b64decode(stored[len(PREFIX):])
        blob_in = _to_blob(enc)
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            log.warning("DPAPI decrypt failed (config copied from another Windows account?)")
            return stored
        try:
            dec = _from_blob(blob_out)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return dec.decode("utf-8")
    except Exception:  # noqa: BLE001 - can't decrypt (e.g. copied from another account): leave as-is
        log.warning("DPAPI decrypt failed; leaving stored value as-is")
        return stored
