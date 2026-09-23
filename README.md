[English](README.md) | [简体中文](README.zh-CN.md)

# Configure Astra → Luna Subagents

This repository ships an explicit-only Codex skill and a small, portable
global configurator. The default profile is:

- T1/main: `gpt-6-astra`, medium effort.
- T2/default, mapper, and implementation worker: `gpt-5.6-luna`, max effort.
- T3/routine state checker: `gpt-5.6-luna`, max effort.
- `implementation_worker` is the only write-capable role; mapper and routine
  checker are read-only.

The model registry selects provider/model and supported effort only. Sandbox,
instructions, and authority remain in each agent TOML file. The installer
preserves unrelated config, roles, provider definitions, hooks, MCP servers,
and project registrations. It does not scan or edit projects.

Actual model and effort availability is client/account dependent. Before
applying, inspect the models and reasoning efforts supported by your Codex
client. If the requested profile is unavailable, ask the user before choosing
any substitute. A successful static/config parse is not a paid live model
test and does not prove access or billing availability.

## Install the skill

The existing shareable URL and skill entry remain valid:

`https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents`

One-message entry point:

`$skill-installer Install <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents> and then use $configure-sol-luna-subagents.`

Or use the standard two-step flow:

1. `$skill-installer Install <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents>`
2. `$configure-sol-luna-subagents`

The skill remains explicit-only. Installing the skill stores its files under
the client's skill location; it does not apply this global configuration.

## Safe workflow

Run the script from the skill directory. `CODEX_HOME` may be supplied through
the environment or `--codex-home`; tests and examples should use a temporary
directory.

```bash
python scripts/configure.py              # preview; no write
python scripts/configure.py audit        # read-only scope audit
python scripts/configure.py preview     # read-only resolved plan
python scripts/configure.py apply --yes  # explicit confirmation
python scripts/configure.py verify
```

After showing the preview, obtain user approval for the exact plan immediately
before writing. When the client provides a one-to-three-question input UI, use
it for preferences; otherwise ask in ordinary dialogue. `--yes` is only the
confirmation flag after that approval. Without it, `apply`/`sync` asks for an
interactive confirmation; in a non-interactive process it fails closed. Immediately before confirmation, the
preview prints only the exact `CODEX_HOME`, resolved model/effort/provider
values, sandbox modes, and target filenames; it does not print raw config.

Every apply or sync creates a file-scoped UTC backup. Rollback is explicit:

```bash
python scripts/configure.py rollback --backup ~/.codex/backups/configure-sol-luna-subagents/<UTC-timestamp>
```

Rollback restores the recorded scope and may overwrite later edits to those
files. Review the backup path before using it.

## Registries and synchronization

An installation creates portable `model-tiers.toml` and `agent-tiers.toml`
with managed blocks. Existing registries are never overwritten blindly:
unmanaged collisions, malformed TOML, missing markers, and ambiguous role
files stop before any write. Unrelated tiers and roles are retained.

The bundled tier files are editable. After changing a managed tier's model or
effort, inspect the result and run:

```bash
python scripts/configure.py tiers list
python scripts/configure.py tiers check
python scripts/configure.py sync --yes
python scripts/configure.py verify
```

`sync` reads the registries and materializes their model/effort values into
the global config and managed agents; it does not rewrite the registries.
Used tier providers must agree with the effective main provider. Existing
provider definitions and credentials are never changed.

## Profiles and compatibility

The selectable legacy profile is retained:

```bash
python scripts/configure.py apply --profile sol-luna --yes
```

It keeps `gpt-5.6-sol`/max and the historical
`sol_luna_code_mapper`/`sol_luna_implementation_worker` filenames. Those
names are compatibility names, not the current architecture. If an existing
installation has those managed files, the default Astra profile upgrades them
in place and adds the routine checker rather than creating a second writer.
If canonical and compatibility files collide, the installer stops and asks
for an explicit cleanup decision.

## Scope and opt-in customization

The installer writes only the global Codex home: native config, the active
global `AGENTS.md`/`AGENTS.override.md` block, portable tier registries, and
managed personal agent files under `~/.codex/agents/*.toml`. Codex also
supports project-local `.codex/agents/*.toml`; project-specific bindings or
templates are an opt-in task for the user and are not auto-discovered or
modified here.

The personal and project agent locations and per-role model, effort, and
sandbox fields follow the [official subagent configuration documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents).

Training/remote-job cadence and TensorBoard guidance are intentionally not
installed as global policy. Add such project instructions explicitly when a
project needs them.

This package makes no universal model-support or fixed-credit-savings claim;
actual usage depends on task routing, calls, account limits, and pricing.
