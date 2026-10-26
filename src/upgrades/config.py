"""Allowlist loader and enablement gate for the Upgrade Advisor."""

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

from src.config import get_settings


class AdvisedContainer(BaseModel):
    """One container the advisor may review (and, after approval, deploy)."""

    name: str
    host: str
    wud_url: str
    registry: str
    repository: str
    tag: str
    github_repo: str
    release_tag_format: str = "{release}"
    readiness_url: str
    pbs_guest: str
    context_files: list[str]
    auto_deploy: bool = False

    @field_validator("auto_deploy")
    @classmethod
    def _reject_auto_deploy(cls, value: bool) -> bool:
        if value:
            raise ValueError("auto_deploy is not supported yet")
        return value


def load_allowlist(path: str) -> list[AdvisedContainer]:
    """Parse the allowlist YAML (top-level ``containers`` list)."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("containers"), list):
        raise ValueError(f"{path}: expected a top-level 'containers' list")
    return [AdvisedContainer.model_validate(entry) for entry in data["containers"]]


def is_advisor_configured() -> bool:
    """True only when every setting the advisor needs is present."""
    s = get_settings()
    return bool(
        s.upgrade_advisor_config_path
        and s.upgrade_advisor_db_path
        and s.upgrade_advisor_wud_username
        and s.upgrade_advisor_wud_password
        and s.upgrade_advisor_public_base_url
    )
