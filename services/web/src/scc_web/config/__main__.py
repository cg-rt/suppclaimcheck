"""Config versions, activation and rollback for web (P2.09, D-048). See scc_common.config_cli."""

import sys

from scc_common.config_cli import main
from scc_common.db import conninfo_from_env
from scc_web.config import STORE

if __name__ == "__main__":
    sys.exit(main(STORE, conninfo_from_env(), "python -m scc_web.config"))
