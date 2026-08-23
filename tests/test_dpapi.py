"""DPAPI token-at-rest helpers (Windows CryptProtectData)."""
import dpapi


def test_protect_unprotect_roundtrip():
    enc = dpapi.protect("secret-token-123")
    assert dpapi.is_protected(enc) and enc.startswith("dpapi:")
    assert "secret-token-123" not in enc          # ciphertext, not plaintext
    assert dpapi.unprotect(enc) == "secret-token-123"


def test_passthrough_cases():
    assert dpapi.protect("") == "" and dpapi.unprotect("") == ""
    assert dpapi.unprotect("plainvalue") == "plainvalue"   # no marker -> unchanged
    once = dpapi.protect("x")
    assert dpapi.protect(once) == once   # already protected -> returned unchanged, not re-wrapped
