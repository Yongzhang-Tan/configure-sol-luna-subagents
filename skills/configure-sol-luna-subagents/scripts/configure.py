#!/usr/bin/env python3
"""Safely install and synchronize a portable Astra/Luna Codex baseline.

The configurator edits only the selected ``CODEX_HOME``. It uses a small
line-aware TOML merge for native config and marked registry blocks so comments,
unrelated roles, providers, hooks, MCP servers, and project registrations are
preserved. It never starts a model task.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping


try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - environment-dependent
        tomllib = None  # type: ignore[assignment]


SKILL_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = SKILL_DIR / "assets"
CONFIG_NAME = "config.toml"
AGENTS_NAME = "AGENTS.md"
OVERRIDE_NAME = "AGENTS.override.md"
MODEL_TIERS_NAME = "model-tiers.toml"
ROLE_BINDINGS_NAME = "agent-tiers.toml"
AGENT_MARKER_PREFIX = "configure-sol-luna-subagents:"
AGENTS_BEGIN = "<!-- BEGIN MANAGED: configure-sol-luna-subagents -->"
AGENTS_END = "<!-- END MANAGED: configure-sol-luna-subagents -->"
MODEL_BEGIN = "# BEGIN MANAGED: configure-sol-luna-subagents:model-tiers"
MODEL_END = "# END MANAGED: configure-sol-luna-subagents:model-tiers"
ROLES_BEGIN = "# BEGIN MANAGED: configure-sol-luna-subagents:agent-tiers"
ROLES_END = "# END MANAGED: configure-sol-luna-subagents:agent-tiers"
PROFILE_RE = re.compile(r"^# profile: ([a-z0-9-]+)\s*$", re.MULTILINE)

BASE_AGENTS_VALUES: tuple[tuple[str, Any], ...] = (
    ("enabled", True),
    ("max_depth", 1),
    ("max_concurrent_threads_per_session", 3),
)


class ConfigureError(RuntimeError):
    """A safe, user-actionable configuration error."""


@dataclass(frozen=True)
class Profile:
    key: str
    display_name: str
    main_model: str
    main_effort: str
    default_model: str
    default_effort: str
    tier_models: Mapping[str, tuple[str, str, tuple[str, ...]]]
    role_bindings: Mapping[str, tuple[str, str]]


@dataclass(frozen=True)
class RoleSpec:
    path_name: str
    asset_name: str
    expected_name: str
    binding_name: str
    sandbox_mode: str


@dataclass(frozen=True)
class AgentTarget:
    path: Path
    before: str
    after: str
    expected_name: str
    model: str
    effort: str
    provider: str
    sandbox_mode: str


@dataclass(frozen=True)
class RegistryState:
    profile: str
    tiers: Mapping[str, Mapping[str, Any]]
    roles: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Plan:
    home: Path
    profile: Profile
    config_path: Path
    instruction_path: Path
    model_tiers_path: Path
    role_bindings_path: Path
    config_before: str
    config_after: str
    instruction_before: str
    instruction_after: str
    model_tiers_before: str
    model_tiers_after: str
    role_bindings_before: str
    role_bindings_after: str
    agent_targets: tuple[AgentTarget, ...]

    @property
    def targets(self) -> tuple[Path, ...]:
        return (
            self.config_path,
            self.instruction_path,
            self.model_tiers_path,
            self.role_bindings_path,
        ) + tuple(target.path for target in self.agent_targets)


PROFILES: dict[str, Profile] = {
    "astra-luna": Profile(
        key="astra-luna",
        display_name="Astra → Luna",
        main_model="gpt-6-astra",
        main_effort="medium",
        default_model="gpt-5.6-luna",
        default_effort="max",
        tier_models={
     