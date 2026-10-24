"""Database roles and schemas (P1.03): each module reaches only its own schema."""

import psycopg
import pytest
from psycopg import sql


def connect_as(db, module: str) -> psycopg.Connection:
    return psycopg.connect(db.conninfo(module), autocommit=True)


@pytest.mark.parametrize("module", ["core", "gateway"])
def test_other_modules_cannot_read_auth_users(migrated_db, module):
    with (
        connect_as(migrated_db, module) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT * FROM auth.users")


@pytest.mark.parametrize("module", ["web", "gateway"])
def test_other_modules_cannot_read_core_jobs(migrated_db, module):
    with (
        connect_as(migrated_db, module) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT * FROM core.jobs")


def test_web_can_write_its_own_table(migrated_db):
    with connect_as(migrated_db, "web") as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash) VALUES ('a@example.org', 'x')"
        )
        assert conn.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("module", "schema"), [("web", "auth"), ("core", "core"), ("gateway", "gateway")]
)
def test_app_roles_cannot_change_tables(migrated_db, module, schema):
    with (
        connect_as(migrated_db, module) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            sql.SQL("CREATE TABLE {}.probe (id int)").format(sql.Identifier(schema))
        )


def test_app_roles_cannot_create_in_public(migrated_db):
    with (
        connect_as(migrated_db, "core") as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("CREATE TABLE public.probe (id int)")


def test_schemas_and_tables_belong_to_the_owner_roles(migrated_db):
    with psycopg.connect(migrated_db.admin_conninfo) as admin:
        for spec in migrated_db.specs.values():
            owner = admin.execute(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s",
                (spec.schema,),
            ).fetchone()[0]
            assert owner == spec.owner_role
            owners = {
                row[0]
                for row in admin.execute(
                    "SELECT tableowner FROM pg_tables WHERE schemaname = %s",
                    (spec.schema,),
                )
            }
            assert owners == {spec.owner_role}


def test_procrastinate_queue_lives_in_core(migrated_db):
    with psycopg.connect(migrated_db.admin_conninfo) as admin:
        schema = admin.execute(
            "SELECT schemaname FROM pg_tables WHERE tablename = 'procrastinate_jobs'"
        ).fetchone()[0]
    assert schema == "core"


def test_pgvector_is_available_to_core(migrated_db):
    with connect_as(migrated_db, "core") as conn:
        distance = conn.execute(
            "SELECT '[1,2,3]'::vector(3) <-> '[1,2,4]'::vector(3)"
        ).fetchone()[0]
        assert distance == 1.0
