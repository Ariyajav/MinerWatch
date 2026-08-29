# MinerWatch — Architect Rules

Conventions for AI coding agents working in this repository. Human contributors
want [AGENTS.md](AGENTS.md), which covers the cross-platform and safety rules
that actually constrain the code.

## Workflow per feature

1. **PLAN** — produce a numbered plan before editing anything.
2. **IMPLEMENT** — work the plan, running the tests at each step.
3. **REVIEW** — review the committed diff. Anything that can actuate hardware
   (the sleep backends, the watchdog, `install-task.ps1`) gets a second
   adversarial pass, with a concrete failing scenario for each finding.

## Hard rules

- Feature branches only. Never commit to `master`/`main`, never force-push.
- Never touch `.env*`, `miners.yaml`, or credentials of any kind.
- Before any task, run the full suite to confirm a green baseline.
- Use the virtual environment's interpreter for every Python command.
- State from commands, not memory — `git status`, `git branch`, `ls` before
  acting.
- Every new actuator is rehearsed by default. Record the intent in the event
  log, run the full state machine in dry-run, and send bytes only when
  explicitly enabled.
