"""Creates the local test account in the running dev stack (P1.04), through the web admin CLI.

The password is the one tools/dev_secrets.py generated; it goes to the CLI on stdin and is never printed.
Sign in at http://127.0.0.1:18080 as tester@example.com with that password.

Run: uv run python tools/dev_user.py
"""

import subprocess
import sys

from dev_secrets import FOLDER

EMAIL = "tester@example.com"


def main() -> int:
    password = (FOLDER / "web_tester_password").read_text(encoding="utf-8").strip()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/compose.dev.yaml",
            "exec",
            "-T",
            "web",
            "python",
            "-m",
            "scc_web.admin",
            "create-user",
            EMAIL,
        ],
        check=False,
        input=password + "\n",
        text=True,
        capture_output=True,
    )
    print((result.stdout or result.stderr).strip())
    return (
        0
        if result.returncode == 0 or "already exists" in result.stderr
        else result.returncode
    )


if __name__ == "__main__":
    sys.exit(main())
