"""Sets up a deploy rehearsal on Docker Desktop (P1.12, RB-13): production's compose file and scripts, local images.

Run: uv run python tools/rehearse_deploy.py
Then, from .rehearsal/ in Git Bash, run the scripts as on the VM (see docs/RUNBOOK.md, RB-13 rehearsal):
    SCC_PROJECT=scc-rehearsal SCC_COMPOSE_EXTRA=compose.rehearsal.yaml SCC_SKIP_PULL=1 ./deploy.sh 0.1.0 --no-claim

What it prepares, in the git-ignored .rehearsal/ folder:
- copies of deploy/compose.prod.yaml, deploy.sh, rollback.sh, notify.sh and backup scripts, and an empty versions.env;
- secret files copied from the dev stack's folder (never printed), a fresh smoke password, a placeholder tunnel token;
- a local restic repository (.rehearsal/restic-repo) with its own random password, in place of R2;
- compose.rehearsal.yaml, which leaves edge out (no tunnel in dev);
- local images tagged as GHCR images for three versions: 0.1.0 and 0.1.1 (real builds whose /version says so),
  and 0.1.2, which is the 0.1.1 images under a wrong tag, so its smoke test fails on purpose.
"""

import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from dev_secrets import FOLDER as DEV_SECRETS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".rehearsal"
REGISTRY = "ghcr.io/cg-rt/suppclaimcheck"
PYTHON_MODULES = ["web", "core", "gateway"]
SCRIPTS = [
    "compose.prod.yaml",
    "deploy.sh",
    "rollback.sh",
    "notify.sh",
    "backup.sh",
    "restore.sh",
    "disk-check.sh",
]
COPIED_SECRETS = [
    "postgres_superuser_password", "db_web_password", "db_core_password", "db_gateway_password",
    "anthropic_api_key", "ncbi_api_key", "telegram_bot_token", "telegram_chat_id",
]  # fmt: skip
OVERRIDE = """# Rehearsal only (tools/rehearse_deploy.py): no tunnel in dev, so edge is left out.
services:
  edge:
    profiles: [edge]
"""


def sh(*args: str) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def build(version: str) -> None:
    """Builds the Python images with VERSION set to `version`, then puts the real VERSION back."""
    version_file = ROOT / "VERSION"
    original = version_file.read_bytes()
    try:
        version_file.write_text(version + "\n", encoding="utf-8", newline="\n")
        for module in PYTHON_MODULES:
            sh("docker", "build", "--quiet", "-f", f"services/{module}/Dockerfile",
               "--build-arg", f"GIT_SHA=rehearsal-{version}", "-t", f"{REGISTRY}-{module}:{version}", ".")  # fmt: skip
    finally:
        version_file.write_bytes(original)
    for module in ("postgres", "edge"):
        sh(
            "docker",
            "build",
            "--quiet",
            "-t",
            f"{REGISTRY}-{module}:{version}",
            f"services/{module}",
        )


def main() -> int:
    (OUT / "secrets").mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        source = ROOT / "deploy" / name
        if source.exists():
            shutil.copy2(source, OUT / name)
    (OUT / "compose.rehearsal.yaml").write_text(
        OVERRIDE, encoding="utf-8", newline="\n"
    )
    if not (OUT / "versions.env").exists():
        (OUT / "versions.env").write_text(
            "SCC_VERSION=\nSCC_PREVIOUS_VERSION=\n", encoding="utf-8", newline="\n"
        )
    for name in COPIED_SECRETS:
        shutil.copy2(DEV_SECRETS / name, OUT / "secrets" / name)
    smoke = OUT / "secrets" / "smoke_password"
    if not smoke.exists():
        smoke.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
    (OUT / "secrets" / "tunnel_token").write_text(
        "not-used-in-rehearsal\n", encoding="utf-8"
    )
    restic_env = OUT / "secrets" / "restic.env"
    if not restic_env.exists():
        restic_env.write_text(
            f"RESTIC_REPOSITORY={(OUT / 'restic-repo').as_posix()}\n", encoding="utf-8"
        )
        (OUT / "secrets" / "restic_password").write_text(
            secrets.token_urlsafe(24) + "\n", encoding="utf-8"
        )

    build("0.1.0")
    build("0.1.1")
    for module in [*PYTHON_MODULES, "postgres", "edge"]:
        sh("docker", "tag", f"{REGISTRY}-{module}:0.1.1", f"{REGISTRY}-{module}:0.1.2")
    print(
        f"rehearsal ready in {OUT.relative_to(ROOT)}: versions 0.1.0, 0.1.1 and a broken 0.1.2"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
