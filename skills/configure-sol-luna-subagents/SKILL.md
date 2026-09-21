---
name: configure-sol-luna-subagents
description: Configure a portable global Astra-to-Luna Codex baseline with tier-backed roles, one writer, and read-only mapper/checker while preserving unrelated global settings.
---

# Configure Astra → Luna Subagents

Use this skill primarily when the user explicitly invokes
`$configure-sol-luna-subagents`. The safe default is a read-only preview. Do
not scan or edit projects.

## Scope and authorization

The skill identifies the personal global Codex home from `CODEX_HOME`; when
unset, it uses `~/.codex`. It writes only that home: native `config.toml`, the
active global `AGENTS.md`/`AGENTS.override.md` managed block, portable
`model-tiers.toml` and `agent-tiers.toml`, and managed personal agent files
under `agents/`.

Codex also supports project-local `.codex/agents/*.toml`, but project bindings
and templates are opt-in work and this skill never discovers or edits them.
It does not modify provider definitions, credentials, hooks, MCP servers,
trust, approvals, or project registrations. Training/remote-job cadence and
TensorBoard guidance are not installed as global policy.

The model registry selects provider/model and supported effort only. Sandbox,
instructions, and authority stay in each role TOML. The default profile is
T1/main `gpt-6-astra`/medium, T2 Luna/max for the default, mapper, and sole
implementation writer, and T3 Luna/max for the read-only routine state
checker. `sol_luna_*` names remain compatibility filenames for the selectable
legacy Sol profile, not the current architecture.

Model and effort availability must be checked against the actual Codex client
and account before applying. Never silently substitute an unavailable model
or effort: ask the user first. Static verification and `codex features list`
only test configuration parsing; they are not a paid live model smoke test.

## Workflow

1. Read this skill and locate its bundled `scripts/configure.py` and assets.
   Prefer Python 3.11+; Python 3.10 is supported only when `tomli` is already
   importable. Do not install dependencies.
2. Run `python scripts/configure.py preview` (or `audit`). It prints an
   allowlisted scope summary and resolved model/effort/provider values without
   printing raw configuration.
3. Show the preview and ask the user for approval immediately before writing.
   When the request is a preference rather than a safety gate, use the
   client's one-to-three-question input UI when available; otherwise ask in
   ordinary dialogue. `--yes` is valid only after that approval for the exact
   displayed plan; it is not a substitute for approval. Non-interactive apply
   without `--yes` fails closed.
4. The script creates a UTC, file-scoped backup, performs line-aware updates,
   validates the managed registry/config/agents, and rolls back that backup if
   validation fails. It rejects invalid TOML, unmanaged collisions, malformed
   markers, provider mismatches, and ambiguous canonical/compatibility role
   files before writing.
5. Run `python scripts/configure.py verify`. This is static validation only;
   it does not start a model task. Start a new Codex session or restart the
   client so the loaded configuration is refreshed.

Typical commands:

```text
python scripts/configure.py              # preview, no write
python scripts/configure.py audit
python scripts/configure.py apply --yes
python scripts/configure.py verify
python scripts/configure.py tiers list
python scripts/configure.py tiers check
python scripts/configure.py sync --yes
python scripts/configure.py rollback --backup <backup-path>
```

`sync` reads editable registries and materializes their model/effort values
into native config and managed agents without rewriting the registries. Used
tier providers must agree with the effective main provider; provider
definitions and credentials are never changed.

## Native and role contract

- The default profile binds T1/main to `gpt-6-astra`/medium.
- T2 binds `default_subagent`, `code_mapper`, and `implementation_worker` to
  Luna/max; T3 binds `routine_state_checker` to Luna/max.
- `code_mapper` and `routine_state_checker` use `read-only` sandbox mode.
- `implementation_worker` uses `workspace-write` and is the only writer.
- At most one writer is described in the managed instruction block; workers do
  not delegate further or gain remote, destructive, package-install, or
  external-system authority.
- Existing unrelated config and role content remains outside the marked
  blocks. Existing registries without this skill's markers are not guessed at
  or overwritten.

To select the compatibility preset explicitly:

```text
python scripts/configure.py apply --profile sol-luna --yes
```

An existing managed legacy installation is upgraded in place so the old agent
filenames remain usable without creating a second implementation writer.

For rollback, use the exact backup path. Rollback restores the recorded scope
and can overwrite later edits to those files; review it immediately before use.
