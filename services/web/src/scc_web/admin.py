"""Admin CLI for invite-only accounts (P1.04): there is no sign-up page.

Run inside the web container, which has the database settings:
    python -m scc_web.admin create-user someone@example.com [--admin]
    python -m scc_web.admin set-password someone@example.com
    python -m scc_web.admin disable someone@example.com
    python -m scc_web.admin ensure-user smoke@example.com   (deploy.sh: create it, or match its password)

The password is asked for twice at a hidden prompt, or read from stdin when it isn't a terminal,
so it never lands in the shell history or the process list.

The admin users page (P3.10) creates, resets and disables accounts with the same functions.
"""

import argparse
import asyncio
import getpass
import re
import secrets
import sys

import psycopg

from scc_common.db import conninfo_from_env
from scc_web import config
from scc_web.auth import passwords, store


def read_password(cfg: dict) -> str:
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Same password again: ") != password:
            raise SystemExit("The two passwords don't match.")
    problem = passwords.check_new_password(password, cfg)
    if problem:
        raise SystemExit(problem)
    return password


class AccountError(Exception):
    """A refused account change; the message says why, in words the admin can act on."""


EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


def new_password() -> str:
    """A random password for an account made or reset in admin, shown once to that admin."""
    return secrets.token_urlsafe(15)


async def create_account(
    conn: psycopg.AsyncConnection, email: str, role: str, password: str, cfg: dict
) -> int:
    email = email.strip()
    if not EMAIL.fullmatch(email) or len(email) > 254:
        raise AccountError(f"{email or 'That'} doesn't look like an email address.")
    if role not in {"user", "admin"}:
        raise AccountError("The role must be user or admin.")
    problem = passwords.check_new_password(password, cfg)
    if problem:
        raise AccountError(problem)
    if await store.user_by_email(conn, email):
        raise AccountError(f"An account for {email} already exists.")
    return await store.create_user(conn, email, passwords.hash_password(password), role)


async def reset_password(
    conn: psycopg.AsyncConnection, user: store.User, password: str, cfg: dict
) -> None:
    """Sets a new password and ends every session of the account."""
    problem = passwords.check_new_password(password, cfg)
    if problem:
        raise AccountError(problem)
    await store.set_password(conn, user.id, passwords.hash_password(password))
    await store.delete_user_sessions(conn, user.id)


async def disable_account(conn: psycopg.AsyncConnection, user: store.User) -> None:
    """Stops the account signing in and ends its sessions."""
    await store.disable_user(conn, user.id)
    await store.delete_user_sessions(conn, user.id)


async def run(args: argparse.Namespace, conninfo: str, cfg: dict) -> str:
    async with await psycopg.AsyncConnection.connect(conninfo) as conn:
        user = await store.user_by_email(conn, args.email)
        if args.command == "create-user":
            role = "admin" if args.admin else "user"
            if user:
                raise SystemExit(f"An account for {args.email} already exists.")
            try:
                user_id = await create_account(
                    conn, args.email, role, read_password(cfg), cfg
                )
            except AccountError as exc:
                raise SystemExit(str(exc)) from None
            return f"Created {role} account {user_id} for {args.email}."
        if args.command == "ensure-user":
            password = read_password(cfg)
            if user is None:
                await store.create_user(
                    conn, args.email, passwords.hash_password(password), "user"
                )
                return f"Created user account for {args.email}."
            if user.disabled:
                raise SystemExit(
                    f"{args.email} is disabled; enable it by hand if it should sign in."
                )
            if not passwords.verify(user.password_hash, password):
                await store.set_password(
                    conn, user.id, passwords.hash_password(password)
                )
                return f"Password updated for {args.email}."
            return f"{args.email} is ready."
        if user is None:
            raise SystemExit(f"No account for {args.email}.")
        if args.command == "set-password":
            await reset_password(conn, user, read_password(cfg), cfg)
            return f"Password changed for {args.email}; their sessions were ended."
        await disable_account(conn, user)
        return f"Disabled {args.email}; their sessions were ended."


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scc_web.admin", description="Manage invite-only accounts."
    )
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser(
        "create-user", help="create an account (asks for the password)"
    )
    create.add_argument("email")
    create.add_argument(
        "--admin", action="store_true", help="give the account the admin role"
    )
    sub.add_parser(
        "set-password", help="set a new password and end the account's sessions"
    ).add_argument("email")
    sub.add_parser(
        "disable", help="stop the account signing in and end its sessions"
    ).add_argument("email")
    sub.add_parser(
        "ensure-user",
        help="create a user account, or set its password to the one given",
    ).add_argument("email")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    conninfo = conninfo_from_env()
    print(asyncio.run(run(args, conninfo, config.load_active(conninfo))))


if __name__ == "__main__":
    main()
