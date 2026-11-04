"""Passwords and tokens. Argon2id for passwords; session tokens are random and stored hashed."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()   # argon2id with the library's current recommended cost
# Verified against when the e-mail is unknown, so a miss costs the same time as a wrong password.
_DUMMY = _hasher.hash("not-a-real-password")

MIN_PASSWORD_LENGTH = 10


class WeakPassword(ValueError):
    pass


def check_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 256:
        raise WeakPassword("That password is too long.")


def hash_password(password: str) -> str:
    check_strength(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True if the password matches. Never raises; a missing hash still does the work."""
    try:
        return _hasher.verify(stored_hash or _DUMMY, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()