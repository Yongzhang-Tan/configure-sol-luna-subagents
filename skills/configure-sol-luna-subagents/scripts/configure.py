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
            "T1": ("openai", "gpt-6-astra", ("low", "medium", "high", "max")),
            "T2": ("openai", "gpt-5.6-luna", ("low", "medium", "high", "xhigh", "max")),
            "T3": ("openai", "gpt-5.6-luna", ("low", "medium", "high", "xhigh", "max")),
        },
        role_bindings={
            "main": ("T1", "medium"),
            "default_subagent": ("T2", "max"),
            "agents.default": ("T2", "max"),
            "code_mapper": ("T2", "max"),
            "implementation_worker": ("T2", "max"),
            "routine_state_checker": ("T3", "max"),
            # Retained so an existing installation can keep its old filenames.
            "sol_luna_code_mapper": ("T2", "max"),
            "sol_luna_implementation_worker": ("T2", "max"),
        },
    ),
    "sol-luna": Profile(
        key="sol-luna",
        display_name="legacy Sol → Luna",
        main_model="gpt-5.6-sol",
        main_effort="max",
        default_model="gpt-5.6-luna",
        default_effort="max",
        tier_models={
            "T1": ("openai", "gpt-5.6-sol", ("low", "medium", "high", "max")),
            "T2": ("openai", "gpt-5.6-luna", ("low", "medium", "high", "xhigh", "max")),
            "T3": ("openai", "gpt-5.6-luna", ("low", "medium", "high", "xhigh", "max")),
        },
        role_bindings={
            "main": ("T1", "max"),
            "default_subagent": ("T2", "max"),
            "agents.default": ("T2", "max"),
            "code_mapper": ("T2", "max"),
            "implementation_worker": ("T2", "max"),
            "routine_state_checker": ("T3", "max"),
            "sol_luna_code_mapper": ("T2", "max"),
            "sol_luna_implementation_worker": ("T2", "max"),
        },
    ),
}
PROFILE_ALIASES = {
    "astra": "astra-luna",
    "astra-luna": "astra-luna",
    "default": "astra-luna",
    "legacy-sol": "sol-luna",
    "legacy-sol-luna": "sol-luna",
    "sol": "sol-luna",
    "sol-luna": "sol-luna",
}


def _require_tomllib() -> Any:
    if tomllib is None:
        raise ConfigureError(
            "Python 3.11+ or install tomli; no dependency installation is attempted."
        )
    return tomllib


def _home_from_arg(value: str | None) -> Path:
    raw = value or os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(raw).expanduser().resolve()


def _ensure_home_for_apply(home: Path) -> None:
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ConfigureError(f"CODEX_HOME is not a directory: {home}")
    home.mkdir(parents=True, exist_ok=True)


def _assert_safe_parent(path: Path, root: Path) -> None:
    """Require a write target's resolved parent to stay below root."""

    resolved_root = root.resolve()
    parent = path.parent
    if parent.is_symlink():
        raise ConfigureError(f"refusing symlink parent: {parent}")
    try:
        parent.resolve().relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ConfigureError(f"target escapes its allowed root: {path}") from exc


def _assert_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ConfigureError(f"expected a non-symlink directory: {path}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigureError(f"cannot safely decode UTF-8 file: {path}") from exc
    except OSError as exc:
        raise ConfigureError(f"cannot read {path}: {exc}") from exc


def _read_optional_text(path: Path) -> tuple[bool, str]:
    if path.is_symlink():
        raise ConfigureError(f"refusing symlink target: {path}")
    if not path.exists():
        return False, ""
    if not path.is_file():
        raise ConfigureError(f"expected a regular file: {path}")
    return True, _read_text(path)


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _split_comment(text: str) -> tuple[str, str]:
    """Split a TOML line at an unquoted comment marker."""

    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            return text[:index], text[index:]
    return text, ""


def _table_header(line: str) -> tuple[str, str] | None:
    code, _ = _split_comment(_split_line_ending(line)[0])
    stripped = code.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        return "array", stripped[2:-2].strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return "table", stripped[1:-1].strip()
    return None


def _section_ranges(lines: list[str]) -> tuple[int, list[tuple[int, int, str, str]]]:
    headers: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        header = _table_header(line)
        if header is not None:
            headers.append((index, header[0], header[1]))
    first = headers[0][0] if headers else len(lines)
    sections = []
    for position, (start, kind, body) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        sections.append((start, end, kind, body))
    return first, sections


_ASSIGNMENT = re.compile(r"^(?P<prefix>\s*)(?P<key>[A-Za-z0-9_-]+)(?P<between>\s*=\s*)(?P<rhs>.*)$")


def _assignments(lines: list[str], start: int, end: int) -> dict[str, int]:
    found: dict[str, int] = {}
    for index in range(start, end):
        code, _ = _split_comment(_split_line_ending(lines[index])[0])
        match = _ASSIGNMENT.match(code)
        if match is None:
            continue
        key = match.group("key")
        if key in found:
            raise ConfigureError(f"ambiguous duplicate key {key!r} in config.toml")
        found[key] = index
    return found


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    raise ConfigureError(f"unsupported native value type: {type(value).__name__}")


def _replace_assignment(line: str, key: str, value: Any) -> str:
    content, ending = _split_line_ending(line)
    code, comment = _split_comment(content)
    match = re.match(rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)(?P<rhs>.*)$", code)
    if match is None:
        raise ConfigureError(f"cannot safely update config.toml key {key!r}")
    try:
        _require_tomllib().loads(f"{key} = {match.group('rhs')}\n")
    except Exception as exc:
        raise ConfigureError(
            f"config.toml key {key!r} uses a multiline or unsupported layout"
        ) from exc
    whitespace = ""
    if comment:
        comment_start = content.find(comment)
        prefix_to_comment = content[:comment_start]
        whitespace_match = re.search(r"\s*$", prefix_to_comment)
        whitespace = whitespace_match.group(0) if whitespace_match else " "
        if not whitespace:
            whitespace = " "
    return match.group("prefix") + _render_value(value) + whitespace + comment + ending


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_block(fields: Iterable[tuple[str, Any]], newline: str) -> list[str]:
    return [f"{key} = {_render_value(value)}{newline}" for key, value in fields]


def _append_section(text: str, fields: Iterable[tuple[str, Any]], newline: str) -> str:
    result = text
    if result and not result.endswith(("\n", "\r")):
        result += newline
    if result and not result.endswith(newline + newline):
        result += newline
    result += "[agents]" + newline
    result += "".join(_line_block(fields, newline))
    return result


def _parse_toml(text: str, label: str, *, allow_multiline_strings: bool = False) -> dict[str, Any]:
    parser = _require_tomllib()
    if not allow_multiline_strings and ('"""' in text or "'''" in text):
        raise ConfigureError(
            f"{label} contains a multiline TOML string; safe line-preserving merge is ambiguous"
        )
    try:
        value = parser.loads(text)
    except Exception as exc:  # TOMLDecodeError differs across Python versions.
        raise ConfigureError(f"invalid TOML: {label}") from exc
    if not isinstance(value, dict):
        raise ConfigureError(f"TOML root is not a table: {label}")
    return value


def _config_after(
    text: str,
    root_values: tuple[tuple[str, Any], ...],
    agents_values: tuple[tuple[str, Any], ...],
) -> str:
    all_agent_values = (*BASE_AGENTS_VALUES, *agents_values)
    if not text:
        return (
            "".join(f"{key} = {_render_value(value)}\n" for key, value in root_values)
            + "\n[agents]\n"
            + "".join(f"{key} = {_render_value(value)}\n" for key, value in all_agent_values)
        )

    parsed = _parse_toml(text, CONFIG_NAME)
    lines = text.splitlines(keepends=True)
    newline = _newline_for(text)
    first_header, sections = _section_ranges(lines)
    root_assignments = _assignments(lines, 0, first_header)
    replacements: dict[int, str] = {}
    missing_root: list[tuple[str, Any]] = []
    for key, value in root_values:
        if key in parsed:
            if key not in root_assignments:
                raise ConfigureError(f"root key {key!r} uses an unsupported complex layout")
            replacements[root_assignments[key]] = _replace_assignment(
                lines[root_assignments[key]], key, value
            )
        else:
            missing_root.append((key, value))

    agents_value = parsed.get("agents")
    agent_sections = [
        section for section in sections if section[2] == "table" and section[3] == "agents"
    ]
    if len(agent_sections) > 1:
        raise ConfigureError("ambiguous duplicate [agents] sections")
    if agent_sections:
        agent_start, agent_end, _, _ = agent_sections[0]
        agent_assignments = _assignments(lines, agent_start + 1, agent_end)
        if not isinstance(agents_value, dict):
            raise ConfigureError("agents is not a simple [agents] table; refusing to guess")
        replacements_agents: dict[int, str] = {}
        missing_agents: list[tuple[str, Any]] = []
        for key, value in all_agent_values:
            if key in agents_value:
                if key not in agent_assignments:
                    raise ConfigureError(
                        f"[agents] key {key!r} uses an unsupported complex layout"
                    )
                replacements_agents[agent_assignments[key]] = _replace_assignment(
                    lines[agent_assignments[key]], key, value
                )
            else:
  