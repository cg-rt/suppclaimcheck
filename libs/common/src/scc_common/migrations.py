"""Per-module Alembic migrations, run as a one-off step before the module starts (P1.03).

Each module keeps its own `migrations/` folder and version table in its own schema. The step connects as
the database superuser, runs `bootstrap()`, then switches to the module's owner role with `SET ROLE`, so
everything a migration creates belongs to that role and the app role gets its grants automatically.
"""

import logging
import os
from pathlib import Path

import psycopg
import sqlalchemy as sa
from alembic import command, context
from alembic.config import Config

from scc_common.db import ModuleDb, bootstrap, read_secret

log = logging.getLogger("scc.migrate")


def admin_conninfo_from_env() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("SCC_DB_HOST", "postgres"),
        port=os.environ.get("SCC_DB_PORT", "5432"),
        dbname=os.environ.get("SCC_DB_NAME", "scc"),
        user=os.environ.get("SCC_DB_ADMIN_USER", "postgres"),
        password=read_secret(os.environ["SCC_DB_ADMIN_PASSWORD_FILE"]),
        connect_timeout=5,
    )


def migrate(
    spec: ModuleDb, migrations_dir: Path, admin_conninfo: str, app_password: str
) -> None:
    """Bootstraps the module's roles and schema, then upgrades its tables to the latest migration."""
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        bootstrap(admin, spec, app_password)
    log.info("bootstrap done", extra={"db_module": spec.module, "schema": spec.schema})

    engine = sa.create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(admin_conninfo),
        poolclass=sa.NullPool,
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'SET ROLE "{spec.owner_role}"')
            config = Config()
            config.set_main_option("script_location", str(migrations_dir))
            config.attributes["connection"] = connection
            config.attributes["spec"] = spec
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    log.info("migrations at head", extra={"db_module": spec.module})


def run_env() -> None:
    """The body of every module's `migrations/env.py`."""
    connection = context.config.attributes["connection"]
    spec: ModuleDb = context.config.attributes["spec"]
    context.configure(
        connection=connection, target_metadata=None, version_table_schema=spec.schema
    )
    with context.begin_transaction():
        context.run_migrations()
