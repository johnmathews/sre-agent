"""Tests for the Upgrade Advisor settings gate and allowlist loader."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.upgrades.config import AdvisedContainer, is_advisor_configured, load_allowlist

CONTRACT_YAML = """\
containers:
  - name: prometheus
    host: prometheus
    wud_url: http://10.0.0.15:3010
    registry: registry-1.docker.io
    repository: prom/prometheus
    tag: v3
    github_repo: prometheus/prometheus
    release_tag_format: "{release}"
    readiness_url: http://10.0.0.15:9090/-/ready
    pbs_guest: "CT 901"
    context_files:
      - /app/upgrades/context/prometheus/docker-compose.yml.j2
      - /app/upgrades/context/prometheus/prometheus.yml.j2
    auto_deploy: false
"""


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "allowlist.yml"
    path.write_text(text)
    return str(path)


def test_load_allowlist_parses_contract_example(tmp_path: Path) -> None:
    items = load_allowlist(_write(tmp_path, CONTRACT_YAML))
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, AdvisedContainer)
    assert item.name == "prometheus"
    assert item.tag == "v3"
    assert item.release_tag_format == "{release}"
    assert item.pbs_guest == "CT 901"
    assert len(item.context_files) == 2
    assert item.auto_deploy is False


def test_load_allowlist_rejects_missing_fields(tmp_path: Path) -> None:
    text = CONTRACT_YAML.replace("    wud_url: http://10.0.0.15:3010\n", "")
    with pytest.raises(ValidationError):
        load_allowlist(_write(tmp_path, text))


def test_auto_deploy_true_is_rejected_in_phase_1(tmp_path: Path) -> None:
    text = CONTRACT_YAML.replace("auto_deploy: false", "auto_deploy: true")
    with pytest.raises(ValueError, match="auto_deploy is not supported yet"):
        load_allowlist(_write(tmp_path, text))


def test_release_tag_format_defaults(tmp_path: Path) -> None:
    text = CONTRACT_YAML.replace('    release_tag_format: "{release}"\n', "")
    assert load_allowlist(_write(tmp_path, text))[0].release_tag_format == "{release}"


def test_load_allowlist_rejects_missing_containers_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="containers"):
        load_allowlist(_write(tmp_path, "other: []\n"))


def test_advisor_disabled_by_default(mock_settings: Any) -> None:
    assert is_advisor_configured() is False


def test_advisor_configured_when_all_set(mock_settings: Any) -> None:
    mock_settings.upgrade_advisor_config_path = "/x/allowlist.yml"
    mock_settings.upgrade_advisor_db_path = "/x/advisor.db"
    mock_settings.upgrade_advisor_wud_username = "user"
    mock_settings.upgrade_advisor_wud_password = "pw"
    mock_settings.upgrade_advisor_public_base_url = "https://sre.example.com/api"
    assert is_advisor_configured() is True
    mock_settings.upgrade_advisor_wud_password = ""
    assert is_advisor_configured() is False


def _with(**changes: Any) -> str:
    """CONTRACT_YAML with the given top-level keys replaced (or added) in the single entry."""
    data = yaml.safe_load(CONTRACT_YAML)
    data["containers"][0].update(changes)
    return yaml.safe_dump(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown_key", "x"),
        ("context_files", ["/etc/passwd"]),
        ("context_files", ["/app/upgrades/context/../x"]),
        ("context_files", ["relative/path.yml"]),
        ("context_files", ["/app/upgrades/context", "/app/upgrades/context/"]),
        ("context_files", ["/app/upgrades/contextual/x"]),
        ("wud_url", "ftp://x"),
        ("wud_url", "not a url"),
        ("wud_url", "http://u:p@10.0.0.15:3010"),
        ("wud_url", "http://10.0.0.15:3010/x"),
        ("wud_url", "http://10.0.0.15:3010?a=1"),
        ("wud_url", "http://10.0.0.15:3010#f"),
        ("readiness_url", "http://u:p@10.0.0.19:3000/health"),
        ("readiness_url", "http://10.0.0.19:3000/health?x=1"),
        ("readiness_url", "http://10.0.0.19:3000/health#f"),
        ("wud_url", " http://10.0.0.15:3010"),
        ("readiness_url", "http://10.0.0.15:9090/ \n"),
        ("readiness_url", "file:///etc/passwd"),
        ("name", "Bad Name"),
        ("name", "-lead"),
        ("host", "a/b"),
        ("registry", "reg istry"),
        ("registry", "reg/istry"),
        ("repository", "Prom/Prometheus"),
        ("repository", "prom//prometheus"),
        ("tag", "-v3"),
        ("tag", "v3 x"),
        ("github_repo", "prometheus"),
        ("github_repo", "a/b/c"),
        ("pbs_guest", "CT115"),
        ("pbs_guest", "LXC 115"),
        ("release_tag_format", "v1"),
        ("release_tag_format", "{release}-{other}"),
        ("release_tag_format", "{release.__class__}"),
        ("release_tag_format", "{"),
        ("release_tag_format", "{release:{x}}"),
    ],
)
def test_allowlist_rejects_invalid_value(tmp_path: Path, field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        load_allowlist(_write(tmp_path, _with(**{field: value})))


def test_allowlist_accepts_url_shapes(tmp_path: Path) -> None:
    for wud in ("http://10.0.0.15:3010", "http://10.0.0.15:3010/", "https://wud.example.com"):
        item = load_allowlist(_write(tmp_path, _with(wud_url=wud)))[0]
        assert item.wud_url == wud
    item = load_allowlist(_write(tmp_path, _with(readiness_url="http://10.0.0.19:3000/health")))[0]
    assert item.readiness_url == "http://10.0.0.19:3000/health"


def test_allowlist_accepts_registry_port_and_release_prefix(tmp_path: Path) -> None:
    text = _with(registry="ghcr.example.com:5000", release_tag_format="v{release}")
    item = load_allowlist(_write(tmp_path, text))[0]
    assert item.registry == "ghcr.example.com:5000"
    assert item.release_tag_format == "v{release}"
    assert item.wud_url == "http://10.0.0.15:3010"
