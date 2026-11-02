"""Versioned config store (P2.09, D-048): immutable versions, one active pointer, an audit trail.

Each module keeps four tables in its own schema, made by its own migration with `tables_sql()`:
- `config_versions`: one row per version (TOML text, parsed body, SHA-256, who, when, why, source).
- `config_evals`: eval results attached to a version.
- `config_audit`: every create, register, activate and eval attachment, with the diff.
- `config_active`: one row, the pointer.
Triggers make the first three append-only, even for the module's own role.

Versions come from the module's TOML files (registered by the migrate step) and from the CLI
(`scc_common.config_cli`). The sync functions here serve the CLI and the migrate step; the async ones
serve the running modules.
"""

import difflib
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


class ConfigStoreError(Exception):
    """A refused change: the message says why, in words the owner can act on."""


@dataclass(frozen=True)
class Store:
    schema: str  # the module's schema: core, gateway, auth
    folder: Path  # the module's TOML files
    prefix: str  # version names start with it: "pipeline-", "gateway-", "web-"
    shipped: str  # the version this code ships as its default (the module's ACTIVE)
    evaluated: bool  # whether eval results apply (only the pipeline has evals)
    restart_to_apply: str | None = None  # e.g. "gateway": printed after activation

    def table(self, name: str) -> str:
        return f"{self.schema}.config_{name}"

    def is_repo_name(self, version: str) -> bool:
        return re.fullmatch(re.escape(self.prefix) + r"v\d+", version) is not None


def tables_sql(schema: str) -> list[str]:
    """The DDL for a module's migration, one statement each. Run as the schema's owner role."""
    statements = [
        f"""
        CREATE TABLE {schema}.config_versions (
            version text PRIMARY KEY,
            body_toml text NOT NULL,
            body jsonb NOT NULL,
            sha256 text NOT NULL UNIQUE,
            source text NOT NULL CHECK (source IN ('repo', 'cli')),
            created_at timestamptz NOT NULL DEFAULT now(),
            created_by text NOT NULL,
            reason text NOT NULL
        )""",
        f"""
        CREATE TABLE {schema}.config_evals (
            id bigserial PRIMARY KEY,
            version text NOT NULL REFERENCES {schema}.config_versions (version),
            run_id text NOT NULL,
            split text NOT NULL,
            run_date date NOT NULL,
            passed double precision NOT NULL,
            cases integer NOT NULL,
            thresholds_met jsonb NOT NULL,
            attached_at timestamptz NOT NULL DEFAULT now(),
            attached_by text NOT NULL,
            UNIQUE (version, run_id)
        )""",
        f"""
        CREATE TABLE {schema}.config_audit (
            id bigserial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            actor text NOT NULL,
            action text NOT NULL CHECK (action IN ('register', 'create', 'activate', 'attach_eval')),
            version text NOT NULL,
            previous_version text,
            reason text NOT NULL,
            diff text
        )""",
        f"""
        CREATE TABLE {schema}.config_active (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            version text NOT NULL REFERENCES {schema}.config_versions (version),
            activated_at timestamptz NOT NULL DEFAULT now(),
            activated_by text NOT NULL
        )""",
        f"""
        CREATE FUNCTION {schema}.config_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'config history is append-only: % on % refused', TG_OP, TG_TABLE_NAME;
        END
        $$""",
    ]
    for table in ("config_versions", "config_evals", "config_audit"):
        statements.append(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {schema}.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {schema}.config_append_only()"
        )
        statements.append(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {schema}.{table}"
            f" FOR EACH STATEMENT EXECUTE FUNCTION {schema}.config_append_only()"
        )
    return statements


def drop_sql(schema: str) -> list[str]:
    return [
        f"DROP TABLE {schema}.config_active, {schema}.config_audit, {schema}.config_evals, {schema}.config_versions",
        f"DROP FUNCTION {schema}.config_append_only()",
    ]


def content_hash(body: dict) -> str:
    """SHA-256 of the settings, independent of TOML formatting and of the version name.

    The eval runner records the same hash, so a run matches every version with exactly its settings.
    """
    settings = {k: v for k, v in body.items() if k != "version"}
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def shape(body: dict, path: str = "") -> dict[str, str]:
    """Every key path and its value type: what a CLI version must keep."""
    out: dict[str, str] = {}
    for key, value in body.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            out[here] = "table"
            out |= shape(value, here)
        else:
            out[here] = type(value).__name__
    return out


def shape_problems(new: dict, reference: dict) -> list[str]:
    want, got = shape(reference), shape(new)
    problems = [f"missing {p}" for p in sorted(want.keys() - got.keys())]
    problems += [f"unknown {p}" for p in sorted(got.keys() - want.keys())]
    problems += [
        f"{p} is {got[p]}, expected {want[p]}"
        for p in sorted(want.keys() & got.keys())
        # An int where a float is expected is the same number to TOML readers.
        if got[p] != want[p] and {got[p], want[p]} != {"int", "float"}
    ]
    return problems


def parse(store: Store, toml_text: str) -> dict:
    try:
        body = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigStoreError(f"not valid TOML: {exc}") from None
    version = body.get("version")
    if not isinstance(version, str) or not version.startswith(store.prefix):
        raise ConfigStoreError(
            f'the file needs version = "{store.prefix}..." at the top'
        )
    return body


def diff(old_toml: str | None, new_toml: str, old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            (old_toml or "").splitlines(keepends=True),
            new_toml.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
        )
    )


# --- sync: the migrate step and the CLI ---------------------------------------------------------


def _audit(
    conn: psycopg.Connection,
    store: Store,
    actor: str,
    action: str,
    version: str,
    reason: str,
    previous: str | None = None,
    diff_text: str | None = None,
) -> None:
    conn.execute(
        f"INSERT INTO {store.table('audit')} (actor, action, version, previous_version, reason, diff)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (actor, action, version, previous, reason, diff_text),
    )


def _row(conn: psycopg.Connection, store: Store, version: str):
    return conn.execute(
        f"SELECT version, body_toml, body, sha256, source, created_at, created_by, reason"
        f" FROM {store.table('versions')} WHERE version = %s",
        (version,),
    ).fetchone()


def active_version(conn: psycopg.Connection, store: Store) -> str | None:
    row = conn.execute(f"SELECT version FROM {store.table('active')}").fetchone()
    return row[0] if row else None


def _insert(
    conn: psycopg.Connection,
    store: Store,
    toml_text: str,
    body: dict,
    source: str,
    actor: str,
    reason: str,
) -> None:
    version = body["version"]
    same = conn.execute(
        f"SELECT version FROM {store.table('versions')} WHERE sha256 = %s",
        (content_hash(body),),
    ).fetchone()
    if same:
        raise ConfigStoreError(f"{version} has the same content as {same[0]}")
    conn.execute(
        f"INSERT INTO {store.table('versions')} (version, body_toml, body, sha256, source, created_by, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (version, toml_text, Jsonb(body), content_hash(body), source, actor, reason),
    )


def _set_active(
    conn: psycopg.Connection, store: Store, version: str, actor: str, reason: str
) -> str | None:
    previous = conn.execute(
        f"SELECT version FROM {store.table('active')} FOR UPDATE"
    ).fetchone()
    previous = previous[0] if previous else None
    conn.execute(
        f"INSERT INTO {store.table('active')} (singleton, version, activated_by) VALUES (true, %s, %s)"
        " ON CONFLICT (singleton) DO UPDATE"
        " SET version = EXCLUDED.version, activated_at = now(), activated_by = EXCLUDED.activated_by",
        (version, actor),
    )
    old = _row(conn, store, previous) if previous else None
    new = _row(conn, store, version)
    _audit(
        conn,
        store,
        actor,
        "activate",
        version,
        reason,
        previous,
        diff(old[1] if old else None, new[1], previous or "(none)", version),
    )
    return previous


def sync_repo_versions(
    conn: psycopg.Connection, store: Store, app_version: str
) -> list[str]:
    """Registers the module's TOML files; activates the shipped default if it is new or nothing is active.

    Runs in the migrate step, in one transaction. A file whose content differs from the registered
    version of the same name stops the deploy: a version, once registered, never changes.
    """
    registered: list[str] = []
    with conn.transaction():
        conn.execute(f"LOCK TABLE {store.table('active')} IN EXCLUSIVE MODE")
        for path in sorted(store.folder.glob(f"{store.prefix}v*.toml")):
            toml_text = path.read_text("utf-8")
            body = parse(store, toml_text)
            if body["version"] != path.stem:
                raise ConfigStoreError(f"{path.name} says it is {body['version']}")
            existing = _row(conn, store, body["version"])
            if existing:
                if existing[3] != content_hash(body):
                    raise ConfigStoreError(
                        f"{path.name} differs from the registered {body['version']}: "
                        "a version never changes; copy it to a new version instead"
                    )
                continue
            _insert(
                conn,
                store,
                toml_text,
                body,
                "repo",
                "deploy",
                f"shipped in app {app_version}",
            )
            _audit(
                conn,
                store,
                "deploy",
                "register",
                body["version"],
                f"shipped in app {app_version}",
            )
            registered.append(body["version"])
        if store.shipped in registered or active_version(conn, store) is None:
            _set_active(
                conn,
                store,
                store.shipped,
                "deploy",
                f"default of app {app_version}",
            )
    return registered


def create(
    conn: psycopg.Connection, store: Store, toml_text: str, actor: str, reason: str
) -> str:
    """Stores a new version from the CLI. It must keep the shipped version's keys and value types."""
    body = parse(store, toml_text)
    version = body["version"]
    if store.is_repo_name(version):
        raise ConfigStoreError(
            f"{version} looks like a repo version name; pick a name such as "
            f"{store.shipped}-<what changed> so it can never collide with a future release"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,63}", version):
        raise ConfigStoreError(
            "version names use lowercase letters, digits, dots and hyphens (3-64 characters)"
        )
    with conn.transaction():
        if _row(conn, store, version):
            raise ConfigStoreError(f"{version} already exists")
        reference = _row(conn, store, store.shipped)
        if reference is None:
            raise ConfigStoreError(
                f"{store.shipped} is not registered: run the migrate step first"
            )
        problems = shape_problems(body, reference[2])
        if problems:
            raise ConfigStoreError(
                f"{version} must have the same settings as {store.shipped}: "
                + "; ".join(problems)
            )
        _insert(conn, store, toml_text, body, "cli", actor, reason)
        _audit(
            conn,
            store,
            actor,
            "create",
            version,
            reason,
            store.shipped,
            diff(reference[1], toml_text, store.shipped, version),
        )
    return version


def latest_eval(conn: psycopg.Connection, store: Store, version: str):
    return conn.execute(
        f"SELECT run_id, split, run_date, passed, cases, thresholds_met FROM {store.table('evals')}"
        " WHERE version = %s ORDER BY run_date DESC, id DESC LIMIT 1",
        (version,),
    ).fetchone()


def warnings(conn: psycopg.Connection, store: Store, version: str) -> list[str]:
    """Why activating this version deserves a second look. Empty when there is nothing to say."""
    if not store.evaluated:
        return []
    row = latest_eval(conn, store, version)
    if row is None:
        return [f"{version} has no eval result attached"]
    failed = sorted(name for name, ok in row[5].items() if not ok)
    if failed:
        return [
            f"{version}'s latest eval ({row[0]}, {row[1]}) missed: {', '.join(failed)}"
        ]
    return []


def activate(
    conn: psycopg.Connection,
    store: Store,
    version: str,
    actor: str,
    reason: str,
    accept_warnings: str | None = None,
) -> tuple[str | None, list[str]]:
    """Points the module at `version`. Returns (previous version, warnings that were accepted)."""
    with conn.transaction():
        if _row(conn, store, version) is None:
            raise ConfigStoreError(f"no version {version}")
        if active_version(conn, store) == version:
            raise ConfigStoreError(f"{version} is already active")
        found = warnings(conn, store, version)
        if found and not accept_warnings:
            raise ConfigStoreError(
                "; ".join(found) + '. To activate anyway, add --accept-warnings "<why>"'
            )
        if found:
            reason = (
                f"{reason} [warnings accepted: {'; '.join(found)}: {accept_warnings}]"
            )
        previous = _set_active(conn, store, version, actor, reason)
    return previous, found


def rollback(
    conn: psycopg.Connection,
    store: Store,
    actor: str,
    reason: str,
    accept_warnings: str | None = None,
) -> tuple[str, str]:
    """Reactivates the version that was active before the current one. Returns (from, to).

    It was live until the last activation, so its warnings, if any, are recorded rather than asked about.
    """
    row = conn.execute(
        f"SELECT version, previous_version FROM {store.table('audit')}"
        " WHERE action = 'activate' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None or row[1] is None:
        raise ConfigStoreError("there is no earlier active version to go back to")
    activate(
        conn,
        store,
        row[1],
        actor,
        f"rollback: {reason}",
        accept_warnings or "rollback to the version that was live before",
    )
    return row[0], row[1]


def attach_eval(
    conn: psycopg.Connection, store: Store, version: str, run: dict, actor: str
) -> None:
    """Links an eval run file to the version it ran, matched by content hash."""
    if not store.evaluated:
        raise ConfigStoreError("this module's config has no evals")
    meta, summary = run["run"], run["summary"]
    if meta.get("partial") or meta.get("overrides"):
        raise ConfigStoreError(
            "only full runs without --set overrides describe a version"
        )
    with conn.transaction():
        row = _row(conn, store, version)
        if row is None:
            raise ConfigStoreError(f"no version {version}")
        if meta.get("config_sha256") != row[3]:
            raise ConfigStoreError(
                f"run {meta['run_id']} ran different settings from {version} (content hash differs)"
            )
        conn.execute(
            f"INSERT INTO {store.table('evals')} (version, run_id, split, run_date, passed, cases,"
            " thresholds_met, attached_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                version,
                meta["run_id"],
                meta["split"],
                meta["date"],
                summary["passed"],
                summary["cases"],
                Jsonb(summary["thresholds_met"]),
                actor,
            ),
        )
        _audit(
            conn,
            store,
            actor,
            "attach_eval",
            version,
            f"eval {meta['run_id']}: {summary['passed']} of {summary['cases']} {meta['split']} cases",
        )


def versions(conn: psycopg.Connection, store: Store) -> list[tuple]:
    """(version, source, created_at, created_by, reason, active, latest eval or None), oldest first."""
    active = active_version(conn, store)
    rows = conn.execute(
        f"SELECT version, source, created_at, created_by, reason FROM {store.table('versions')}"
        " ORDER BY created_at, version"
    ).fetchall()
    return [
        (
            *r,
            r[0] == active,
            latest_eval(conn, store, r[0]) if store.evaluated else None,
        )
        for r in rows
    ]


def get_toml(conn: psycopg.Connection, store: Store, version: str) -> str:
    row = _row(conn, store, version)
    if row is None:
        raise ConfigStoreError(f"no version {version}")
    return row[1]


def history(conn: psycopg.Connection, store: Store, limit: int = 50) -> list[tuple]:
    return conn.execute(
        f"SELECT at, actor, action, version, previous_version, reason FROM {store.table('audit')}"
        " ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()


# --- async: the running modules -----------------------------------------------------------------


async def load_active_async(conn: psycopg.AsyncConnection, store: Store) -> dict:
    """The active version's body. A module with no active version is not deployed correctly: fail loudly."""
    cur = await conn.execute(
        f"SELECT v.body FROM {store.table('active')} a JOIN {store.table('versions')} v USING (version)"
    )
    row = await cur.fetchone()
    if row is None:
        raise ConfigStoreError(
            f"no active config in {store.schema}: run the migrate step"
        )
    return row[0]


async def active_version_async(conn: psycopg.AsyncConnection, store: Store) -> str:
    cur = await conn.execute(f"SELECT version FROM {store.table('active')}")
    row = await cur.fetchone()
    if row is None:
        raise ConfigStoreError(
            f"no active config in {store.schema}: run the migrate step"
        )
    return row[0]


async def load_version_async(
    conn: psycopg.AsyncConnection, store: Store, version: str
) -> dict:
    cur = await conn.execute(
        f"SELECT body FROM {store.table('versions')} WHERE version = %s", (version,)
    )
    row = await cur.fetchone()
    if row is None:
        raise ConfigStoreError(f"no version {version} in {store.schema}")
    return row[0]


def load_active(conn: psycopg.Connection, store: Store) -> dict:
    """Sync twin of `load_active_async`, for start-up code that runs before the event loop."""
    row = conn.execute(
        f"SELECT v.body FROM {store.table('active')} a JOIN {store.table('versions')} v USING (version)"
    ).fetchone()
    if row is None:
        raise ConfigStoreError(
            f"no active config in {store.schema}: run the migrate step"
        )
    return row[0]
