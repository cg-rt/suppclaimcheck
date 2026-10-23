"""Tests for tools/check_map.py. Run: uv run pytest"""

import check_map


def module(module_id, status="planned", folder=None):
    return {
        "id": module_id,
        "status": status,
        "folder": folder or f"services/{module_id}",
    }


def test_planned_module_without_folder_is_fine():
    assert check_map.find_problems({"modules": [module("web")]}, set()) == []


def test_built_module_needs_its_folder():
    problems = check_map.find_problems({"modules": [module("web", "built")]}, set())
    assert problems == ["module web is built, but services/web does not exist"]


def test_unmapped_folder_fails():
    problems = check_map.find_problems(
        {"modules": [module("web")]}, {"services/web", "services/extra"}
    )
    assert problems == [
        "services/extra is not in docs/architecture/architecture.yaml: add it as a module"
    ]


def test_bad_status_folder_and_duplicates_are_reported():
    problems = check_map.find_problems(
        {
            "modules": [
                module("a", "done"),
                module("b", folder="lib/b"),
                module("c", folder="services/a"),
            ]
        },
        {"services/a"},
    )
    assert len(problems) == 3
    assert "status 'done'" in problems[0]
    assert "must be services/<name>" in problems[1]
    assert "already used by module a" in problems[2]


def test_service_folders_skips_files_and_hidden_folders(tmp_path):
    (tmp_path / "services" / "web").mkdir(parents=True)
    (tmp_path / "services" / ".cache").mkdir()
    (tmp_path / "services" / "notes.md").write_text("x\n")
    assert check_map.service_folders(tmp_path) == {"services/web"}


def test_the_real_map_agrees_with_the_repo():
    assert check_map.main() == 0
