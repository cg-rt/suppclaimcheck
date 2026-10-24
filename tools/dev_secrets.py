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
    # Not a Docker secret: the password of the local test account that tools/dev_user.py creates.
    "web_tester_password",
]
# Compose needs every declared secret file to exist. These start as placeholders: the tunnel isn't used
# in dev, and an empty API key file means "not configured" (LLM calls fail cleanly, PubMed runs at 3/s).
# To use a real key in dev, paste it into the file yourself; this script never overwrites a file.
PLACEHOLDERS = {
    "tunnel_token": "not-used-in-dev",
    "anthropic_api_key": "",
    "ncbi_api_key": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}


def main() -> int:
    FOLDER.mkdir(parents=True, exist_ok=True)
    created = []
    for name in NAMES + list(PLACEHOLDERS):
        path = FOLDER / name
        if path.exists():
            continue
        value = (
            PLACEHOLDERS[name] if name in PLACEHOLDERS else secrets.token_urlsafe(32)
        )
        path.write_text(value + "\n", encoding="utf-8")
        created.append(name)
    print(
        f"dev secrets: created {len(created)} ({', '.join(created) or 'none'}), kept {len(NAMES) + len(PLACEHOLDERS) - len(created)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
