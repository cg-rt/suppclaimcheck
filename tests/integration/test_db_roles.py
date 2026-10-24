"""Database roles and schemas (P1.03): each module reaches only its own schema.

Needs a PostgreSQL 18 + pgvector server and a superuser connection string in SCC_TEST_ADMIN_DSN.
CI provides one; locally, see "Working on it" in README.md. Each run uses a fresh database and
role names with a random prefix, so it never touches the dev stack's roles.
"""

import dataclasses
import os
import uuid

import psycopg
import pytest
from psycopg import sql

from scc_common.migrations import migrate
from scc_core import migrate as core_migrate
from scc_gateway import migrate as gateway_migrate
from scc_web import migrate as web_migrate

ADMIN_DSN = os.environ.get("SCC_TEST_ADMIN_DSN")
if not ADMIN_DSN:
    if os.environ.get("CI"):
        raise RuntimeError(
            "SCC_TEST_ADMIN_DSN must be set in CI: the database tests may not be skipped there"
        )
    pytest.skip(
        "SCC_TEST_ADMIN_DSN not set (no test database)", allow_module_level=True
    )

MODULES = [web_migrate, core_migrate, gateway_migrate]
PASSWORD = "test-only"


@pytest.fixture(scope="module")
def db():
    suffix = uuid.uuid4().hex[:8]
    prefix = f"t{suffix}_"
    dbname = f"scc_test_{suffix}"
    specs = {m.SPEC.module: dataclasses.replace(m.SPEC, prefix=prefix) for m in MODULES}
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    admin_conninfo = psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=dbname)
    try:
        # Twice: the migrate step runs on every deploy, so it must be safe to repeat.
        for _ in range(2):
            for m in MODULES:
                migrate(specs[m.SPEC.module], m.MIGRATIONS, admin_conninfo, PASSWORD)
        yield admin_conninfo, specs
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(dbname)
                )
            )
            for spec in specs.values():
                for role in (spec.app_role, spec.owner_role):
                    admin.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                    )


def connect_as(db, module: str) -> psycopg.Connection:
    admin_conninfo, specs = db
    return psycopg.connect(
        psycopg.conninfo.make_conninfo(
            admin_conninfo, user=specs[module].app_role, password=PASSWORD
        ),
        autocommit=True,
    )


@pytest.mark.parametrize("module", ["core", "gateway"])
def test_other_modules_cannot_read_auth_users(db, module):
    with (
        connect_as(db, module) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT * FROM auth.users")


def test_web_can_write_its_own_table(db):
    with connect_as(db, "web") as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash) VALUES ('a@example.org', 'x')"
        )
        assert conn.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("module", "schema"), [("web", "auth"), ("core", "core"), ("gateway", "gateway")]
)
def test_app_roles_cannot_change_tables(db, module, schema):
    with (
        connect_as(db, module) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            sql.SQL("CREATE TABLE {}.probe (id int)").format(sql.Identifier(schema))
        )


def test_app_roles_cannot_create_in_public(db):
    with (
        connect_as(db, "core") as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("CREATE TABLE public.probe (id int)")


def test_schemas_and_version_tables_belong_to_the_owner_roles(db):
    admin_conninfo, specs = db
    with psycopg.connect(admin_conninfo) as admin:
        for spec in specs.values():
            owner = admin.execute(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s",
                (spec.schema,),
            ).fetchone()[0]
            assert owner == spec.owner_role
            table_owner = admin.execute(
                "SELECT tableowner FROM pg_tables WHERE schemaname = %s AND tablename = 'alembic_version'",
                (spec.schema,),
            ).fetchone()[0]
            assert table_owner == spec.owner_role


def test_pgvector_is_available_to_core(db):
    with connect_as(db, "core") as conn:
        distance = conn.execute(
            "SELECT '[1,2,3]'::vector(3) <-> '[1,2,4]'::vector(3)"
        ).fetchone()[0]
        assert distance == 1.0
