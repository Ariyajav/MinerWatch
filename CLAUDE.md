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

<!-- agent-brain:begin. Generated from projects/claude-md-block.md in Ariyajav/agent-brain by bin/subprojects.py block. Edit it there, not here. -->
## agent-brain: how this repo feeds back

This repo is a sub-project of agent-brain (github.com/Ariyajav/agent-brain), which learns from it. Every night agent-brain reads the pull requests merged here and the trailers on their commits, and its Mentor turns repeated friction into rules. You don't need access to agent-brain. Your part is three habits.

1. **Say what you verified, in every commit.** Put a `Verified:` trailer on its own line just above `Co-Authored-By:`: the command you ran and its result, or what couldn't run and why. For example `Verified: make check: 98 passed`, or `Verified: py_compile only; the test suite did not run (no database here)`. Never claim a check you didn't run. A commit without the trailer counts as unverified.
2. **Leave one lesson when something went wrong.** If you redid work, fixed something a recent PR broke, or a guess about an outside system turned out wrong, add one `Agent-Lesson:` trailer: a single imperative sentence about how to work, not about this codebase. Leave it out when nothing went wrong.
3. **Keep facts here, send habits up.** Facts about this code or the systems it talks to (field names, enum values, limits, how it is deployed) go into this CLAUDE.md, in the same pull request that taught you them. Habits that would help in any repo go in the `Agent-Lesson:` trailer.

### Rules from agent-brain's lessons.md
- Check repo and system state with a fresh command before you state it; what you remember may be stale. (2026-07-07)
- Take an outside API's field names, enum values and limits from its spec or a real response, and cite the source in the commit; never infer them from names. (2026-11-05)
- Before adding a check that refuses something that worked before (validation, hardening), list the setups actually in use and test the check against each one. (2026-11-05)
- When you fix a bug that a recent PR introduced, put the regression test in the same PR. (2026-11-05)
- Tests you couldn't run haven't passed: say so in the `Verified:` trailer and the PR description, and let CI run them before merge. (2026-11-05)
- Make dev and CI fail the way production would: the same roles and permissions, log levels, file modes and network paths. Whatever only production has, you can't see until it ships. (2026-11-05)
- Treat the first run on a host you can't reach as a test: a dry-run or plan mode, its assumptions about the host written down, and logs that let someone fix it from the log alone. (2026-11-05)
- A feature ships with the path a user takes to reach it, and a test that walks that path. (2026-11-05)
- Never leave a hand fix on a host: copy it back to the repo and through the normal gate the same day. (2026-11-05)
- Before deleting a branch, retarget the open PRs based on it; GitHub closes them for good when their base disappears. (2026-11-05)
- When a change touches several files or components, run the whole test suite, not only the tests next to the change. (2026-07-10)
- When a reviewer proposes a code change, check it against the library's documentation before adopting it; the diagnosis can be right and the fix wrong. (2026-07-10)
- When a reviewer flags a possible bug you think is by design, run the scenario before dismissing it. (2026-07-08)
- A claim that something is missing is itself a claim to check: search the code before building it. (2026-07-11)
- A commit message describes everything the commit carries: run `git status` before committing. (2026-07-11)
- Minimal means no unrequested code, never fewer tests, error handling, input validation or safety checks. (2026-07-10)
- If a tool result is unreadable, say you can't verify it; never describe output from memory. (2026-07-08)
<!-- agent-brain:end -->
