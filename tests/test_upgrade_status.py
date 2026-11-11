"""Unit tests for the upgrade_status tool: input bounds, exporter row joining and the deploy-safety text scan."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agent.tools.upgrade_status import (
    ExporterImage,
    UpgradeStatusInput,
    exporter_images,
    format_exporter_lines,
)

MODULE = Path(__file__).resolve().parents[1] / "src" / "agent" / "tools" / "upgrade_status.py"

# Names of the deploy stack; the same set test_upgrades_api_integration.py keeps out of the agent and MCP surface.
FORBIDDEN_TEXT = (
    "src.upgrades.deploy",
    "src.upgrades.wud",
    "src.upgrades.verify",
    "src.upgrades.job",
    "src.api.upgrades",
    "deploy_approved",
    "trigger_deploy",
)


def test_module_text_names_no_deploy_code() -> None:
    """The tool reads the advisor DB; its source never names the deploy stack, even in a comment."""
    text = MODULE.read_text()
    for name in FORBIDDEN_TEXT:
        assert name not in text, f"upgrade_status.py mentions {name}"
    assert "from src.upgrades import store" in text  # the one advisor module it uses


@pytest.mark.parametrize("container", ["homepage", "tube-archivist", "a.b_c-1"])
def test_input_accepts_container_names(container: str) -> None:
    assert UpgradeStatusInput(container=container).container == container


@pytest.mark.parametrize("container", ["", "home page", 'x"}', "a" * 129, "x|y"])
def test_input_rejects_bad_container(container: str) -> None:
    with pytest.raises(ValidationError):
        UpgradeStatusInput(container=container)


@pytest.mark.parametrize(("days", "ok"), [(1, True), (365, True), (0, False), (366, False)])
def test_input_days_bounds(days: int, ok: bool) -> None:
    if ok:
        assert UpgradeStatusInput(days=days).days == days
    else:
        with pytest.raises(ValidationError):
            UpgradeStatusInput(days=days)


def test_input_defaults() -> None:
    model = UpgradeStatusInput()
    assert model.container is None and model.days == 30


def _series(metric: dict[str, str], value: str) -> dict[str, object]:
    return {"metric": metric, "value": [1794369600, value]}


def test_exporter_images_join_on_real_host_and_container() -> None:
    """Rows join on (exported_hostname, container_name); the exporter's own hostname="infra" is ignored."""
    outdated = [
        _series({"container_name": "homepage", "hostname": "infra", "exported_hostname": "media vm"}, "1"),
        _series({"container_name": "homepage", "hostname": "infra", "exported_hostname": "infra vm"}, "0"),
    ]
    info = [
        _series(
            {
                "container_name": "homepage",
                "hostname": "infra",
                "exported_hostname": "media vm",
                "image": "ghcr.io/example/homepage:latest",
                "status": "outdated",
                "current_version": "v1.0.0",
                "available_version": "v1.1.0",
            },
            "1",
        )
    ]
    images = exporter_images(outdated, info)
    assert images == [
        ExporterImage(
            host="infra vm",
            container="homepage",
            outdated=False,
            image="",
            status="",
            current_version="",
            available_version="",
            base_image="",
        ),
        ExporterImage(
            host="media vm",
            container="homepage",
            outdated=True,
            image="ghcr.io/example/homepage:latest",
            status="outdated",
            current_version="v1.0.0",
            available_version="v1.1.0",
            base_image="",
        ),
    ]


def test_exporter_lines_show_versions_and_flag() -> None:
    images = [
        ExporterImage("media vm", "homepage", True, "ghcr.io/example/homepage:latest", "outdated", "v1.0", "v1.1", ""),
        ExporterImage("media vm", "sonarr", False, "lscr.io/example/sonarr:4", "up_to_date", "4.0", "4.0", ""),
    ]
    lines = format_exporter_lines(images, only_outdated=False)
    assert lines[0].startswith("  homepage on media vm: newer image available (v1.0 -> v1.1)")
    assert "image ghcr.io/example/homepage:latest" in lines[0] and "status outdated" in lines[0]
    assert lines[1].startswith("  sonarr on media vm: up to date")
    assert format_exporter_lines(images, only_outdated=True) == lines[:1]


def test_exporter_lines_unknown_flag_and_base_image() -> None:
    image = ExporterImage("media vm", "jellyfin", None, "local/jellyfin", "local", "", "", "debian:12")
    (line,) = format_exporter_lines([image], only_outdated=False)
    assert "jellyfin on media vm: freshness unknown" in line
    assert "base image debian:12" in line
