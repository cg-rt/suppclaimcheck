"""The compose files keep the network and secret rules (P1.02, D-027)."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = {name: ROOT / "deploy" / f"compose.{name}.yaml" for name in ("dev", "prod")}
EGRESS_ALLOWED = {"edge", "gateway"}
MIGRATE = {"web-migrate", "core-migrate", "gateway-migrate"}


def load(name: str) -> dict:
    return yaml.safe_load(FILES[name].read_text(encoding="utf-8"))


@pytest.fixture(params=["dev", "prod"])
def compose(request):
    return load(request.param)


def test_internal_network_has_no_internet(compose):
    assert compose["networks"]["internal"] == {"internal": True}


def test_only_edge_and_gateway_reach_outside(compose):
    for name, service in compose["services"].items():
        if "egress" in service["networks"]:
            assert name in EGRESS_ALLOWED, f"{name} must not join the egress network"
        assert "internal" in service["networks"], (
            f"{name} must be on the internal network"
        )


def test_core_worker_and_postgres_are_internal_only(compose):
    for name in ("core-worker", "postgres"):
        assert compose["services"][name]["networks"] == ["internal"]


def test_prod_publishes_no_ports_and_has_no_extra_networks():
    data = load("prod")
    assert set(data["networks"]) == {"internal", "egress"}
    for name, service in data["services"].items():
        assert "ports" not in service, f"{name} publishes a port in production"


def test_dev_ports_bind_to_localhost_and_avoid_the_other_stack():
    for name, service in load("dev")["services"].items():
        for port in service.get("ports", []):
            host, host_port, _ = port.split(":")
            assert host == "127.0.0.1", f"{name}: {port}"
            assert host_port not in {"5432", "5678"}, f"{name}: {port}"


def test_long_running_services_restart_and_migrate_steps_do_not(compose):
    for name, service in compose["services"].items():
        expected = "no" if name in MIGRATE else "unless-stopped"
        assert service.get("restart", "no") == expected, name


def test_secrets_are_files_never_environment_values(compose):
    for name, service in compose["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if "PASSWORD" in key or "TOKEN" in key:
                assert key.endswith("_FILE"), f"{name}: {key}"
                assert str(value).startswith("/run/secrets/"), f"{name}: {key}"
    for secret in compose["secrets"].values():
        assert set(secret) == {"file"}


def test_prod_images_come_from_ghcr_at_one_version():
    for name, service in load("prod")["services"].items():
        assert service["image"].startswith("ghcr.io/cg-rt/suppclaimcheck-"), name
        assert service["image"].endswith(":${SCC_VERSION:?set SCC_VERSION}"), name
