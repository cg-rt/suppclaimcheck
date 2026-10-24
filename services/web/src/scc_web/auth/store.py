"""Accounts, server-side sessions and sign-in attempts, in web's own `auth` schema (P1.04, D-016).

Every function takes an open psycopg async connection and, where time matters, `now`, so tests can
move the clock. Session IDs are 32 random bytes; only their SHA-256 is stored.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg.rows import dict_row


@dataclass(frozen=True)
class User:
    id: int
    email: str
    role: str
    password_hash: str
    disabled: bool


@dataclass(frozen=True)
class Session:
    user: User
    csrf_token: str


def _hash_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _user(row: dict) -> User:
    return User(
        row["id"],
        row["email"],
        row["role"],
        row["password_hash"],
        row["disabled_at"] is not None,
    )


async def user_by_email(conn: AsyncConnection, email: str) -> User | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, email, role, password_hash, disabled_at FROM auth.users WHERE lower(email) = lower(%s)",
            (email.strip(),),
        )
        row = await cur.fetchone()
    return _user(row) if row else None


async def create_user(
    conn: AsyncConnection, email: str, password_hash: str, role: str
) -> int:
    cur = await conn.execute(
        "INSERT INTO auth.users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
        (email.strip(), password_hash, role),
    )
    return (await cur.fetchone())[0]


async def set_password(conn: AsyncConnection, user_id: int, password_hash: str) -> None:
    await conn.execute(
        "UPDATE auth.users SET password_hash = %s WHERE id = %s",
        (password_hash, user_id),
    )


async def disable_user(conn: AsyncConnection, user_id: int) -> None:
    await conn.execute(
        "UPDATE auth.users SET disabled_at = now() WHERE id = %s", (user_id,)
    )


async def create_session(
    conn: AsyncConnection, user_id: int, now: datetime, config: dict
) -> tuple[str, str]:
    """Returns (session ID for the cookie, CSRF token). The ID itself is never stored."""
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires = now + timedelta(hours=config["session"]["absolute_hours"])
    await conn.execute(
        "INSERT INTO auth.sessions (id_hash, user_id, csrf_token, created_at, last_seen_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (_hash_id(session_id), user_id, csrf_token, now, now, expires),
    )
    return session_id, csrf_token


async def load_session(
    conn: AsyncConnection, session_id: str, now: datetime, config: dict
) -> Session | None:
    """The live session for this ID, refreshing its idle clock; None if unknown, expired, idle too long or disabled."""
    idle_cutoff = now - timedelta(minutes=config["session"]["idle_minutes"])
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE auth.sessions s SET last_seen_at = %(now)s
            FROM auth.users u
            WHERE s.id_hash = %(id)s AND u.id = s.user_id
              AND s.expires_at > %(now)s AND s.last_seen_at > %(idle)s AND u.disabled_at IS NULL
            RETURNING u.id, u.email, u.role, u.password_hash, u.disabled_at, s.csrf_token
            """,
            {"now": now, "id": _hash_id(session_id), "idle": idle_cutoff},
        )
        row = await cur.fetchone()
    if row is None:
        await delete_session(conn, session_id)
        return None
    return Session(_user(row), row["csrf_token"])


async def delete_session(conn: AsyncConnection, session_id: str) -> None:
    await conn.execute(
        "DELETE FROM auth.sessions WHERE id_hash = %s", (_hash_id(session_id),)
    )


async def delete_user_sessions(conn: AsyncConnection, user_id: int) -> None:
    await conn.execute("DELETE FROM auth.sessions WHERE user_id = %s", (user_id,))


async def record_attempt(
    conn: AsyncConnection, email: str, address: str, succeeded: bool, now: datetime
) -> None:
    await conn.execute(
        "INSERT INTO auth.login_attempts (email_lower, address, succeeded, at) VALUES (lower(%s), %s, %s, %s)",
        (email.strip(), address, succeeded, now),
    )


async def sign_in_blocked(
    conn: AsyncConnection, email: str, address: str, now: datetime, config: dict
) -> bool:
    """True when the account is locked (too many recent failures) or the address has made too many attempts."""
    rules = config["login"]
    since = now - timedelta(minutes=rules["window_minutes"])
    cur = await conn.execute(
        """
        SELECT
          (SELECT count(*) FROM auth.login_attempts
            WHERE email_lower = lower(%s) AND NOT succeeded AND at > %s
              AND at > coalesce((SELECT max(at) FROM auth.login_attempts
                                 WHERE email_lower = lower(%s) AND succeeded), '-infinity')),
          (SELECT count(*) FROM auth.login_attempts WHERE address = %s AND at > %s)
        """,
        (email.strip(), since, email.strip(), address, since),
    )
    account_failures, address_attempts = await cur.fetchone()
    return (
        account_failures >= rules["max_failures_per_account"]
        or address_attempts >= rules["max_attempts_per_address"]
    )
