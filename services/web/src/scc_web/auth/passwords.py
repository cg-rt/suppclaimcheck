"""Password hashing with Argon2id (D-016), via argon2-cffi's defaults (RFC 9106 low-memory profile)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()
# Verified against when the account doesn't exist, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash("not a real password, only used to equalise timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(stored_hash: str | None, password: str) -> bool:
    try:
        return (
            _hasher.verify(stored_hash or _DUMMY_HASH, password)
            and stored_hash is not None
        )
    except VerificationError, InvalidHashError:
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than today's; rehash at the next sign-in."""
    return _hasher.check_needs_rehash(stored_hash)


def check_new_password(password: str, config: dict) -> str | None:
    """Returns a problem with a new password, in words a user can act on, or None."""
    rules = config["password"]
    if len(password) < rules["min_length"]:
        return f"Use at least {rules['min_length']} characters."
    if len(password) > rules["max_length"]:
        return f"Use at most {rules['max_length']} characters."
    return None
