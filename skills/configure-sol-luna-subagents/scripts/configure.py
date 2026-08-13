#!/usr/bin/env python3
"""Safely configure a global two-model Sol/Luna Codex baseline.

The script deliberately edits only files below CODEX_HOME.  It uses a
conservative line-aware merge for config.toml so comments and unrelated
settings remain untouched.
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
from typing import Any, Iterable


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
AGENT_ASSETS = {
    "sol_luna_code_mapper.toml": "sol_luna_code_mapper.toml",
    "sol_luna_implementation_worker.toml": "sol_luna_implementation_worker.toml",
}
AGENT_MARKER_PREFIX = "configure-sol-luna-subagents:"
AGENTS_BEGIN = "<!-- BEGIN MANAGED: configure-sol-luna-subagents -->"
AGENTS_END = "<!-- END MANAGED: configure-sol-luna-subagents -->"

ROOT_VALUES: tuple[tuple[str, Any], ...] = (
    ("model", "gpt-5.6-sol"),
    ("model_reasoning_effort", "max"),
)
AGENTS_VALUES: tuple[tuple[str, Any], ...] = (
    ("enabled", True),
    ("default_subagent_model", "gpt-5.6-luna"),
    ("default_subagent_reasoning_effort", "max"),
    ("max_depth", 1),
    ("max_concurrent_threads_per_session", 3),
)


class ConfigureError(RuntimeError):
    """A safe, user-actionable configuration error."""


@dataclass(frozen=True)
class Plan:
    home: Path
    config_path: Path
    instruction_path: Path
    agent_paths: tuple[Path, ...]
    config_before: str
    config_after: str
    instruction_before: str
    instruction_after: str
    agent_before_after: tuple[tuple[Path, str, str], ...]

    @property
    def targets(self) -> tuple[Path, ...]:
        return (self.config_path, self.instruction_path) + self.agent_paths


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
        resolved_parent = parent.resolve()
        resolved_parent.relative_to(resolved_root)
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
    raise ConfigureError(f"unsupported native value type: {type(value).__name__}")


def _replace_assignment(line: str, key: str, value: Any) -> str:
    content, ending = _split_line_ending(line)
    code, comment = _split_comment(content)
    match = re.match(
        rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)(?P<rhs>.*)$", code
    )
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


def _parse_toml(
    text: str, label: str, *, allow_multiline_strings: bool = False
) -> dict[str, Any]:
    parser = _require_tomllib()
    if not allow_multiline_strings and ("\"\"\"" in text or "'''" in text):
        raise ConfigureError(
            f"{label} contains a multiline TOML string; safe line-preserving merge is ambiguous"
        )
    try:
        value = parser.loads(text)
    except Exception as exc:  # TOMLDecodeError differs slightly across Python versions.
        raise ConfigureError(f"invalid TOML: {label}") from exc
    if not isinstance(value, dict):
        raise ConfigureError(f"TOML root is not a table: {label}")
    return value


def _config_after(text: str) -> str:
    if not text:
        newline = "\n"
        return (
            "model = \"gpt-5.6-sol\"\n"
            "model_reasoning_effort = \"max\"\n\n"
            "[agents]\n"
            "enabled = true\n"
            "default_subagent_model = \"gpt-5.6-luna\"\n"
            "default_subagent_reasoning_effort = \"max\"\n"
            "max_depth = 1\n"
            "max_concurrent_threads_per_session = 3\n"
        )

    parsed = _parse_toml(text, CONFIG_NAME)
    lines = text.splitlines(keepends=True)
    newline = _newline_for(text)
    first_header, sections = _section_ranges(lines)
    root_assignments = _assignments(lines, 0, first_header)
    replacements: dict[int, str] = {}
    missing_root: list[tuple[str, Any]] = []
    for key, value in ROOT_VALUES:
        if key in parsed:
            if key not in root_assignments:
                raise ConfigureError(f"root key {key!r} uses an unsupported complex layout")
            replacements[root_assignments[key]] = _replace_assignment(
                lines[root_assignments[key]], key, value
            )
        else:
            missing_root.append((key, value))

    agents_value = parsed.get("agents")
    agent_section = [
        section for section in sections if section[2] == "table" and section[3] == "agents"
    ]
    if len(agent_section) > 1:
        raise ConfigureError("ambiguous duplicate [agents] sections")
    if agent_section:
        agent_start, agent_end, _, _ = agent_section[0]
        agent_assignments = _assignments(lines, agent_start + 1, agent_end)
        replacements_agents: dict[int, str] = {}
        missing_agents: list[tuple[str, Any]] = []
        for key, value in AGENTS_VALUES:
            present = isinstance(agents_value, dict) and key in agents_value
            if present:
                if key not in agent_assignments:
                    raise ConfigureError(f"[agents] key {key!r} uses an unsupported complex layout")
                replacements_agents[agent_assignments[key]] = _replace_assignment(
                    lines[agent_assignments[key]], key, value
                )
            else:
                missing_agents.append((key, value))
        if isinstance(agents_value, dict) and "max_threads" in agents_value:
            if "max_threads" not in agent_assignments:
                raise ConfigureError("[agents].max_threads uses an unsupported complex layout")
            remove_index = agent_assignments["max_threads"]
            _replace_assignment(lines[remove_index], "max_threads", 0)
        else:
            remove_index = None
        replacements_all = {**replacements, **replacements_agents}
        new_lines: list[str] = []
        for index, line in enumerate(lines):
            if index == remove_index:
                continue
            new_lines.append(replacements_all.get(index, line))
        if missing_root:
            first_header_after, _ = _section_ranges(new_lines)
            new_lines[first_header_after:first_header_after] = _line_block(missing_root, newline)
        if missing_agents:
            first_after, sections_after = _section_ranges(new_lines)
            exact = [
                section
                for section in sections_after
                if section[2] == "table" and section[3] == "agents"
            ]
            if len(exact) != 1:
                raise ConfigureError("cannot locate the updated [agents] section")
            insert_at = exact[0][1]
            new_lines[insert_at:insert_at] = _line_block(missing_agents, newline)
        return "".join(new_lines)

    if agents_value is not None:
        raise ConfigureError("agents is not a simple [agents] table; refusing to guess")
    new_lines = [replacements.get(index, line) for index, line in enumerate(lines)]
    if missing_root:
        first_header_after, _ = _section_ranges(new_lines)
        new_lines[first_header_after:first_header_after] = _line_block(missing_root, newline)
    return _append_section("".join(new_lines), AGENTS_VALUES, newline)


def _replace_marked_block(text: str, begin: str, end: str, replacement: str) -> str:
    newline = _newline_for(text or replacement)
    replacement = replacement.rstrip("\r\n").replace("\n", newline) + newline
    begin_count = text.count(begin)
    end_count = text.count(end)
    if begin_count == 0 and end_count == 0:
        if text and not text.endswith(("\n", "\r")):
            text += "\n"
        return text + replacement
    if begin_count != 1 or end_count != 1:
        raise ConfigureError("managed instruction block has duplicate or incomplete markers")
    begin_at = text.find(begin)
    end_at = text.find(end)
    if end_at <= begin_at:
        raise ConfigureError("managed instruction block markers are out of order")
    start = text.rfind("\n", 0, begin_at) + 1
    end_line = text.find("\n", end_at)
    end = len(text) if end_line == -1 else end_line + 1
    return text[:start] + replacement + text[end:]


def _agent_marker(role: str, side: str) -> str:
    return f"# {side} MANAGED: {AGENT_MARKER_PREFIX}{role}"


def _agent_after(path: Path, asset_text: str, role: str) -> tuple[bool, str, str]:
    existed, before = _read_optional_text(path)
    begin = _agent_marker(role, "BEGIN")
    end = _agent_marker(role, "END")
    if not existed:
        return False, before, asset_text
    if begin not in before and end not in before:
        raise ConfigureError(f"unmanaged agent collision: {path.name}")
    after = _replace_marked_block(before, begin, end, asset_text)
    return True, before, after


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
    agents: dict[str, str] = {}
    for filename, asset_name in AGENT_ASSETS.items():
        path = ASSETS_DIR / asset_name
        if not path.is_file():
            raise ConfigureError(f"missing bundled agent asset: {asset_name}")
        agents[filename] = _read_text(path)
    agents_block_path = ASSETS_DIR / "AGENTS.block.md"
    if not agents_block_path.is_file():
        raise ConfigureError("missing bundled AGENTS block asset")
    block = _read_text(agents_block_path)
    if AGENTS_BEGIN not in block or AGENTS_END not in block:
        raise ConfigureError("bundled AGENTS block markers are incomplete")
    return block, agents


def _build_plan(home: Path) -> Plan:
    block, agents = _assets()
    config_path = home / CONFIG_NAME
    config_exists, config_before = _read_optional_text(config_path)
    config_after = _config_after(config_before) if config_exists else _config_after("")
    instruction_path = _active_instruction_path(home)
    instruction_exists, instruction_before = _read_optional_text(instruction_path)
    instruction_after = _replace_marked_block(
        instruction_before if instruction_exists else "", AGENTS_BEGIN, AGENTS_END, block
    )
    _assert_directory(home / "agents")
    agent_paths: list[Path] = []
    agent_before_after: list[tuple[Path, str, str]] = []
    for filename, asset in agents.items():
        role = Path(filename).stem
        path = home / "agents" / filename
        existed, before, after = _agent_after(path, asset, role)
        agent_paths.append(path)
        agent_before_after.append((path, before, after))
        del existed
    return Plan(
        home=home,
        config_path=config_path,
        instruction_path=instruction_path,
        agent_paths=tuple(agent_paths),
        config_before=config_before,
        config_after=config_after,
        instruction_before=instruction_before,
        instruction_after=instruction_after,
        agent_before_after=tuple(agent_before_after),
    )


def _validate_config(text: str) -> None:
    parsed = _parse_toml(text, CONFIG_NAME)
    if parsed.get("model") != "gpt-5.6-sol":
        raise ConfigureError("config.toml root model is not gpt-5.6-sol")
    if parsed.get("model_reasoning_effort") != "max":
        raise ConfigureError("config.toml root reasoning effort is not max")
    agents = parsed.get("agents")
    if not isinstance(agents, dict):
        raise ConfigureError("config.toml has no [agents] table")
    expected = dict(AGENTS_VALUES)
    for key, value in expected.items():
        if agents.get(key) != value:
            raise ConfigureError(f"[agents].{key} has an unexpected value")
    if "max_threads" in agents:
        raise ConfigureError("legacy [agents].max_threads is still active")


def _validate_agent(path: Path, expected_name: str, effort: str) -> None:
    text = _read_text(path)
    role = Path(path).stem
    begin = _agent_marker(role, "BEGIN")
    end = _agent_marker(role, "END")
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ConfigureError(f"managed agent markers are incomplete: {path.name}")
    parsed = _parse_toml(text, path.name, allow_multiline_strings=True)
    if parsed.get("name") != expected_name:
        raise ConfigureError(f"unexpected agent name: {path.name}")
    if parsed.get("model") != "gpt-5.6-luna":
        raise ConfigureError(f"unexpected agent model: {path.name}")
    if parsed.get("model_reasoning_effort") != effort:
        raise ConfigureError(f"unexpected agent reasoning effort: {path.name}")
    if path.name.endswith("code_mapper.toml") and parsed.get("sandbox_mode") != "read-only":
        raise ConfigureError(f"code mapper is not read-only: {path.name}")
    if path.name.endswith("implementation_worker.toml") and parsed.get("sandbox_mode") != "workspace-write":
        raise ConfigureError(f"implementation worker is not workspace-write: {path.name}")
    if not isinstance(parsed.get("developer_instructions"), str):
        raise ConfigureError(f"agent instructions are missing: {path.name}")


def _validate_agents_block(path: Path) -> None:
    text = _read_text(path)
    if text.count(AGENTS_BEGIN) != 1 or text.count(AGENTS_END) != 1:
        raise ConfigureError(f"active AGENTS managed block is incomplete: {path.name}")


def _verify_static(home: Path) -> Plan:
    plan = _build_plan(home)
    if not plan.config_path.is_file():
        raise ConfigureError("global config.toml is missing")
    _validate_config(_read_text(plan.config_path))
    _validate_agents_block(plan.instruction_path)
    _validate_agent(plan.agent_paths[0], "sol_luna_code_mapper", "medium")
    _validate_agent(plan.agent_paths[1], "sol_luna_implementation_worker", "max")
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
    _assert_safe_parent(root / "placeholder", plan.home)
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
        "schema": 1,
        "codex_home": str(plan.home),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": records,
    }
    _atomic_write(
        backup / "manifest.json",
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
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
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
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
    records = manifest["files"]
    validated: list[tuple[dict[str, Any], Path, Path | None]] = []
    for record in records:
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
        raise ConfigureError("codex features list could not load the global configuration")


def _status(path: Path) -> str:
    if path.is_file():
        return "present"
    return "missing"


def _print_audit(plan: Plan) -> None:
    print("AUDIT OK")
    print(f"CODEX_HOME: {plan.home}")
    print(f"config.toml: {_status(plan.config_path)}")
    print(f"active instructions: {plan.instruction_path.name} ({_status(plan.instruction_path)})")
    for path in plan.agent_paths:
        print(f"agent {path.name}: {_status(path)}")
    print("planned scope: global config, active AGENTS file, and two namespaced agents")


def _cmd_audit(home: Path) -> int:
    _print_audit(_build_plan(home))
    return 0


def _cmd_apply(home: Path, run_codex: bool) -> int:
    _ensure_home_for_apply(home)
    plan = _build_plan(home)
    backup, _ = _timestamped_backup(plan)
    try:
        writes: list[tuple[Path, str]] = [(plan.config_path, plan.config_after), (plan.instruction_path, plan.instruction_after)]
        writes.extend((path, after) for path, _, after in plan.agent_before_after)
        changed = 0
        for path, content in writes:
            if not path.exists() or _read_text(path) != content:
                _atomic_write(
                    path,
                    content.encode("utf-8"),
                    mode=_target_mode(path),
                    root=home,
                )
                changed += 1
        _verify_static(home)
        if run_codex:
            _run_codex_check(home)
    except Exception as exc:
        try:
            _rollback_manifest(home, str(backup))
        except Exception as rollback_exc:
            raise ConfigureError(
                f"apply validation failed ({exc}); automatic rollback also failed ({rollback_exc})"
            ) from exc
        if isinstance(exc, ConfigureError):
            raise ConfigureError(f"apply validation failed; automatic rollback completed: {exc}") from exc
        raise ConfigureError(f"apply failed; automatic rollback completed: {exc}") from exc
    print(f"APPLY OK: changed {changed} file(s)")
    print(f"backup: {backup}")
    print("next step: run verify --run-codex, then start a new Codex session or restart the client")
    return 0


def _cmd_verify(home: Path, run_codex: bool) -> int:
    _verify_static(home)
    if run_codex:
        _run_codex_check(home)
    print("VERIFY OK: static configuration and managed files are valid")
    if run_codex:
        print("codex load: OK")
    return 0


def _add_home_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--codex-home",
        metavar="PATH",
        help="global Codex home; defaults to CODEX_HOME or ~/.codex",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "apply", "verify"):
        command = commands.add_parser(name)
        _add_home_option(command)
        if name in ("apply", "verify"):
            command.add_argument(
                "--run-codex",
                action="store_true",
                help="also run codex features list; no model task is started",
            )
    rollback = commands.add_parser("rollback")
    _add_home_option(rollback)
    rollback.add_argument("--backup", required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        _require_tomllib()
        home = _home_from_arg(args.codex_home)
        if args.command == "audit":
            return _cmd_audit(home)
        if args.command == "apply":
            return _cmd_apply(home, args.run_codex)
        if args.command == "verify":
            return _cmd_verify(home, args.run_codex)
        if args.command == "rollback":
            restored = _rollback_manifest(home, args.backup)
            print(f"ROLLBACK OK: restored {restored} manifest file(s)")
            return 0
        raise ConfigureError(f"unknown command: {args.command}")
    except ConfigureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: filesystem operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
