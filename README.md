[English](README.md) | [简体中文](README.zh-CN.md)

# Configure Sol → Luna Subagents

This repository provides a cross-platform Codex skill for a small global
subagent baseline: the main thread uses Sol, and two narrowly scoped agents use
Luna. It changes only the global Codex home. It does not scan or edit project
directories, project instructions, skills, hooks, MCP servers, providers,
trust settings, approvals, or sandbox settings.

The skill is intended for Linux, macOS, and Windows. Python 3.11 or newer is
recommended because it includes the standard-library `tomllib` parser. Python
3.10 is also supported when `tomli` is already installed. The skill never
installs dependencies automatically; report the missing parser if neither is
available.
Model access must already be available to the Codex client.

## Install and use

The final skill URL is:

`https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents`

### One-message entry point

Ask Codex:

`$skill-installer Install <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents> and immediately run configure-sol-luna-subagents according to its SKILL.md.`

This relies on the current Codex client discovering and reading the newly
installed skill in the same turn. If skill discovery is delayed, use the
standard two-step entry point instead.

### Standard two-step entry point

First turn:

`$skill-installer Install <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents>`

Next turn:

`$configure-sol-luna-subagents`

The skill defaults to the global install action. It identifies `CODEX_HOME`
from the environment, or falls back to `~/.codex`. Invocation is authorization
for this exact global configuration change, so it does not ask for a second
confirmation. It stops instead when it finds an invalid TOML file, an
unmanaged collision with either namespaced agent, a malformed managed block, or
another ambiguity that could make a safe merge impossible.

After a successful install, start a new Codex session or restart the client so
the global configuration is loaded. Existing sessions do not retroactively
change their already-loaded agent configuration.

## Resulting baseline

- Main thread: `gpt-5.6-sol`, maximum reasoning.
- `sol_luna_code_mapper`: `gpt-5.6-luna`, medium reasoning, read-only.
- `sol_luna_implementation_worker`: `gpt-5.6-luna`, maximum reasoning, workspace-write.
- At most one implementation writer is active.
- The Luna worker may receive one focused correction for a concrete defect or
  failed verification; if it still cannot finish, the Sol main thread reviews,
  replans, or completes the task.
- Subagents do not gain authority to delegate, access remote systems, install
  packages, or perform destructive actions.

The native configuration also removes the active `[agents].max_threads` legacy
key and sets `max_depth = 1` plus
`max_concurrent_threads_per_session = 3`. Unrelated configuration is retained.

## Manual commands

From the skill directory:

```bash
python scripts/configure.py audit
python scripts/configure.py apply --run-codex
python scripts/configure.py verify
python scripts/configure.py rollback --backup ~/.codex/backups/configure-sol-luna-subagents/<UTC-timestamp>
```

`apply --run-codex` creates a timestamped, file-scoped backup before writing and
validates `codex features list` within the same transaction; if validation fails,
it automatically rolls back. `rollback` restores only the files listed in that
backup manifest and removes files that the installation created. `verify`
performs the final static checks without starting a model task.

See [简体中文说明](README.zh-CN.md) for the Chinese version.
