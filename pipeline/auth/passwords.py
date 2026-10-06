"""Password hashing — Argon2id via a proven library, with a stdlib fallback.

Prefers ``argon2-cffi`` (Argon2id). If unavailable, falls back to
``hashlib.scrypt`` (a vetted stdlib KDF) so the system is never left storing
plaintext. Hash strings are self-describing so verification always picks the
right scheme. Plaintext passwords are never stored or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading

try:  # Preferred: Argon2id
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError

    _PH = PasswordHasher()  # sensible defaults (Argon2id)
    _HAVE_ARGON2 = True
except Exception:  # pragma: no cover - fallback path
    _PH = None
    _HAVE_ARGON2 = False

# scrypt parameters (used only in fallback).
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return a self-describing password hash. Never returns plaintext."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if _HAVE_ARGON2:
        return _PH.hash(password)
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                        r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
                        maxmem=0)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time-ish verification against a stored hash of any scheme."""
    if not stored_hash:
        return False
    if stored_hash.startswith("$argon2"):
        if not _HAVE_ARGON2:
            return False
        try:
            return _PH.verify(stored_hash, password)
        except (VerifyMismatchError, InvalidHashError, Exception):
            return False
    if stored_hash.startswith("scrypt$"):
        try:
            _, n, r, p, salt_hex, hash_hex = stored_hash.split("$")
            dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p),
                                dklen=len(bytes.fromhex(hash_hex)), maxmem=0)
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return False


def needs_rehash(stored_hash: str) -> bool:
    if _HAVE_ARGON2 and stored_hash.startswith("$argon2"):
        try:
            return _PH.check_needs_rehash(stored_hash)
        except Exception:
            return False
    # A scrypt hash while argon2 is now available -> upgrade on next login.
    return _HAVE_ARGON2 and stored_hash.startswith("scrypt$")


def generate_temp_password(length: int = 16) -> str:
    """Cryptographically secure temporary password (URL-safe)."""
    return secrets.token_urlsafe(length)[:length]


# One precomputed dummy hash using the live algorithm/work factor. The
# password that produced it is discarded so it cannot authenticate anyone.
_DUMMY_HASH: str | None = None
_DUMMY_LOCK = threading.Lock()


def dummy_password_hash() -> str:
    """Valid stored-hash string for unknown-user verification work."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        with _DUMMY_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = hash_password(secrets.token_urlsafe(24))
    return _DUMMY_HASH
