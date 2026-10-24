"""Database access per module: one schema and two roles each (P1.03, AGENTS.md section 7).

For a module `core` with prefix `scc_`:
- `scc_core_owner` (no login) owns the schema and every table in it. Migrations run as this role.
- `scc_core_app` (login) is what the running module connects as: it can read and write rows in its own
  schema, but can't create or change tables, and has no access to any other module's schema.

`bootstrap()` creates or repairs all of this. It is idempotent and runs before every migration,
as the database superuser, from the module's one-off migrate step.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

DEFAULT_PREFIX = "scc_"


@dataclass(frozen=True)
class ModuleDb:
    module: str  # web, core, gateway
    schema: str  # auth, core, gateway
    extensions: tuple[str, ...] = field(default=())
    prefix: str = DEFAULT_PREFIX

    @property
    def owner_role(self) -> str:
        return f"{self.prefix}{self.module}_owner"

    @property
    def app_role(self) -> str:
        return f"{self.prefix}{self.module}_app"


def read_secret(path: str | os.PathLike) -> str:
    """Docker secrets are files under /run/secrets; trailing newlines are not part of the value."""
    return Path(path).read_text(encoding="utf-8").strip()


def conninfo_from_env(
    user_var: str = "SCC_DB_USER", password_file_var: str = "SCC_DB_PASSWORD_FILE"
) -> str:
    """Connection string built from SCC_DB_* variables; the password comes from a secret file, never the environment."""
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("SCC_DB_HOST", "postgres"),
        port=os.environ.get("SCC_DB_PORT", "5432"),
        dbname=os.environ.get("SCC_DB_NAME", "scc"),
        user=os.environ[user_var],
        password=read_secret(os.environ[password_file_var]),
        connect_timeout=5,
        application_name=os.environ.get("SCC_SERVICE", "scc"),
    )


def db_check(conninfo: str):
    """A `/health/deps` check that the module can log in to the database as its own role."""

    async def check() -> None:
        async with await psycopg.AsyncConnection.connect(conninfo) as conn:
            await conn.execute("SELECT 1")

    return check


def bootstrap(admin: psycopg.Connection, spec: ModuleDb, app_password: str) -> None:
    """Creates or repairs the module's roles, schema and grants. `admin` must be a superuser in autocommit mode."""
    owner = sql.Identifier(spec.owner_role)
    app = sql.Identifier(spec.app_role)
    schema = sql.Identifier(spec.schema)
    database = sql.Identifier(admin.info.dbname)

    for role, login in ((spec.owner_role, False), (spec.app_role, True)):
        exists = admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if not exists:
            admin.execute(
                sql.SQL("CREATE ROLE {} {}").format(
                    sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN")
                )
            )
    # Setting the password every time means rotating it is: change the secret file, run the migrate step.
    admin.execute(
        sql.SQL(
            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
        ).format(app, sql.Literal(app_password))
    )

    for extension in spec.extensions:
        admin.execute(
            sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                sql.Identifier(extension)
            )
        )

    statements = [
        # Nobody gets in by default; each app role is granted its own database and schema only.
        "REVOKE ALL ON DATABASE {database} FROM PUBLIC",
        # Extensions live in public; nobody may create anything there (the PostgreSQL 15+ default, made explicit).
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "GRANT CONNECT ON DATABASE {database} TO {app}",
        "CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {owner}",
        "ALTER SCHEMA {schema} OWNER TO {owner}",
        "REVOKE ALL ON SCHEMA {schema} FROM PUBLIC",
        "GRANT USAGE ON SCHEMA {schema} TO {app}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {app}",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {app}",
        "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app}",
        "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {app}",
        # Unqualified names resolve to the module's own schema, then to extensions such as pgvector.
        "ALTER ROLE {app} IN DATABASE {database} SET search_path = {schema}, public",
    ]
    for statement in statements:
        admin.execute(
            sql.SQL(statement).format(
                database=database, schema=schema, owner=owner, app=app
            )
        )
