"""Creates random passwords for the development stack (P1.02), one file per Docker secret.

Existing files are left alone, so running it again is safe. The folder is in .gitignore and the values
are for the local dev stack only; production secrets live on the VM (AGENTS.md section 10).
The values are never printed.

Run: uv run python tools/dev_secrets.py
"""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "secrets" / "dev"
NAMES = [
    "postgres_superuser_password",
    "db_web_password",
    "db_core_password",
    "db_gateway_password",
]
# Compose needs every declared secret file to exist; the tunnel isn't used in dev, so this one stays a placeholder.
PLACEHOLDERS = ["tunnel_token"]


def main() -> int:
    FOLDER.mkdir(parents=True, exist_ok=True)
    created = []
    for name in NAMES + PLACEHOLDERS:
        path = FOLDER / name
        if path.exists():
            continue
        value = "not-used-in-dev" if name in PLACEHOLDERS else secrets.token_urlsafe(32)
        path.write_text(value + "\n", encoding="utf-8")
        created.append(name)
    print(
        f"dev secrets: created {len(created)} ({', '.join(created) or 'none'}), kept {len(NAMES) + len(PLACEHOLDERS) - len(created)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
