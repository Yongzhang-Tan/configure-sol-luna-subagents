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
        raise ConfigureError(f"managed registry block is incomplete in {path.name}")
    marker_block = before[before.find(begin): before.find(end) + len(end)]
    match = PROFILE_RE.search(marker_block)
    desired_match = PROFILE_RE.search(desired)
    if match is None or desired_match is None:
        raise ConfigureError(f"managed registry profile is missing in {path.name}")
    if match.group(1) == desired_match.group(1):
        # Existing managed values are the editable source of truth.
        return before, before
    return before, _replace_marked_block(before, begin, end, desired)


def _role_table(roles: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    direct = roles.get(name)
    if isinstance(direct, dict):
        return direct
    current: Any = roles
    for part in name.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, dict) else None


def _registry_state(model_text: str, role_text: str, profile: Profile) -> RegistryState:
    model = _parse_toml(model_text, MODEL_TIERS_NAME, allow_multiline_strings=True)
    roles_doc = _parse_toml(role_text, ROLE_BINDINGS_NAME, allow_multiline_strings=True)
    model_match = PROFILE_RE.search(model_text)
    role_match = PROFILE_RE.search(role_text)
    if not model_match or not role_match or model_match.group(1) != role_match.group(1):
        raise ConfigureError("model-tiers.toml and agent-tiers.toml have no matching managed profile")
    if model_match.group(1) != profile.key:
        raise ConfigureError(
            f"registry profile is {model_match.group(1)!r}, but requested profile is {profile.key!r}"
        )
    tiers = model.get("tiers")
    roles = roles_doc.get("roles")
    if not isinstance(tiers, dict) or not isinstance(roles, dict):
        raise ConfigureError("tier registries must contain [tiers.*] and [roles.*] tables")
    referenced_tiers = set(profile.tier_models)
    for name in profile.role_bindings:
        binding = _role_table(roles, name)
        if isinstance(binding, dict) and isinstance(binding.get("tier"), str):
            referenced_tiers.add(binding["tier"])

    def collect_referenced(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("tier"), str):
                referenced_tiers.add(value["tier"])
            for child in value.values():
                collect_referenced(child)
        elif isinstance(value, list):
            for child in value:
                collect_referenced(child)

    collect_referenced(roles)
    for name in referenced_tiers:
        spec = tiers.get(name)
        if not isinstance(spec, dict) or spec.get("enabled") is not True:
            raise ConfigureError(f"tier {name!r} is missing or disabled")
        if (
            not isinstance(spec.get("model"), str)
            or not spec["model"].strip()
            or not isinstance(spec.get("model_provider"), str)
            or not spec["model_provider"].strip()
        ):
            raise ConfigureError(f"tier {name!r} has no portable provider/model")
        efforts = spec.get("supported_efforts")
        if (
            not isinstance(efforts, list)
            or not efforts
            or not all(isinstance(item, str) and item.strip() for item in efforts)
        ):
            raise ConfigureError(f"tier {name!r} has invalid supported_efforts")
    for name in profile.role_bindings:
        binding = _role_table(roles, name)
        if binding is None:
            raise ConfigureError(f"role binding {name!r} is missing")
        tier, effort = binding.get("tier"), binding.get("effort")
        if (
            not isinstance(tier, str)
            or not tier.strip()
            or not isinstance(effort, str)
            or not effort.strip()
            or tier not in tiers
        ):
            raise ConfigureError(f"role binding {name!r} is invalid")
        supported = tiers[tier].get("supported_efforts", [])
        if effort not in supported:
            raise ConfigureError(f"role binding {name!r} requests unsupported effort {effort!r}")
    return RegistryState(profile.key, tiers, roles)


def _resolved_role(state: RegistryState, role: str) -> tuple[str, str, str]:
    binding = _role_table(state.roles, role)
    if binding is None:
        raise ConfigureError(f"role binding {role!r} is missing")
    tier_name, effort = binding["tier"], binding["effort"]
    tier = state.tiers[tier_name]
    return str(tier["model"]), str(effort), str(tier["model_provider"])


def _validate_provider_bindings(state: RegistryState, roles: Iterable[str]) -> None:
    _, _, main_provider = _resolved_role(state, "main")
    for role in roles:
        _, _, provider = _resolved_role(state, role)
        if provider != main_provider:
            raise ConfigureError(
                f"role binding {role!r} uses provider {provider!r}, which differs from main provider "
                f"{main_provider!r}; cross-provider materialization is refused"
            )


def _render_agent_asset(text: str, model: str, effort: str) -> str:
    lines = text.splitlines(keepends=True)
    model_indexes: list[int] = []
    effort_indexes: list[int] = []
    for index, line in enumerate(lines):
        code, _ = _split_comment(_split_line_ending(line)[0])
        if re.match(r"^\s*model\s*=", code):
            model_indexes.append(index)
        if re.match(r"^\s*model_reasoning_effort\s*=", code):
            effort_indexes.append(index)
    if len(model_indexes) != 1 or len(effort_indexes) != 1:
        raise ConfigureError("agent asset has ambiguous model fields")
    newline = _newline_for(text)
    lines[model_indexes[0]] = f'model = {_render_value(model)}{newline}'
    lines[effort_indexes[0]] = f'model_reasoning_effort = {_render_value(effort)}{newline}'
    return "".join(lines)


def _role_specs(home: Path, profile: Profile) -> tuple[RoleSpec, ...]:
    pairs = (
        ("code_mapper", "sol_luna_code_mapper.toml", "sol_luna_code_mapper", "read-only"),
        ("implementation_worker", "sol_luna_implementation_worker.toml", "sol_luna_implementation_worker", "workspace-write"),
    )
    selected: list[RoleSpec] = []
    for canonical_role, old_filename, old_name, sandbox in pairs:
        canonical_filename = f"{canonical_role}.toml"
        canonical_path = home / "agents" / canonical_filename
        old_path = home / "agents" / old_filename
        if old_path.exists() and canonical_path.exists():
            raise ConfigureError(
                f"both canonical and compatibility agent files exist for {canonical_role}; refusing ambiguous writers"
            )
        if old_path.exists():
            # Upgrade old installations in place so a second writer is never created.
            path_name, asset_name, expected = old_filename, old_filename, old_name
        elif canonical_path.exists():
            # Preserve an existing canonical installation even when the legacy
            # profile is selected later; never create a second writer.
            path_name, asset_name, expected = canonical_filename, canonical_filename, canonical_role
        elif profile.key == "sol-luna":
            path_name, asset_name, expected = old_filename, old_filename, old_name
        else:
            path_name, asset_name, expected = canonical_filename, canonical_filename, canonical_role
        selected.append(RoleSpec(path_name, asset_name, expected, canonical_role, sandbox))
    old_routine = home / "agents" / "sol_luna_routine_state_checker.toml"
    canonical_routine = home / "agents" / "routine_state_checker.toml"
    if old_routine.exists() and canonical_routine.exists():
        raise ConfigureError("both canonical and compatibility routine checkers exist; refusing ambiguity")
    if old_routine.exists():
        selected.append(RoleSpec("sol_luna_routine_state_checker.toml", "routine_state_checker.toml", "sol_luna_routine_state_checker", "routine_state_checker", "read-only"))
    else:
        selected.append(RoleSpec("routine_state_checker.toml", "routine_state_checker.toml", "routine_state_checker", "routine_state_checker", "read-only"))
    return tuple(selected)


def _provider_compatible(config_text: str, state: RegistryState) -> None:
    parsed = _parse_toml(config_text, CONFIG_NAME) if config_text else {}
    # Native Codex defaults an omitted provider to OpenAI. Do not silently
    # materialize a tier from another provider into that inherited config.
    configured = parsed.get("model_provider", "openai")
    _, _, expected = _resolved_role(state, "main")
    if configured != expected:
        raise ConfigureError(
            f"config.toml model_provider {configured!r} does not match tier provider {expected!r}; "
            "the installer will not rewrite providers or credentials"
        )


def _build_plan(home: Path, profile: Profile, *, sync_only: bool = False) -> Plan:
    block, assets = _assets()
    model_path, role_path = home / MODEL_TIERS_NAME, home / ROLE_BINDINGS_NAME
    if sync_only:
        model_exists, model_before = _read_optional_text(model_path)
        role_exists, role_before = _read_optional_text(role_path)
        if not model_exists or not role_exists:
            raise ConfigureError("sync requires both existing model-tiers.toml and agent-tiers.toml")
        model_after, role_after = model_before, role_before
    else:
        model_before, model_after = _registry_after(
            model_path,
            _registry_block(profile, roles=False),
            MODEL_BEGIN,
            MODEL_END,
            profile.tier_models,
            MODEL_TIERS_NAME,
        )
        role_before, role_after = _registry_after(
            role_path,
            _registry_block(profile, roles=True),
            ROLES_BEGIN,
            ROLES_END,
            profile.role_bindings,
            ROLE_BINDINGS_NAME,
        )
    state = _registry_state(model_after, role_after, profile)
    _validate_provider_bindings(state, profile.role_bindings)
    config_path = home / CONFIG_NAME
    config_exists, config_before = _read_optional_text(config_path)
    _provider_compatible(config_before if config_exists else "", state)
    main_model, main_effort, _ = _resolved_role(state, "main")
    default_model, default_effort, _ = _resolved_role(state, "default_subagent")
    root_values = (("model", main_model), ("model_reasoning_effort", main_effort))
    agents_values = (
        ("default_subagent_model", default_model),
        ("default_subagent_reasoning_effort", default_effort),
    )
    config_after = (
        _config_after(config_before, root_values, agents_values)
        if config_exists
        else _config_after("", root_values, agents_values)
    )
    instruction_path = _active_instruction_path(home)
    instruction_exists, instruction_before = _read_optional_text(instruction_path)
    instruction_after = _replace_marked_block(
        instruction_before if instruction_exists else "", AGENTS_BEGIN, AGENTS_END, block
    )
    agents_dir = home / "agents"
    _assert_directory(agents_dir)
    targets: list[AgentTarget] = []
    for spec in _role_specs(home, profile):
        model, effort, provider = _resolved_role(state, spec.binding_name)
        asset = _render_agent_asset(assets[spec.asset_name], model, effort)
        path = agents_dir / spec.path_name
        before, after = _agent_after(path, asset, Path(spec.path_name).stem)
        targets.append(
            AgentTarget(path, before, after, spec.expected_name, model, effort, provider, spec.sandbox_mode)
        )
    return Plan(
        home=home,
        profile=profile,
        config_path=config_path,
        instruction_path=instruction_path,
        model_tiers_path=model_path,
        role_bindings_path=role_path,
        config_before=config_before,
        config_after=config_after,
        instruction_before=instruction_before,
        instruction_after=instruction_after,
        model_tiers_before=model_before,
        model_tiers_after=model_after,
        role_bindings_before=role_before,
        role_bindings_after=role_after,
        agent_targets=tuple(targets),
    )


def _validate_config(text: str, state: RegistryState) -> None:
    parsed = _parse_toml(text, CONFIG_NAME)
    main_model, main_effort, _ = _resolved_role(state, "main")
    default_model, default_effort, _ = _resolved_role(state, "default_subagent")
    if parsed.get("model") != main_model or parsed.get("model_reasoning_effort") != main_effort:
        raise ConfigureError("config.toml main model/effort does not match the tier registry")
    agents = parsed.get("agents")
    if not isinstance(agents, dict):
        raise ConfigureError("config.toml has no [agents] table")
    expected = dict(
        (*BASE_AGENTS_VALUES,
         ("default_subagent_model", default_model),
         ("default_subagent_reasoning_effort", default_effort))
    )
    for key, value in expected.items():
        if agents.get(key) != value:
            raise ConfigureError(f"[agents].{key} has an unexpected value")
    if "max_threads" in agents:
        raise ConfigureError("legacy [agents].max_threads is still active")
    _provider_compatible(text, state)


def _validate_agent(target: AgentTarget) -> None:
    text = _read_text(target.path)
    role = target.path.stem
    if text.count(_agent_marker(role, "BEGIN")) != 1 or text.count(_agent_marker(role, "END")) != 1:
        raise ConfigureError(f"managed agent markers are incomplete: {target.path.name}")
    parsed = _parse_toml(text, target.path.name, allow_multiline_strings=True)
    if parsed.get("name") != target.expected_name:
        raise ConfigureError(f"unexpected agent name: {target.path.name}")
    if parsed.get("model") != target.model or parsed.get("model_reasoning_effort") != target.effort:
        raise ConfigureError(f"agent model/effort does not match tier registry: {target.path.name}")
    if parsed.get("sandbox_mode") != target.sandbox_mode:
        raise ConfigureError(f"unexpected sandbox mode: {target.path.name}")
    if not isinstance(parsed.get("developer_instructions"), str):
        raise ConfigureError(f"agent instructions are missing: {target.path.name}")


def _verify_static(home: Path, profile: Profile) -> Plan:
    plan = _build_plan(home, profile, sync_only=True)
    state = _registry_state(plan.model_tiers_after, plan.role_bindings_after, profile)
    _validate_config(_read_text(plan.config_path), state)
    instruction_text = _read_text(plan.instruction_path)
    if instruction_text.count(AGENTS_BEGIN) != 1 or instruction_text.count(AGENTS_END) != 1:
        raise ConfigureError(f"active AGENTS managed block is incomplete: {plan.instruction_path.name}")
    for target in plan.agent_targets:
        _validate_agent(target)
    return plan


def _atomic_write(
    path: Path, data: bytes, mode: int | None = None, *, root: Path | None = None
) -> None:
    if root is not None:
        _assert_safe_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigureError(f"refusing to overwrite non-regular target: {path}")
    inherited_mode = mode
    if inherited_mode is None and path.exists():
        inherited_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if inherited_mode is not None:
            os.chmod(temporary_path, inherited_mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigureError(f"atomic write failed for {path}: {exc}") from exc


def _target_mode(path: Path) -> int | None:
    if path.is_symlink():
        raise ConfigureError(f"refusing symlink target: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ConfigureError(f"expected a regular file: {path}")
    return stat.S_IMODE(path.stat().st_mode)


def _timestamped_backup(plan: Plan) -> tuple[Path, dict[str, Any]]:
    root = plan.home / "backups" / "configure-sol-luna-subagents"
    _assert_directory(plan.home / "backups")
    _assert_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / timestamp
    suffix = 1
    while backup.exists():
        backup = root / f"{timestamp}-{suffix}"
        suffix += 1
    backup.mkdir()
    records: list[dict[str, Any]] = []
    for target in plan.targets:
        mode = _target_mode(target)
        existed = mode is not None
        relative = target.relative_to(plan.home).as_posix()
        record: dict[str, Any] = {
            "path": relative,
            "existed_before": existed,
            "mode": mode,
            "backup": None,
        }
        if existed:
            backup_relative = Path("files") / relative
            backup_file = backup / backup_relative
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(backup_file, target.read_bytes(), mode=mode, root=backup)
            record["backup"] = backup_relative.as_posix()
        records.append(record)
    manifest = {
        "schema": 2,
        "codex_home": str(plan.home),
        "profile": plan.profile.key,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": records,
    }
    _atomic_write(
        backup / "manifest.json",
        (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
        root=backup,
    )
    return backup, manifest


def _manifest_records(home: Path, backup_arg: str) -> tuple[Path, dict[str, Any]]:
    _assert_directory(home / "backups")
    _assert_directory(home / "backups" / "configure-sol-luna-subagents")
    expected_root = (home / "backups" / "configure-sol-luna-subagents").resolve()
    backup = Path(backup_arg).expanduser().resolve()
    if backup.parent != expected_root:
        raise ConfigureError("backup must be a direct child of CODEX_HOME's expected backup root")
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ConfigureError("backup manifest is missing")
    try:
        manifest = json.loads(_read_text(manifest_path))
    except json.JSONDecodeError as exc:
        raise ConfigureError("backup manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") not in (1, 2):
        raise ConfigureError("unsupported backup manifest")
    if manifest.get("codex_home") != str(home):
        raise ConfigureError("backup CODEX_HOME does not match the requested CODEX_HOME")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ConfigureError("backup manifest has no file records")
    seen: set[str] = set()
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConfigureError("backup manifest has an invalid file record")
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or record["path"] in seen:
            raise ConfigureError("backup manifest contains an unsafe or duplicate path")
        seen.add(record["path"])
        target = home / relative
        _assert_safe_parent(target, home)
        if target.resolve().relative_to(home) != relative:
            raise ConfigureError("backup manifest target escapes CODEX_HOME")
        if record.get("existed_before"):
            backup_relative = record.get("backup")
            if not isinstance(backup_relative, str):
                raise ConfigureError("backup manifest is missing a source file")
            source = (backup / backup_relative).resolve()
            try:
                source.relative_to(backup)
            except ValueError as exc:
                raise ConfigureError("backup manifest source escapes backup directory") from exc
            if not source.is_file() or source.is_symlink():
                raise ConfigureError("backup source file is missing")
    return backup, manifest


def _rollback_manifest(home: Path, backup_arg: str) -> int:
    backup, manifest = _manifest_records(home, backup_arg)
    validated: list[tuple[dict[str, Any], Path, Path | None]] = []
    for record in manifest["files"]:
        target = home / Path(record["path"])
        _assert_safe_parent(target, home)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ConfigureError(f"rollback target is not a regular file: {target}")
        source = None
        if record.get("existed_before"):
            source = (backup / Path(record["backup"])).resolve()
        validated.append((record, target, source))
    restored = 0
    for record, target, source in validated:
        if record.get("existed_before"):
            assert source is not None
            mode = record.get("mode")
            _atomic_write(
                target,
                source.read_bytes(),
                mode=mode if isinstance(mode, int) else None,
                root=home,
            )
        elif target.exists():
            target.unlink()
        restored += 1
    return restored


def _run_codex_check(home: Path) -> None:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    try:
        result = subprocess.run(
            ["codex", "features", "list"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigureError("codex executable was not found for --run-codex") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigureError("codex features list timed out") from exc
    if result.returncode != 0:
        raise ConfigureError("codex features list could not parse the global configuration")


def _planned_content(plan: Plan, path: Path) -> str:
    mapping = {
        plan.config_path: plan.config_after,
        plan.instruction_path: plan.instruction_after,
        plan.model_tiers_path: plan.model_tiers_after,
        plan.role_bindings_path: plan.role_bindings_after,
    }
    if path in mapping:
        return mapping[path]
    for target in plan.agent_targets:
        if target.path == path:
            return target.after
    raise ConfigureError(f"internal plan target is unknown: {path}")


def _changed_paths(plan: Plan) -> list[Path]:
    changed: list[Path] = []
    for path in plan.targets:
        content = _planned_content(plan, path)
        if not path.exists() or _read_text(path) != content:
            changed.append(path)
    return changed


def _confirm(plan: Plan, *, yes: bool) -> bool:
    changed = _changed_paths(plan)
    if not changed:
        print(f"NOOP: {plan.profile.display_name} is already synchronized")
        return False
    _print_audit(plan, preview=True)
    print(f"About to update {len(changed)} file(s) below CODEX_HOME for {plan.profile.display_name}:")
    for path in changed:
        print(f"  - {path.relative_to(plan.home)}")
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigureError("apply/sync requires --yes in a non-interactive session")
    answer = input("Apply this plan? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        raise ConfigureError("operation cancelled; no files were changed")
    return True


def _write_plan(plan: Plan) -> int:
    changed = 0
    for path in plan.targets:
        content = _planned_content(plan, path)
        if not path.exists() or _read_text(path) != content:
            _atomic_write(
                path,
                content.encode("utf-8"),
                mode=_target_mode(path),
                root=plan.home,
            )
            changed += 1
    return changed


def _apply_plan(plan: Plan, *, run_codex: bool) -> tuple[int, Path]:
    backup, _ = _timestamped_backup(plan)
    try:
        changed = _write_plan(plan)
        _verify_static(plan.home, plan.profile)
        if run_codex:
            _run_codex_check(plan.home)
    except Exception as exc:
        try:
            _rollback_manifest(plan.home, str(backup))
        except Exception as rollback_exc:
            raise ConfigureError(
                f"validation failed ({exc}); automatic rollback also failed ({rollback_exc})"
            ) from exc
        if isinstance(exc, ConfigureError):
            raise ConfigureError(
                f"validation failed; automatic rollback completed: {exc}"
            ) from exc
        raise ConfigureError(f"apply failed; automatic rollback completed: {exc}") from exc
    return changed, backup


def _print_audit(plan: Plan, *, preview: bool = False) -> None:
    print("PREVIEW OK" if preview else "AUDIT OK")
    print(f"CODEX_HOME: {plan.home}")
    print(f"profile: {plan.profile.key}")
    state = _registry_state(plan.model_tiers_after, plan.role_bindings_after, plan.profile)
    main_model, main_effort, main_provider = _resolved_role(state, "main")
    default_model, default_effort, default_provider = _resolved_role(state, "default_subagent")
    print(f"resolved main: {main_model} / {main_effort} / provider={main_provider}")
    print(f"resolved default_subagent: {default_model} / {default_effort} / provider={default_provider}")
    for label, path in (
        ("config.toml", plan.config_path),
        ("active instructions", plan.instruction_path),
        (MODEL_TIERS_NAME, plan.model_tiers_path),
        (ROLE_BINDINGS_NAME, plan.role_bindings_path),
    ):
        print(f"{label}: {'present' if path.is_file() else 'missing'}")
    for target in plan.agent_targets:
        print(
            f"agent {target.path.name}: "
            f"{'present' if target.path.is_file() else 'missing'} "
            f"({target.model} / {target.effort} / provider={target.provider} / {target.sandbox_mode})"
        )
    print("planned scope: global config, active instructions, portable tier registries, and managed agents")
    if preview:
        print(f"would change: {len(_changed_paths(plan))} file(s); no files were written")


def _cmd_audit(home: Path, profile: Profile, *, preview: bool = False) -> int:
    plan = _build_plan(home, profile)
    _print_audit(plan, preview=preview)
    return 0


def _cmd_apply(
    home: Path,
    profile: Profile,
    *,
    yes: bool,
    run_codex: bool,
    sync_only: bool = False,
) -> int:
    # Keep a cancelled/non-interactive preview genuinely non-mutating; the
    # home directory is created only after confirmation succeeds.
    plan = _build_plan(home, profile, sync_only=sync_only)
    if not _confirm(plan, yes=yes):
        return 0
    if not sync_only:
        _ensure_home_for_apply(home)
    changed, backup = _apply_plan(plan, run_codex=run_codex)
    print(f"{'SYNC' if sync_only else 'APPLY'} OK: changed {changed} file(s)")
    print(f"backup: {backup}")
    print("next step: run verify, then start a new Codex session or restart the client")
    return 0


def _cmd_verify(home: Path, profile: Profile, run_codex: bool) -> int:
    _verify_static(home, profile)
    if run_codex:
        _run_codex_check(home)
    print("VERIFY OK: static configuration and managed files are valid")
    if run_codex:
        print("codex parse check: OK (this is not a paid model smoke test)")
    return 0


def _cmd_tiers(home: Path, action: str, profile: Profile) -> int:
    model_path, role_path = home / MODEL_TIERS_NAME, home / ROLE_BINDINGS_NAME
    if action == "list":
        for path in (model_path, role_path):
            if path.is_file():
                parsed = _parse_toml(_read_text(path), path.name, allow_multiline_strings=True)
                print(f"{path.name}: present ({len(parsed)} top-level table(s))")
            else:
                print(f"{path.name}: missing")
        return 0
    if not model_path.is_file() or not role_path.is_file():
        raise ConfigureError("tier check requires both registry files; run preview, then apply")
    state = _registry_state(_read_text(model_path), _read_text(role_path), profile)
    print(f"TIER CHECK OK: profile={state.profile}, tiers={len(state.tiers)}, roles={len(state.roles)}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")

    def common(
        command: argparse.ArgumentParser,
        *,
        confirmation: bool = False,
        run_codex: bool = False,
    ) -> None:
        command.add_argument(
            "--codex-home",
            metavar="PATH",
            help="global Codex home; defaults to CODEX_HOME or ~/.codex",
        )
        command.add_argument(
            "--profile",
            choices=tuple(PROFILE_ALIASES),
            default=None,
            help="astra-luna (default) or legacy sol-luna",
        )
        if confirmation:
            command.add_argument(
                "--yes",
                action="store_true",
                help="confirm the exact planned write without prompting",
            )
        if run_codex:
            command.add_argument(
                "--run-codex",
                action="store_true",
                help="also run codex features list as a parse check",
            )

    for name in ("audit", "preview"):
        common(commands.add_parser(name))
    common(commands.add_parser("apply"), confirmation=True, run_codex=True)
    common(commands.add_parser("verify"), run_codex=True)
    common(commands.add_parser("sync"), confirmation=True, run_codex=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument(
        "--codex-home",
        metavar="PATH",
        help="global Codex home; defaults to CODEX_HOME or ~/.codex",
    )
    rollback.add_argument("--backup", required=True, metavar="PATH")
    tiers = commands.add_parser("tiers")
    tiers.add_argument("action", choices=("list", "check"))
    tiers.add_argument(
        "--codex-home",
        metavar="PATH",
        help="global Codex home; defaults to CODEX_HOME or ~/.codex",
    )
    tiers.add_argument("--profile", choices=tuple(PROFILE_ALIASES), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        _require_tomllib()
        home = _home_from_arg(getattr(args, "codex_home", None))
        if args.command is None:
            return _cmd_audit(home, _profile(None, home=home), preview=True)
        if args.command == "rollback":
            restored = _rollback_manifest(home, args.backup)
            print(f"ROLLBACK OK: restored {restored} manifest file(s)")
            print("Note: rollback restores the backup scope and may overwrite later edits to those files.")
            return 0
        profile = _profile(
            getattr(args, "profile", None),
            home=home,
            auto=args.command in {"sync", "verify", "tiers"},
        )
        if args.command == "audit":
            return _cmd_audit(home, profile)
        if args.command == "preview":
            return _cmd_audit(home, profile, preview=True)
        if args.command == "apply":
            return _cmd_apply(home, profile, yes=args.yes, run_codex=args.run_codex)
        if args.command == "sync":
            return _cmd_apply(
                home,
                profile,
                yes=args.yes,
                run_codex=args.run_codex,
                sync_only=True,
            )
        if args.command == "verify":
            return _cmd_verify(home, profile, args.run_codex)
        if args.command == "tiers":
            return _cmd_tiers(home, args.action, profile)
        raise ConfigureError(f"unknown command: {args.command}")
    except ConfigureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: filesystem operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
