"""Allowlist loader and enablement gate for the Upgrade Advisor."""

import string
from pathlib import Path, PurePosixPath
from typing import Annotated

import yaml
from pydantic import AfterValidator, AnyHttpUrl, BaseModel, ConfigDict, StringConstraints, field_validator

from src.config import get_settings

CONTEXT_ROOT = PurePosixPath("/app/upgrades/context")


def _http_url(value: str) -> str:
    """Validate as an http(s) URL with a host; return the original string unchanged."""
    if value != value.strip():
        msg = "URL must not have leading or trailing whitespace"
        raise ValueError(msg)
    AnyHttpUrl(value)
    return value


HttpUrlStr = Annotated[str, AfterValidator(_http_url)]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")]
Registry = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.-]+(:[0-9]+)?$")]
Repository = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$")]
Tag = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")]
GithubRepo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
PbsGuest = Annotated[str, StringConstraints(pattern=r"^(CT|VM) [0-9]+$")]


class AdvisedContainer(BaseModel):
    """One container the advisor may review (and, after approval, deploy)."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    host: Name
    wud_url: HttpUrlStr
    registry: Registry
    repository: Repository
    tag: Tag
    github_repo: GithubRepo
    release_tag_format: str = "{release}"
    readiness_url: HttpUrlStr
    pbs_guest: PbsGuest
    context_files: list[str]
    auto_deploy: bool = False

    @field_validator("context_files")
    @classmethod
    def _context_files_under_root(cls, value: list[str]) -> list[str]:
        for entry in value:
            path = PurePosixPath(entry)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or not path.is_relative_to(CONTEXT_ROOT)
                or path == CONTEXT_ROOT
            ):
                msg = f"context file {entry!r} must be absolute, free of '..', and under {CONTEXT_ROOT}/"
                raise ValueError(msg)
        return value

    @field_validator("release_tag_format")
    @classmethod
    def _release_tag_format_fields(cls, value: str) -> str:
        try:
            parsed = list(string.Formatter().parse(value))
        except ValueError as exc:
            msg = f"release_tag_format is not a valid format string: {exc}"
            raise ValueError(msg) from exc
        if any(spec and "{" in spec for _, _, spec, _ in parsed):
            msg = "release_tag_format must not nest fields inside a format spec"
            raise ValueError(msg)
        fields = [name for _, name, _, _ in parsed if name is not None]
        if fields.count("release") < 1 or any(f != "release" for f in fields):
            msg = "release_tag_format must contain {release} and no other {...} field"
            raise ValueError(msg)
        return value

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
