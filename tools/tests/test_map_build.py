"""Tests for tools/map_build.py. Run: uv run pytest"""

import json
import shutil

import pytest

import map_build

SMALL_MAP = {
    "system": {
        "host_label": "Test host",
        "entry": {"client": "Browser", "via": "Tunnel", "module": "edge"},
    },
    "networks": [
        {"id": "internal", "internet": False},
        {"id": "egress", "internet": True},
    ],
    "modules": [
        {
            "id": "edge",
            "role": "way in",
            "containers": ["edge"],
            "networks": ["internal", "egress"],
            "depends_on": ["api"],
            "external_apis": ["Tunnel"],
        },
        {
            "id": "core",
            "role": "logic",
            "containers": ["api", "worker"],
            "networks": ["internal"],
            "depends_on": ["db", "db"],
            "external_apis": ["LLM"],
        },
        {
            "id": "db",
            "kind": "datastore",
            "containers": ["db"],
            "networks": ["internal"],
            "depends_on": [],
        },
    ],
}


def test_mermaid_draws_entry_modules_and_dependencies():
    lines = map_build.mermaid(SMALL_MAP).splitlines()
    assert lines[0] == "flowchart LR"
    assert '  client(["Browser"]) --> entry["Tunnel"]' in lines
    assert '    m_core["core: logic<br/>api + worker"]' in lines
    assert '    m_db[("db")]' in lines
    assert "  entry --> m_edge" in lines
    # A container name resolves to its module; duplicate dependencies are drawn once.
    assert "  m_edge --> m_core" in lines
    assert lines.count("  m_core --> m_db") == 1
    assert '  m_core --> x_core["Outside services<br/>LLM"]' in lines
    # The entry module's outside service is the entry itself, so it gets no extra node.
    assert not any("x_edge" in line for line in lines)
    assert "  class m_edge egress" in lines


def test_unknown_dependency_is_an_error():
    broken = {"modules": [{"id": "a", "depends_on": ["nowhere"]}]}
    with pytest.raises(ValueError, match="unknown dependency 'nowhere'"):
        map_build.mermaid(broken)


def test_labels_escape_double_quotes():
    assert map_build.label('say "hi"') == '"say #quot;hi#quot;"'


def test_readme_block_replaces_only_the_marked_section():
    readme = (
        "# Title\n\n<!-- map:start old -->\nold diagram\n<!-- map:end -->\n\nAfter.\n"
    )
    result = map_build.with_block(readme, map_build.readme_block(SMALL_MAP))
    assert result.startswith("# Title\n\n" + map_build.START)
    assert result.endswith(map_build.END + "\n\nAfter.\n")
    assert "old diagram" not in result
    assert "Only `edge` can reach the internet" in result


def test_readme_without_markers_is_an_error():
    with pytest.raises(ValueError, match="no map:start"):
        map_build.with_block("# Title\n", "block")


def test_data_js_is_loadable_json_behind_one_assignment():
    js = map_build.data_js(SMALL_MAP)
    prefix = "window.ARCHITECTURE_MAP = "
    body = js.splitlines(keepends=True)[1:]
    text = "".join(body)
    assert text.startswith(prefix) and text.endswith(";\n")
    assert json.loads(text[len(prefix) : -2]) == SMALL_MAP


def test_check_reports_a_stale_copy(tmp_path):
    for path in (map_build.MAP_PATH, map_build.README_PATH, map_build.DATA_PATH):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(map_build.ROOT / path, tmp_path / path)
    assert map_build.stale_files(tmp_path) == []
    map_file = tmp_path / map_build.MAP_PATH
    map_file.write_text(
        map_file.read_text(encoding="utf-8").replace(
            "role: site, admin, login", "role: changed"
        ),
        encoding="utf-8",
    )
    assert set(map_build.stale_files(tmp_path)) == {
        map_build.DATA_PATH,
        map_build.README_PATH,
    }


def test_the_committed_files_are_up_to_date():
    assert map_build.stale_files(map_build.ROOT) == []
