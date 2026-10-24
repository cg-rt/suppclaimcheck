"""Admin CLI for invite-only accounts (P1.04): there is no sign-up page.

Run inside the web container, which has the database settings:
    python -m scc_web.admin create-user someone@example.com [--admin]
    python -m scc_web.admin set-password someone@example.com
    python -m scc_web.admin disable someone@example.com

The password is asked for twice at a hidden prompt, or read from stdin when it isn't a terminal,
so it never lands in the shell history or the process list.
"""

import argparse
import asyncio
import getpass
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


async def run(args: argparse.Namespace, conninfo: str, cfg: dict) -> str:
    async with await psycopg.AsyncConnection.connect(conninfo) as conn:
        user = await store.user_by_email(conn, args.email)
        if args.command == "create-user":
            if user:
                raise SystemExit(f"An account for {args.email} already exists.")
            role = "admin" if args.admin else "user"
            user_id = await store.create_user(
                conn, args.email, passwords.hash_password(read_password(cfg)), role
            )
            return f"Created {role} account {user_id} for {args.email}."
        if user is None:
            raise SystemExit(f"No account for {args.email}.")
        if args.command == "set-password":
            await store.set_password(
                conn, user.id, passwords.hash_password(read_password(cfg))
            )
            await store.delete_user_sessions(conn, user.id)
            return f"Password changed for {args.email}; their sessions were ended."
        await store.disable_user(conn, user.id)
        await store.delete_user_sessions(conn, user.id)
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
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    print(asyncio.run(run(args, conninfo_from_env(), config.load())))


if __name__ == "__main__":
    main()
