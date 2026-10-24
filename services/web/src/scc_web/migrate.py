"""One-off migrate step: `python -m scc_web.migrate` (P1.03). Needs the superuser and the web app secrets."""

import os
from pathlib import Path

from scc_common.db import ModuleDb, read_secret
from scc_common.logs import configure_logging
from scc_common.migrations import admin_conninfo_from_env, migrate

SPEC = ModuleDb(module="web", schema="auth")
MIGRATIONS = Path(__file__).parent / "migrations"


def main() -> None:
    configure_logging("web-migrate")
    migrate(
        SPEC,
        MIGRATIONS,
        admin_conninfo_from_env(),
        read_secret(os.environ["SCC_DB_PASSWORD_FILE"]),
    )


if __name__ == "__main__":
    main()
