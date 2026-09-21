<!-- BEGIN MANAGED: configure-sol-luna-subagents -->
## Global T1 → T2/T3 Subagent Baseline

- The T1 main thread owns planning, orchestration, authorization, independent review, and final decisions. The model and effort are selected by the managed tier registry.
- Use the mapped T2 `code_mapper` (or the installed `sol_luna_code_mapper` compatibility name) only for non-trivial local discovery; do not delegate trivial reads or waiting.
- Use one mapped T2 `implementation_worker` (or the installed `sol_luna_implementation_worker` compatibility name) for an exact delegated local task. It is the sole write-capable worker and may receive at most one focused correction for a concrete defect or failed verification.
- Use the mapped T3 `routine_state_checker` only for one bounded, read-only healthy-state snapshot. It never waits, polls, diagnoses, repairs, starts, stops, or modifies anything.
- If a task still cannot finish, return to the T1 main thread for review, re-planning, or final implementation. Do not mechanically retry or run parallel writers.
- Subagents do not expand authorization. They do not delegate further, access remote systems, install packages, or perform destructive actions.
- The T1 main thread independently reviews the final diff or artifact and fresh verification evidence.
- `sol_luna_code_mapper` and `sol_luna_implementation_worker` are retained compatibility filenames for the selectable legacy profile, not the architecture.
<!-- END MANAGED: configure-sol-luna-subagents -->
