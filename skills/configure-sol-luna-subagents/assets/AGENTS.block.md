<!-- BEGIN MANAGED: configure-sol-luna-subagents -->
## Global Sol → Luna Subagent Baseline

- The Sol main thread owns planning, orchestration, authorization, independent review, and final decisions.
- Use the Luna mapper only for non-trivial local discovery; do not delegate trivial reads or waiting.
- Use one Luna implementation worker for an exact delegated local task. It may receive at most one focused correction for a concrete defect or failed verification.
- If the task still cannot finish, return to the Sol main thread for review, re-planning, or final implementation. Do not mechanically retry or run parallel writers.
- Subagents do not expand authorization. They do not delegate further, access remote systems, install packages, or perform destructive actions.
- The Sol main thread independently reviews the final diff or artifact and fresh verification evidence.
<!-- END MANAGED: configure-sol-luna-subagents -->
