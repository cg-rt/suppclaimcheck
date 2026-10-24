"""A fresh, fully migrated database for tests that need PostgreSQL.

Needs a PostgreSQL 18 + pgvector server and a superuser connection string in SCC_TEST_ADMIN_DSN.
CI provides one; locally, see "Working on it" in README.md. Each test module gets its own database
and role names with a random prefix, so tests never touch the dev stack's roles. Without the
variable the tests are skipped, except in CI, where that is an error.
"""

import dataclasses
import os
import uuid
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql

from scc_common.db import ModuleDb
from scc_common.migrations import migrate
from scc_core import migrate as core_migrate
from scc_gateway import migrate as gateway_migrate
from scc_web import migrate as web_migrate

ADMIN_DSN = os.environ.get("SCC_TEST_ADMIN_DSN")
MODULES = [web_migrate, core_migrate, gateway_migrate]
PASSWORD = "test-only"


@dataclass
class TestDb:
    admin_conninfo: str
    specs: dict[str, ModuleDb]

    def conninfo(self, module: str) -> str:
        """Connection string for a module's app role, the login the module itself uses."""
        return psycopg.conninfo.make_conninfo(
            self.admin_conninfo, user=self.specs[module].app_role, password=PASSWORD
        )


def require_db() -> None:
    if ADMIN_DSN:
        return
    if os.environ.get("CI"):
        raise RuntimeError(
            "SCC_TEST_ADMIN_DSN must be set in CI: the database tests may not be skipped there"
        )
    pytest.skip("SCC_TEST_ADMIN_DSN not set (no test database)")


@pytest.fixture(scope="module")
def migrated_db():
    require_db()
    suffix = uuid.uuid4().hex[:8]
    dbname = f"scc_test_{suffix}"
    specs = {
        m.SPEC.module: dataclasses.replace(m.SPEC, prefix=f"t{suffix}_")
        for m in MODULES
    }
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    admin_conninfo = psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=dbname)
    try:
        # Twice: the migrate step runs on every deploy, so it must be safe to repeat.
        for _ in range(2):
            for m in MODULES:
                migrate(specs[m.SPEC.module], m.MIGRATIONS, admin_conninfo, PASSWORD)
        yield TestDb(admin_conninfo, specs)
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
