# Operator guide

Running MinerWatch against real hardware, from first install to a scheduled
service that survives a reboot.

## Roll out in four phases

Each phase is provable before the next, and the first three cannot touch
hardware.

1. **Fake miners.** `scripts/sim.ps1 -WithHttp` (or run the simulators
   directly) plus `demo.yaml`. A full sleep/wake cycle against simulators that
   speak both real protocols. Zero risk.
2. **Real miners, watch only.** Put real addresses in `miners.yaml`, leave
   `sleep.enabled: false`. This confirms reachability and that the `WINDOW`
   column in `config` matches a clock.
3. **Rehearse.** `sleep.enabled: true`, `dry_run: true`. Run across a real
   window boundary and look for exactly one `would_sleep` and one `would_wake`
   per edge — not one per poll.
4. **Live.** A manual `sleep`/`wake --live` on one miner first, then the
   scheduled task in `as-configured` mode.

## Getting started

```bash
cp miners.example.yaml miners.yaml     # then edit it for your fleet
python -m minerwatch -c miners.yaml config    # what did that actually resolve to?
python -m minerwatch -c miners.yaml check     # can we reach each sleep backend?
python -m minerwatch -c miners.yaml run       # supervision loop, rehearsing
```

`miners.yaml` is gitignored — it holds your addresses and web-UI credentials.

On Windows, spell out the virtual environment's interpreter:

```powershell
.\.venv\Scripts\python.exe -m minerwatch -c miners.yaml <subcommand>
```

`-m minerwatch` is the canonical form and does not depend on an entry-point
script existing; `pip install -e .` does not reliably write
`.venv\Scripts\minerwatch.exe`. A bare `python` picks the system interpreter,
which cannot see `.venv` and fails with `No module named 'yaml'`.

Subcommands: `run`, `status`, `config`, `check`, `diagnose`, `history`,
`sleep`, `wake`, `clear-attention`.

## Read the resolved config, not the file

Inheritance makes mistakes invisible in the file itself — a miner in the wrong
group, or a `sleep:` block that was never enabled. `config` prints what each
miner actually resolved to:

```
MINER      GROUP      ADDRESS          SLEEP                RUNNING HOURS
miner-01   hall-b     10.0.0.1:4028    bitmain_http live    every day 21:00-18:00
miner-03   hall-a     10.0.0.3:4028    off (monitor only)   every day 00:00-24:00

NOTE: software sleep is off for 1 of 2 miner(s):
      miner-03
      These are polled and watchdogged, but never slept or woken.
```

**Check the GROUP column before editing groups.** Group names and miner-id
prefixes are independent. Assuming they match is the easy way to arm the wrong
half of a fleet — and near-identical `sleep:` blocks are easy to swap, which
turns a night's sleep off with nothing but the `config` table to show it.
Verify each hit under its own group heading:

```powershell
Select-String -Path miners.yaml -Pattern "dry_run" -Context 6,0
```

Comment each line with what it **is**, not what to do to it. A comment like
`# drop to false when you're ready` reads as a standing instruction long after
it has been followed.

`config --hours` draws a 7×24 running/asleep map per distinct schedule, which
is the quickest way to check that a window crossing midnight does what you
meant.

## Configuration traps worth knowing

- **`port` is the cgminer API port (4028), not the web port.** Putting 80 there
  makes every miner time out and read as unreachable. `config` lints for it.
- **A full-day window is `"00:00-24:00"`.** `"00:00-23:59"` parses and silently
  drops a minute every day.
- **Miners that run continuously need the full-day window,** not no window and
  not a narrow one. Outside its window the watchdog treats a genuine failure as
  `expected_off`, so a too-narrow window leaves a miner unmonitored for the
  remaining hours.
- **`normal_value` hazard.** For any miner *meant* to sit in a reduced power
  mode, a wake writing `normal_value: 0` pushes it back to full power. Confirm
  against the miner's own web UI before enabling sleep on it. A deliberately
  underclocked group is the standard reason to leave sleep off entirely.
- **Restarts are safe on an underclocked miner.** A cgminer `restart` restarts
  the mining process; it does not alter the work mode, so the miner comes back
  in the same configuration.
- **`grace_seconds` must cover a real spin-up.** A work-mode change reboots the
  mining process, and an S19 XP has been measured still at 0 TH/s seven minutes
  after a verified wake. `900` is a reasonable floor for `bitmain_http`.

## Reading the state

`status` reads the **database**, not the miners — it prints the last recorded
poll:

```
MINER   STATE        HASHRATE  WINDOW   POWER    ATTENTION  LAST SEEN
m-01    mining      95.2 TH/s  open     awake    -          2026-08-25T16:56:03+00:00
m-04    stopped             0  closed   asleep   -          2026-08-25T16:56:03+00:00
m-05    unreachable         -  open     manual   yes        2026-08-25T16:56:03+00:00
```

`0` and `-` are deliberately different: one means the miner reported zero, the
other that it never answered.

**Identical `LAST SEEN` values across two invocations mean nothing has polled
since** — which usually means the service is not running. `run --once`
refreshes it, but a bare `run --once` honours the config and can actuate; use
`run --once --dry-run` to refresh safely.

**`ATTENTION` is the column that tells you a miner has latched.** A fleet-wide
latch is invisible everywhere else until you look at `history`.

## Reading the log

INFO records only startup lines, state transitions and power actions. Between
window edges the log is legitimately silent — indistinguishable from a dead
process. To confirm the service is alive, check that `status` timestamps are
advancing, not that the log has new lines.

For what actually happened, replay the events table:

```bash
python -m minerwatch -c miners.yaml history <miner|all> --decisions --hours 24
```

`--decisions` hides the routine per-poll readings and collapses runs of
identical decisions. It distinguishes "never polled" from "polled all day and
never acted on" — those look identical in a silent log and are different faults.

## Running unattended on Windows

`scripts/install-task.ps1` registers a Task Scheduler entry that runs at boot,
restarts itself every 5 minutes if it dies, runs as SYSTEM (so it needs no
stored password and survives logout), and logs to `logs\minerwatch.log`.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Mode as-configured
```

| Mode | What actuates |
| --- | --- |
| `rehearse` (default) | nothing — forces `--dry-run` over the config |
| `as-configured` | whichever miners set `dry_run: false` |
| `live-sleep` | **every** enabled miner, config ignored |
| `live` | every enabled miner, plus real watchdog restarts |

`live-sleep` overrides per-miner settings, so a fleet with a deliberate
live/rehearsing split needs `as-configured`, not `live-sleep`. Before
registering anything the installer parses `miners.yaml`, names every miner the
chosen mode would actuate, and asks for confirmation — a task whose config has
a typo would otherwise die at boot with exit code 2 and no console to say why.

### After any config or code change, restart the task

The process holds the config it started with; `miners.yaml` is read once, at
startup, and there is no hot reload.

```powershell
.\.venv\Scripts\python.exe -m minerwatch -c miners.yaml status   # validates the file
Stop-ScheduledTask -TaskName MinerWatch
Start-ScheduledTask -TaskName MinerWatch
```

Then check the startup line names the miners you expect:
`Sleep is LIVE for: miner-01, miner-02`. That one line is the difference
between a scheduled sleep happening tonight and being rehearsed.

Nothing is lost across a restart — every latch is rebuilt from the event log.
The exception is renaming a miner's `id`, which orphans its history; wake a
miner before renaming it.

Re-registering the task stops it, so start it again afterwards.
`LastTaskResult 267014` (`0x41306`) means *terminated by the user*, not a
crash. An empty `NextRunTime` is normal for a boot trigger.

## Recovering a latched miner

```bash
python -m minerwatch -c miners.yaml clear-attention <miner|all>
```

This releases both the watchdog and sleep latches and restores the retry
budget. **Diagnose before clearing** — clearing without knowing why a miner
stopped just means re-latching an hour later. See
[watchdog.md](watchdog.md#the-latch-seen-in-anger) for what a fleet-wide latch
looks like and why it is usually correct.
