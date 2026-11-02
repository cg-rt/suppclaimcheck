"""The config CLI shared by every module (P2.09, D-048): `python -m scc_<module>.config <command>`.

Runs inside the module's container, as the module's own database role. Commands:
  list                      versions, which one is active, and each one's latest eval
  show VERSION              the version's TOML
  diff A [B]                what changes from A to B (B defaults to the active version)
  history                   the audit trail, newest first
  create --file F           store a new version (F = - reads standard input)
  activate VERSION          point the module at VERSION
  rollback                  undo the last activation
  attach-eval VERSION --run F   link an eval run file to the version it ran
Every change needs --by (who) and --reason (why); both go into the audit trail.
"""

import argparse
import json
import sys
from pathlib import Path

import psycopg

from scc_common import config_store
from scc_common.config_store import ConfigStoreError, Store


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="versions, the active one and their latest eval")
    show = sub.add_parser("show", help="a version's TOML")
    show.add_argument("version")
    compare = sub.add_parser("diff", help="what changes from A to B")
    compare.add_argument("a")
    compare.add_argument("b", nargs="?", help="default: the active version")
    log = sub.add_parser("history", help="the audit trail, newest first")
    log.add_argument("--limit", type=int, default=50)

    def change(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("--by", required=True, help="who is making the change")
        return p

    create = change("create", "store a new version from a TOML file")
    create.add_argument(
        "--file", required=True, help="TOML file; - reads standard input"
    )
    create.add_argument("--reason", required=True)
    activate = change("activate", "point the module at a version")
    activate.add_argument("version")
    activate.add_argument("--reason", required=True)
    activate.add_argument("--accept-warnings", metavar="WHY")
    rollback = change("rollback", "undo the last activation")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--accept-warnings", metavar="WHY")
    attach = change("attach-eval", "link an eval run file to the version it ran")
    attach.add_argument("version")
    attach.add_argument(
        "--run", required=True, help="run JSON file; - reads standard input"
    )
    return parser


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text("utf-8")


def _applied(store: Store) -> str:
    if store.restart_to_apply:
        return f"Restart {store.restart_to_apply} to apply it."
    return "New checks use it from now on; checks already running keep their version."


def run(store: Store, conn: psycopg.Connection, args: argparse.Namespace) -> str:
    match args.command:
        case "list":
            lines = []
            for (
                version,
                source,
                created_at,
                by,
                reason,
                active,
                evaluation,
            ) in config_store.versions(conn, store):
                mark = "*" if active else " "
                line = f"{mark} {version:<32} {source:<4} {created_at:%Y-%m-%d %H:%M} {by}: {reason}"
                if store.evaluated:
                    if evaluation is None:
                        line += "  [no eval]"
                    else:
                        run_id, split, _, passed, cases, met = evaluation
                        missed = [k for k, ok in met.items() if not ok]
                        line += f"  [eval {run_id}: {passed:g} of {cases} {split}"
                        line += (
                            f", missed {', '.join(missed)}]"
                            if missed
                            else ", all thresholds met]"
                        )
                lines.append(line)
            return "\n".join(lines) + "\n(* = active)"
        case "show":
            return config_store.get_toml(conn, store, args.version)
        case "diff":
            b = args.b or config_store.active_version(conn, store)
            text = config_store.diff(
                config_store.get_toml(conn, store, args.a),
                config_store.get_toml(conn, store, b),
                args.a,
                b,
            )
            return text or f"{args.a} and {b} are the same"
        case "history":
            return "\n".join(
                f"{at:%Y-%m-%d %H:%M} {actor} {action} {version}"
                + (f" (was {previous})" if previous else "")
                + f": {reason}"
                for at, actor, action, version, previous, reason in config_store.history(
                    conn, store, args.limit
                )
            )
        case "create":
            version = config_store.create(
                conn, store, _read(args.file), args.by, args.reason
            )
            return f"Created {version}. It is not active: run `activate {version}` to use it."
        case "activate":
            previous, accepted = config_store.activate(
                conn, store, args.version, args.by, args.reason, args.accept_warnings
            )
            note = f" Warnings accepted: {'; '.join(accepted)}." if accepted else ""
            return f"{args.version} is active (was {previous}).{note} {_applied(store)}"
        case "rollback":
            was, now = config_store.rollback(
                conn, store, args.by, args.reason, args.accept_warnings
            )
            return f"{now} is active again (was {was}). {_applied(store)}"
        case "attach-eval":
            config_store.attach_eval(
                conn, store, args.version, json.loads(_read(args.run)), args.by
            )
            return f"Eval attached to {args.version}."
    raise AssertionError(args.command)


def main(store: Store, conninfo: str, prog: str, argv: list[str] | None = None) -> int:
    args = _parser(prog).parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            print(run(store, conn, args))
    except ConfigStoreError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0
