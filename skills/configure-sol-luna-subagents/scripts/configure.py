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
                missing_agents.append((key, value))
        remove_index = None
        if "max_threads" in agents_value:
            if "max_threads" not in agent_assignments:
                raise ConfigureError("[agents].max_threads uses an unsupported complex layout")
            remove_index = agent_assignments["max_threads"]
        replacements_all = {**replacements, **replacements_agents}
        new_lines = [
            replacements_all.get(index, line)
            for index, line in enumerate(lines)
            if index != remove_index
        ]
        if missing_root:
            first_header_after, _ = _section_ranges(new_lines)
            new_lines[first_header_after:first_header_after] = _line_block(
                missing_root, newline
            )
        if missing_agents:
            _, sections_after = _section_ranges(new_lines)
            exact = [
                section
                for section in sections_after
                if section[2] == "table" and section[3] == "agents"
            ]
            if len(exact) != 1:
                raise ConfigureError("cannot locate the updated [agents] section")
            new_lines[exact[0][1]:exact[0][1]] = _line_block(missing_agents, newline)
        return "".join(new_lines)

    if agents_value is not None:
        raise ConfigureError("agents is not a simple [agents] table; refusing to guess")
    new_lines = [replacements.get(index, line) for index, line in enumerate(lines)]
    if missing_root:
        first_header_after, _ = _section_ranges(new_lines)
        new_lines[first_header_after:first_header_after] = _line_block(missing_root, newline)
    return _append_section("".join(new_lines), all_agent_values, newline)


def _replace_marked_block(text: str, begin: str, end: str, replacement: str) -> str:
    newline = _newline_for(text or replacement)
    replacement = replacement.rstrip("\r\n").replace("\n", newline) + newline
    begin_count = text.count(begin)
    end_count = text.count(end)
    if begin_count == 0 and end_count == 0:
        if text and not text.endswith(("\n", "\r")):
            text += newline
        return text + replacement
    if begin_count != 1 or end_count != 1:
        raise ConfigureError("managed block has duplicate or incomplete markers")
    begin_at = text.find(begin)
    end_at = text.find(end)
    if end_at <= begin_at:
        raise ConfigureError("managed block markers are out of order")
    start = text.rfind("\n", 0, begin_at) + 1
    end_line = text.find("\n", end_at)
    end_position = len(text) if end_line == -1 else end_line + 1
    return text[:start] + replacement + text[end_position:]


def _agent_marker(role: str, side: str) -> str:
    return f"# {side} MANAGED: {AGENT_MARKER_PREFIX}{role}"


def _agent_after(path: Path, asset_text: str, role: str) -> tuple[str, str]:
    existed, before = _read_optional_text(path)
    begin = _agent_marker(role, "BEGIN")
    end = _agent_marker(role, "END")
    if not existed:
        return before, asset_text
    if begin not in before and end not in before:
        raise ConfigureError(f"unmanaged agent collision: {path.name}")
    return before, _replace_marked_block(before, begin, end, asset_text)


def _active_instruction_path(home: Path) -> Path:
    override = home / OVERRIDE_NAME
    if override.is_symlink() or (override.exists() and not override.is_file()):
        raise ConfigureError(f"invalid active instruction candidate: {override}")
    if override.exists() and _read_text(override).strip():
        return override
    base = home / AGENTS_NAME
    if base.is_symlink() or (base.exists() and not base.is_file()):
        raise ConfigureError(f"invalid active instruction candidate: {base}")
    return base


def _assets() -> tuple[str, dict[str, str]]:
    block_path = ASSETS_DIR / "AGENTS.block.md"
    if not block_path.is_file():
        raise ConfigureError("missing bundled AGENTS block asset")
    block = _read_text(block_path)
    if AGENTS_BEGIN not in block or AGENTS_END not in block:
        raise ConfigureError("bundled AGENTS block markers are incomplete")
    names = (
        "code_mapper.toml",
        "implementation_worker.toml",
        "routine_state_checker.toml",
        "sol_luna_code_mapper.toml",
        "sol_luna_implementation_worker.toml",
    )
    assets: dict[str, str] = {}
    for name in names:
        path = ASSETS_DIR / name
        if not path.is_file():
            raise ConfigureError(f"missing bundled agent asset: {name}")
        assets[name] = _read_text(path)
    return block, assets


def _profile(value: str | None, *, home: Path | None = None, auto: bool = False) -> Profile:
    if auto and value is None and home is not None:
        model_path = home / MODEL_TIERS_NAME
        if model_path.is_file():
            match = PROFILE_RE.search(_read_text(model_path))
            if match and match.group(1) in PROFILES:
                return PROFILES[match.group(1)]
    key = PROFILE_ALIASES.get(value or "astra-luna")
    if key is None:
        raise ConfigureError(f"unknown profile {value!r}; choose astra-luna or sol-luna")
    return PROFILES[key]


def _registry_block(profile: Profile, *, roles: bool) -> str:
    begin, end = (ROLES_BEGIN, ROLES_END) if roles else (MODEL_BEGIN, MODEL_END)
    lines = [begin, f"# profile: {profile.key}"]
    if not roles:
        lines.append("# Tiers select provider/model only; role permissions stay in agent TOML files.")
        for tier, (provider, model, efforts) in profile.tier_models.items():
            lines.extend(
                [
                    "",
                    f"[tiers.{tier}]",
                    "enabled = true",
                    f"model_provider = {_render_value(provider)}",
                    f"model = {_render_value(model)}",
                    f"supported_efforts = {_render_value(efforts)}",
                ]
            )
    else:
        lines.append("# Tier bindings select model/effort; sandbox and authority stay in agent TOML files.")
        for role, (tier, effort) in profile.role_bindings.items():
            table = f'[roles."{role}"]' if "." in role else f"[roles.{role}]"
            lines.extend(["", table, f"tier = {_render_value(tier)}", f"effort = {_render_value(effort)}"])
    lines.append(end)
    return "\n".join(lines) + "\n"


def _registry_after(
    path: Path,
    desired: str,
    begin: str,
    end: str,
    target_names: Iterable[str],
    label: str,
) -> tuple[str, str]:
    existed, before = _read_optional_text(path)
    if not existed:
        return before, desired
    _parse_toml(before, label, allow_multiline_strings=True)
    begin_count, end_count = before.count(begin), before.count(end)
    if begin_count == 0 and end_count == 0:
        parsed = _parse_toml(before, label, allow_multiline_strings=True)
        container_name = "roles" if begin == ROLES_BEGIN else "tiers"
        container = parsed.get(container_name)
        if isinstance(container, dict) and (
            any(_role_table(container, name) is not None for name in target_names)
            if begin == ROLES_BEGIN
            else any(name in container for name in target_names)
        ):
            raise ConfigureError(f"unmanaged registry collision in {path.name}")
        suffix = "" if not before or before.endswith(("\n", "\r")) else "\n"
        return before, before + suffix + "\n" + desired
    if begin_count != 1 or end_count != 1:
        raise ConfigureError(f"manag