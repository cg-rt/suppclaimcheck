"""Checks that the architecture map and the services/ folders agree (P0.3).

Every folder in services/ must be a module in docs/architecture/architecture.yaml, and every
module that is built or live must have its folder. Planned modules may not have one yet.

Run: uv run python tools/check_map.py
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = Path("docs/architecture/architecture.yaml")
STATUSES = {"planned", "built", "live"}


def find_problems(map_data: dict, service_folders: set[str]) -> list[str]:
    """Returns one line per mismatch between the map and the folders. Folders are like 'services/web'."""
    problems = []
    mapped = {}
    for module in map_data.get("modules") or []:
        module_id = module.get("id", "?")
        folder = module.get("folder")
        status = module.get("status")
        if status not in STATUSES:
            problems.append(
                f"module {module_id}: status {status!r} is not one of {sorted(STATUSES)}"
            )
        if not folder or not folder.startswith("services/"):
            problems.append(
                f"module {module_id}: folder {folder!r} must be services/<name>"
            )
            continue
        if folder in mapped:
            problems.append(
                f"module {module_id}: folder {folder} is already used by module {mapped[folder]}"
            )
        mapped[folder] = module_id
        if status in {"built", "live"} and folder not in service_folders:
            problems.append(
                f"module {module_id} is {status}, but {folder} does not exist"
            )
    for folder in sorted(service_folders - mapped.keys()):
        problems.append(f"{folder} is not in {MAP_PATH.as_posix()}: add it as a module")
    return problems


def service_folders(root: Path) -> set[str]:
    services = root / "services"
    if not services.is_dir():
        return set()
    return {
        f"services/{p.name}"
        for p in services.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }


def main() -> int:
    map_data = yaml.safe_load((ROOT / MAP_PATH).read_text(encoding="utf-8"))
    problems = find_problems(map_data, service_folders(ROOT))
    for problem in problems:
        print(f"check_map: {problem}")
    if not problems:
        print("check_map: the map and services/ agree.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
