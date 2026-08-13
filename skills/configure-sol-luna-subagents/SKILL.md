---
name: configure-sol-luna-subagents
description: Configure a conservative global Codex Sol main thread with two namespaced Luna subagents, preserving unrelated settings and project files.
---

# Configure Sol → Luna Subagents

Use this skill primarily when the user explicitly invokes
`$configure-sol-luna-subagents`. The default action is `install`: audit the
global Codex home, apply the exact Sol → Luna baseline, and verify it. Do not
scan or edit projects.

## Scope and authorization

The skill identifies the active global Codex home from `CODEX_HOME`; when that
variable is unset, use `~/.codex`. The invocation itself authorizes this exact
global installation, so do not ask for a second confirmation. Stop before any
write if an existing same-named agent is unmanaged, TOML is invalid, a managed
block is malformed, or the active instruction file cannot be determined safely.

This skill does not configure project-local files, additional registries,
providers, hooks, MCP servers, trust, approvals, or sandbox settings.
Model access must already be available to Codex.

## Workflow

1. Read this skill and locate its bundled `scripts/configure.py` and assets.
   Prefer an available Python 3.11+ interpreter. If only Python 3.10 is
   available, check that `tomli` can be imported. Do not install dependencies;
   if neither `tomllib` nor `tomli` is available, report the prerequisite and
   stop.
2. Run `python scripts/configure.py audit`. Report only a scope summary; never
   print existing configuration contents.
3. Run `python scripts/configure.py apply --run-codex`. The deterministic script parses the
   current config before editing, creates a UTC timestamped backup under
   `$CODEX_HOME/backups/configure-sol-luna-subagents/`, performs atomic,
   section-aware updates, and automatically rolls back that backup if static or
   requested client validation fails.
4. Run `python scripts/configure.py verify`. This performs the final static
   checks after the transactional apply; it does not start a model task.
5. Respond in the user's language with the result, the backup path, the exact
   global-only scope, any error or uncertainty, and the need to start a new
   Codex session or restart the client. Existing sessions do not retroactively
   load the new configuration.

For a read-only check, run `audit` or `verify` without `apply`. To undo an
installation, use the exact backup path with:

```text
python scripts/configure.py rollback --backup <backup-path>
```

## Native target

The script changes only these native settings:

- Root `model = "gpt-5.6-sol"` and `model_reasoning_effort = "max"`.
- `[agents].enabled = true`.
- `[agents].default_subagent_model = "gpt-5.6-luna"` and
  `default_subagent_reasoning_effort = "max"`.
- `[agents].max_depth = 1` and
  `max_concurrent_threads_per_session = 3`.
- Removes `[agents].max_threads` when it is a direct legacy key, avoiding its
  incompatibility with `multi_agent_v2`.
- Creates or updates only the namespaced
  `sol_luna_code_mapper` (Luna/medium/read-only) and
  `sol_luna_implementation_worker` (Luna/max/workspace-write).
- Adds or replaces the marked block in the active `AGENTS.override.md` when it
  is non-empty; otherwise it uses `AGENTS.md`.

Unrelated keys, comments, features, hooks, and providers remain unchanged.
The implementation worker is the only writer, may receive one focused
correction for a concrete defect or failed verification, and returns to the Sol
main thread if it still cannot finish. No worker delegates further or performs
remote or destructive actions.
