"""Versioned config store (P2.09, D-048): audit, rollback, append-only history, eval warnings.

Every test works on the module database from `migrated_db`, as the module's own role (what the CLI
uses), with version names of its own, so the tests don't depend on each other's order.
"""

import dataclasses
import uuid

import psycopg
import pytest

from scc_common import config_cli, config_store
from scc_common.config_store import ConfigStoreError
from scc_core import config as core_config
from scc_gateway import config as gateway_config
from scc_web import config as web_config

CORE = core_config.STORE
SHIPPED_TOML = (CORE.folder / f"{CORE.shipped}.toml").read_text("utf-8")
ALL_MET = {"verdict_accuracy": True, "latency_p95_s": True}


def connect(db, module: str) -> psycopg.Connection:
    return psycopg.connect(db.conninfo(module), autocommit=True)


def variant(name: str, top_k: int = 5) -> str:
    """The shipped pipeline config under another name, with one setting changed."""
    text = SHIPPED_TOML.replace(f'version = "{CORE.shipped}"', f'version = "{name}"')
    assert "top_k = 6" in text
    return text.replace("top_k = 6", f"top_k = {top_k}")


def unique(label: str) -> str:
    return f"{CORE.shipped}-{label}-{uuid.uuid4().hex[:6]}"


def run_file(conn, version: str, met: dict, **meta) -> dict:
    body = conn.execute(
        "SELECT body FROM core.config_versions WHERE version = %s", (version,)
    ).fetchone()[0]
    return {
        "run": {
            "run_id": f"run-{uuid.uuid4().hex[:6]}",
            "split": "tuning",
            "date": "2026-11-02",
            "partial": False,
            "overrides": [],
            "config_sha256": config_store.content_hash(body),
            **meta,
        },
        "summary": {"passed": 11.0, "cases": 22, "thresholds_met": met},
    }


@pytest.mark.parametrize(
    ("module", "store"),
    [("core", CORE), ("gateway", gateway_config.STORE), ("web", web_config.STORE)],
)
def test_migrate_registers_the_repo_files_and_activates_the_shipped_default(
    migrated_db, module, store
):
    with connect(migrated_db, module) as conn:
        names = [row[0] for row in config_store.versions(conn, store)]
        files = sorted(p.stem for p in store.folder.glob(f"{store.prefix}v*.toml"))
        assert sorted(n for n in names if store.is_repo_name(n)) == files
        assert config_store.load_active(conn, store)["version"] == store.shipped
        actions = {row[2] for row in config_store.history(conn, store, limit=500)}
        assert {"register", "activate"} <= actions


@pytest.mark.parametrize("table", ["config_versions", "config_audit", "config_evals"])
def test_history_is_append_only_even_for_the_modules_own_role(migrated_db, table):
    name = unique("frozen")
    with connect(migrated_db, "core") as conn:
        # A row in every table, so the row triggers have something to refuse.
        top_k = 12 + ["config_versions", "config_audit", "config_evals"].index(table)
        config_store.create(conn, CORE, variant(name, top_k), "owner", "x")
        config_store.attach_eval(
            conn, CORE, name, run_file(conn, name, ALL_MET), "owner"
        )
        column = "split" if table == "config_evals" else "reason"
        for statement in (
            f"UPDATE core.{table} SET {column} = 'x'",
            f"DELETE FROM core.{table}",
            f"TRUNCATE core.{table}",
        ):
            with pytest.raises(psycopg.Error, match="append-only|permission denied"):
                conn.execute(statement)


def test_create_records_who_why_and_the_diff_and_does_not_activate(migrated_db):
    name = unique("k5")
    with connect(migrated_db, "core") as conn:
        before = config_store.active_version(conn, CORE)
        config_store.create(conn, CORE, variant(name), "owner", "try K = 5")
        assert config_store.active_version(conn, CORE) == before
        _, actor, action, version, _, reason = config_store.history(
            conn, CORE, limit=1
        )[0]
        assert (actor, action, version, reason) == (
            "owner",
            "create",
            name,
            "try K = 5",
        )
        diff = conn.execute(
            "SELECT diff FROM core.config_audit WHERE version = %s AND action = 'create'",
            (name,),
        ).fetchone()[0]
        assert "-top_k = 6" in diff and "+top_k = 5" in diff


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace("top_k = 5", 'top_k = "five"'), "retrieval.top_k is str"),
        (lambda t: t.replace("rrf_k = 60\n", ""), "missing retrieval.rrf_k"),
        (lambda t: t + "\n[new_section]\nx = 1\n", "unknown new_section"),
    ],
)
def test_create_refuses_a_version_with_different_settings(migrated_db, change, message):
    name = unique("bad")
    with (
        connect(migrated_db, "core") as conn,
        pytest.raises(ConfigStoreError, match=message),
    ):
        config_store.create(conn, CORE, change(variant(name)), "owner", "x")


def test_create_refuses_repo_names_duplicates_and_identical_content(migrated_db):
    with connect(migrated_db, "core") as conn:
        with pytest.raises(ConfigStoreError, match="repo version name"):
            config_store.create(conn, CORE, variant("pipeline-v99"), "owner", "x")
        with pytest.raises(ConfigStoreError, match="same content as pipeline-v4"):
            config_store.create(
                conn, CORE, variant(unique("same"), top_k=6), "owner", "x"
            )
        name = unique("dup")
        config_store.create(conn, CORE, variant(name, top_k=4), "owner", "x")
        with pytest.raises(ConfigStoreError, match="already exists"):
            config_store.create(conn, CORE, variant(name, top_k=3), "owner", "x")


def test_activating_without_an_eval_needs_a_reason_and_rollback_restores(migrated_db):
    name = unique("act")
    with connect(migrated_db, "core") as conn:
        config_store.create(conn, CORE, variant(name, top_k=7), "owner", "x")
        before = config_store.active_version(conn, CORE)
        with pytest.raises(ConfigStoreError, match="no eval result"):
            config_store.activate(conn, CORE, name, "owner", "try it")
        assert config_store.active_version(conn, CORE) == before

        previous, accepted = config_store.activate(
            conn, CORE, name, "owner", "try it", accept_warnings="dev check only"
        )
        assert previous == before and accepted
        assert config_store.active_version(conn, CORE) == name
        reason = config_store.history(conn, CORE, limit=1)[0][5]
        assert "no eval result" in reason and "dev check only" in reason

        # Going back needs no --accept-warnings: the earlier version was live until now.
        was, now = config_store.rollback(conn, CORE, "owner", "worse answers")
        assert (was, now) == (name, before)
        assert config_store.active_version(conn, CORE) == before
        _, _, action, version, previous, reason = config_store.history(
            conn, CORE, limit=1
        )[0]
        assert (action, version, previous) == ("activate", before, name)
        assert reason.startswith("rollback: worse answers")


def test_an_attached_eval_clears_or_states_the_warning(migrated_db):
    good, weak = unique("good"), unique("weak")
    with connect(migrated_db, "core") as conn:
        config_store.create(conn, CORE, variant(good, top_k=8), "owner", "x")
        config_store.create(conn, CORE, variant(weak, top_k=9), "owner", "x")
        config_store.attach_eval(
            conn, CORE, good, run_file(conn, good, ALL_MET), "owner"
        )
        missed = {"verdict_accuracy": False, "latency_p95_s": True}
        config_store.attach_eval(
            conn, CORE, weak, run_file(conn, weak, missed), "owner"
        )

        assert config_store.warnings(conn, CORE, good) == []
        assert "missed: verdict_accuracy" in config_store.warnings(conn, CORE, weak)[0]
        before = config_store.active_version(conn, CORE)
        config_store.activate(conn, CORE, good, "owner", "eval passed")
        config_store.activate(
            conn, CORE, before, "owner", "back", accept_warnings="test"
        )


def test_attach_eval_refuses_runs_of_other_settings(migrated_db):
    name = unique("eval")
    with connect(migrated_db, "core") as conn:
        config_store.create(conn, CORE, variant(name, top_k=10), "owner", "x")
        other = run_file(conn, CORE.shipped, ALL_MET)
        with pytest.raises(ConfigStoreError, match="different settings"):
            config_store.attach_eval(conn, CORE, name, other, "owner")
        overridden = run_file(conn, name, ALL_MET, overrides=["retrieval.top_k=3"])
        with pytest.raises(ConfigStoreError, match="without --set"):
            config_store.attach_eval(conn, CORE, name, overridden, "owner")
        partial = run_file(conn, name, ALL_MET, partial=True)
        with pytest.raises(ConfigStoreError, match="only full runs"):
            config_store.attach_eval(conn, CORE, name, partial, "owner")


def test_gateway_and_web_activation_has_no_eval_warning_and_says_restart(migrated_db):
    store = gateway_config.STORE
    toml = (store.folder / f"{store.shipped}.toml").read_text("utf-8")
    name = f"{store.shipped}-lower-timeout-{uuid.uuid4().hex[:6]}"
    text = toml.replace(f'version = "{store.shipped}"', f'version = "{name}"')
    text = text.replace(
        "timeout_s = ", "timeout_s = 1 + ", 1
    )  # invalid TOML on purpose
    with connect(migrated_db, "gateway") as conn:
        with pytest.raises(ConfigStoreError, match="not valid TOML"):
            config_store.create(conn, store, text, "owner", "x")
        body = toml.replace(f'version = "{store.shipped}"', f'version = "{name}"')
        body = body.replace("[alerts]", "[alerts]\n", 1)  # same settings: refused
        with pytest.raises(ConfigStoreError, match="same content"):
            config_store.create(conn, store, body, "owner", "x")
        changed = _bump_first_number(body, "timeout_s")
        config_store.create(conn, store, changed, "owner", "shorter LLM timeout")
        args = config_cli._parser("x").parse_args(
            ["activate", name, "--by", "owner", "--reason", "test"]
        )
        out = config_cli.run(store, conn, args)
        assert "Restart gateway to apply it" in out
        config_cli.run(
            store,
            conn,
            config_cli._parser("x").parse_args(
                ["rollback", "--by", "owner", "--reason", "test done"]
            ),
        )
        assert config_store.active_version(conn, store) == store.shipped


def _bump_first_number(text: str, key: str) -> str:
    start = text.index(f"{key} = ") + len(f"{key} = ")
    end = start
    while text[end].isdigit() or text[end] == ".":
        end += 1
    value = text[start:end]
    bumped = str(float(value) + 1) if "." in value else str(int(value) + 1)
    return text[:start] + bumped + text[end:]


def test_a_deploy_does_not_undo_an_activation_or_rollback(migrated_db):
    name = unique("keep")
    admin = migrated_db.admin_conninfo
    owner = migrated_db.specs["core"].owner_role
    with connect(migrated_db, "core") as conn:
        config_store.create(conn, CORE, variant(name, top_k=11), "owner", "x")
        before = config_store.active_version(conn, CORE)
        config_store.activate(conn, CORE, name, "owner", "x", accept_warnings="test")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'SET ROLE "{owner}"')
        assert config_store.sync_repo_versions(conn, CORE, "9.9.9") == []
        assert config_store.active_version(conn, CORE) == name
    with connect(migrated_db, "core") as conn:
        config_store.activate(conn, CORE, before, "owner", "x", accept_warnings="test")


def test_a_changed_repo_file_stops_the_deploy(migrated_db, tmp_path):
    for path in CORE.folder.glob("pipeline-v*.toml"):
        (tmp_path / path.name).write_text(path.read_text("utf-8"), "utf-8")
    shipped = tmp_path / f"{CORE.shipped}.toml"
    shipped.write_text(shipped.read_text("utf-8").replace("top_k = 6", "top_k = 2"))
    store = dataclasses.replace(CORE, folder=tmp_path)
    owner = migrated_db.specs["core"].owner_role
    with psycopg.connect(migrated_db.admin_conninfo, autocommit=True) as conn:
        conn.execute(f'SET ROLE "{owner}"')
        with pytest.raises(ConfigStoreError, match="a version never changes"):
            config_store.sync_repo_versions(conn, store, "9.9.9")


def test_cli_list_marks_the_active_version_and_its_eval(migrated_db):
    with connect(migrated_db, "core") as conn:
        out = config_cli.run(CORE, conn, config_cli._parser("x").parse_args(["list"]))
    active = [line for line in out.splitlines() if line.startswith("*")]
    assert len(active) == 1 and CORE.shipped in active[0]
    assert "[no eval]" in active[0]
