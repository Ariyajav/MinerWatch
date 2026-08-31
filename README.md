# MinerWatch — Antminer Schedule, Sleep & Watchdog

[![tests](https://github.com/Ariyajav/MinerWatch/actions/workflows/tests.yml/badge.svg)](https://github.com/Ariyajav/MinerWatch/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Monitor Antminer devices, stop and start them **in software** on a schedule,
and restart the ones that genuinely fail. Runs on Linux and on a Windows host.

Cutting power to stop a miner takes the control plane with it: you cannot read
temperatures, and you cannot start it again without a switched PDU. MinerWatch
stops miners the other way — over the network, leaving the controller running —
and puts the whole thing behind two safety gates, because an automation that
can stop a fleet can also stop it by accident.

## Features

- Per-miner and per-group run windows, with time zones
- **Software sleep/wake** — stop a miner without cutting power, so it stays
  reachable and can be woken over the network
- Two backends: the cgminer API (Vnish, Braiins OS+) and the Bitmain stock
  firmware web UI, with automatic discovery of the power-mode field
- **Every write is verified by read-back** — stock firmware answers `OK!` to
  changes it then discards
- Watchdog with a continuous-failure clock, cooldown, rate limiting, and a
  manual-attention latch
- **Two recovery mechanisms** — the cgminer `restart`, or a control-board
  reboot through the stock web UI for firmware that has no `restart` at all,
  with automatic escalation between them
- SQLite event log that survives restarts — every latch is rebuilt from it, so
  a process started twenty minutes before a scheduled wake still knows what it
  owes each miner
- Hardware-free simulators for both control paths
- Dry-run by default: nothing reaches a miner until you say so twice

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | How the poller, sleeper, watchdog and event log fit together, and why the ordering matters |
| [docs/operating.md](docs/operating.md) | Rolling out to real hardware in four phases, reading the state, running as a service |
| [docs/watchdog.md](docs/watchdog.md) | The restart decision, the failure clock, and what the attention latch is telling you |
| [docs/windows.md](docs/windows.md) | Windows host support, and field notes on the S19 XP read/write asymmetry |
| [AGENTS.md](AGENTS.md) | Conventions for contributors — cross-platform and safety rules |

## Requirements

Python **3.10 or newer** (verified on 3.10, 3.11, 3.12 and 3.13). On Windows the
`tzdata` package is also required and is installed automatically — Windows ships
no IANA time zone database, so without it every schedule lookup fails.

## Quick start

Start from the example config. `miners.yaml` is gitignored, because it holds
your addresses and web-UI credentials:

```bash
cp miners.example.yaml miners.yaml     # then edit it for your fleet
```

Or try it with no hardware at all — `demo.yaml` points at the bundled
simulators and runs a full sleep/wake cycle:

```bash
python -m sim.miner_sim --port 4101 &        # inside its window: left alone
python -m sim.miner_sim --port 4102 &        # outside its window: gets slept
python -m minerwatch -c demo.yaml run --once
python -m minerwatch -c demo.yaml status
```

On Windows, `scripts\sim.ps1 -WithHttp` starts all three simulators — including
the fake Bitmain web UI on port 4103 — and wires them together.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # venv + deps + tests
powershell -ExecutionPolicy Bypass -File scripts\sim.ps1     # fake miners (optional)
.\.venv\Scripts\python.exe -m minerwatch -c miners.yaml status
.\.venv\Scripts\python.exe -m minerwatch -c miners.yaml run
```

> **Use the virtual environment's interpreter, not a bare `python`.** A plain
> `python -m minerwatch` picks the system Python, which cannot see anything
> installed into `.venv` and fails with `No module named 'yaml'`. The launch
> scripts handle this for you; if you invoke Python directly, spell out
> `.\.venv\Scripts\python.exe`. `-m minerwatch` is the canonical form and does
> not depend on entry points; `pip install -e .` usually also writes a
> `.\.venv\Scripts\minerwatch.exe` shorthand, but do not rely on it being there.

### Linux / macOS

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m minerwatch -c miners.yaml run
```

> **Paths differ by platform.** The interpreter is `.venv\Scripts\python.exe`
> on Windows and `.venv/bin/python` elsewhere. Every command below is shown in
> the POSIX form; substitute accordingly.

## Commands

```bash
minerwatch -c miners.yaml run                 # supervision loop (rehearsal)
minerwatch -c miners.yaml run --live-sleep    # real sleep/wake, rehearsed restarts
minerwatch -c miners.yaml run --live          # real everything
minerwatch -c miners.yaml status              # state + hashrate for every miner
minerwatch -c miners.yaml config              # resolved settings, after group inheritance
minerwatch -c miners.yaml config --hours      # ...plus a 7x24 running/asleep map
minerwatch -c miners.yaml check               # read-only: can we reach each sleep backend?
minerwatch -c miners.yaml diagnose miner-01     # which request shape does this firmware honour?
minerwatch -c miners.yaml history all --decisions --hours 24
minerwatch -c miners.yaml sleep miner-01 --live
minerwatch -c miners.yaml wake  all   --live
minerwatch -c miners.yaml clear-attention miner-01
```

Equivalently `python -m minerwatch ...`. The historic form
`python -m minerwatch miners.yaml` still works and means `run`.

## Software sleep

Cutting power to stop a miner takes the control plane with it: you cannot see
temperatures, and you cannot start it again without a switched PDU. A software
sleep leaves the controller running, so the miner still answers its API at zero
hashrate and a later `wake` brings it back.

MinerWatch applies this at the edges of each miner's schedule — asleep when the
window closes, awake when it opens — and records the intent in the event log so
the decision survives a restart of MinerWatch itself.

### Backends

| Backend | Mechanism | Fits |
| --- | --- | --- |
| `cgminer` | JSON over the miner's API port: `ascset 0,sleep` / `0,wake`, falling back to `pause` / `resume` | Vnish, Braiins OS+, and other firmwares that expose a real sleep on the socket |
| `bitmain_http` | Digest-authenticated CGI on the web UI, setting the power-mode field to `1` (sleep) or `0` (normal) | Bitmain stock firmware, S17/S19 generation |

The watchdog has its own equivalent split. Several stock Bitmain builds answer
`Invalid command` to the cgminer `restart`, so every attempt fails and the retry
budget is spent for nothing; `watchdog.recover_with` selects a control-board
reboot instead, or `auto` to escalate only when the firmware says the command
does not exist. See [docs/watchdog.md](docs/watchdog.md#choosing-a-recovery-mechanism).

The `cgminer` backend tries each configured command in order and keeps the
first one the firmware accepts, so a mixed fleet can share one setting. A
rejection is cheap (a normal error reply) and falls through to the next
candidate; a connection failure abandons the chain immediately.

Stock Bitmain firmware generally does **not** implement sleep on the cgminer
socket — use `bitmain_http` there.

Bitmain renamed the power-mode field between firmware generations without
keeping an alias, so the backend discovers it: `miner-mode`,
`bitmain-work-mode`, `work-mode`, `miner_mode`, in that order. `check` reports
which one it found. If a miner uses none of them, `check` lists the fields its
config *does* have, and `sleep.mode_key` points the backend at the right one:

```yaml
sleep:
  backend: bitmain_http
  mode_key: bitmain-work-mode   # only needed when discovery misses
```

The new value is written in whatever JSON type the firmware used — some builds
quote these as strings, and posting an int where a string is expected is
rejected by some CGI handlers and silently ignored by others.

The **values** are firmware-specific too. `0` normal / `1` sleep is the common
pairing and the default, but some builds use a third value for a low-power
mode. Confirm rather than assume: set Work Mode by hand in the miner's own web
UI, run `check`, and read back the number the firmware chose.

```yaml
sleep:
  backend: bitmain_http
  sleep_value: 3        # only if your firmware disagrees with the default
  normal_value: 0
```

`check` reports `unrecognised` when a miner sits on a value that is neither.

### The read and write field names can differ

Captured from an S19 XP web UI: `get_miner_conf.cgi` returns
`"bitmain-work-mode": "0"`, and saving Work Mode posts `"miner-mode": 1` with
`Content-Type: text/plain`. Echoing the document back under the field it was
*read* from is accepted, answered `"OK!"`, and discarded — read/write symmetry
is simply not a property of this API.

So `write_profile` (default `auto`) tries the conservative shape first — echo it
back unchanged — and only if that write does not stick falls back to the
browser's shape: the mode renamed to `miner-mode` as an integer, JSON nulls sent
as `"0"`, and the headers a real save carries. `mirror` and `browser` pin one or
the other once you know which your fleet needs.

### The write is verified, not assumed

Stock firmware answers `{"stats":"success","code":"M000","msg":"OK!"}` to a POST
it then ignores. Seen in the field on an S19 XP: the CGI reported success and
the config still read `bitmain-work-mode=0` a second later. So every write is
read back and confirmed, and a setting that does not persist is a failure:

```
sleep_failed  the miner accepted the change ({"stats":"success"...}) but
              bitmain-work-mode still reads 0, not 1. The setting did not
              persist - this firmware likely wants a different request shape;
              try sleep.post_format: form.
```

`sleep.post_format` switches between the JSON document (default, newer builds)
and the older `_ant_`-prefixed form encoding. When you do not know which a
miner wants, `diagnose` finds out:

```bash
minerwatch -c miners.yaml diagnose sxp-01
```

```
FAIL json, value as-is                bitmain-work-mode still reads '0'
FAIL json, value as int               bitmain-work-mode still reads '0'
FAIL json, nulls as empty strings     bitmain-work-mode still reads '0'
OK   form-encoded                     bitmain-work-mode is now '1' - this shape works

Use this shape. In miners.yaml, under the miner's sleep block:
    post_format: form
```

Trying several shapes is safe because each is verified: one that does not take
is proven to have changed nothing, the run aborts if any field other than the
power mode moves, and the original value is restored at the end. If no shape
works, the firmware does not accept a power-mode change over the CGI and the
`cgminer` backend is the next thing to try.

`diagnose` covers that backend too, and starts read-only. cgminer's `check`
command reports `Exists` and `Access` per command, so whether the firmware
implements a sleep — and whether this host is even allowed to call it — is
answerable before anything is sent:

```
FAIL check ascset    Exists=Y Access=N  <- implemented, but this host lacks
                                           privileged access (api-allow needs W)
OK   check config    Exists=Y Access=Y
FAIL sleep candidates  the firmware reports no usable sleep command over this API
```

`Access=N` is the common and confusing case: the command exists and the miner
refuses it, because `api-allow` grants no `W` to your address. Only commands the
firmware admits to are attempted, and anything sent is undone.

Bitmain's bmminer is a cgminer fork and several builds dropped `check` as well,
answering `Invalid command` to the introspection call itself. Then the
candidates are sent directly and the refusal text is read, which separates the
same three cases:

| Reply | Means |
| --- | --- |
| `Invalid command` | the firmware does not implement it |
| `Access denied` | it does, and this host may not call it — fix `api-allow` |
| `STATUS S` / `I` | accepted |

`restart` and `quit` are never sent: both stop mining, neither is a sleep, and a
diagnostic must not be the thing that takes a miner down.

### Configuration

```yaml
sleep:
  enabled: true
  dry_run: false          # the second gate: until this is false, nothing is sent
  backend: cgminer
  cooldown_seconds: 300   # minimum gap between power actions on one miner
  grace_seconds: 180      # spin-up allowance after a wake before alarming
  max_failures: 3         # consecutive failures before manual attention

groups:
  farm-a:
    sleep:
      backend: bitmain_http
      username: root
      password: secret

miners:
  - id: miner-03
    sleep:
      enabled: false      # this one is never touched
```

Settings merge global → group → miner, the same chain schedules use, so a group
can set credentials once and a single miner can override only the backend.

Because inheritance makes mistakes invisible in the file itself — a miner in the
wrong group, or a `sleep:` block that was never enabled — `config` prints what
each miner actually resolved to, and calls out any miner whose software sleep is
off or whose schedule is missing:

```
MINER      GROUP      ADDRESS          SLEEP                RUNNING HOURS
sxp-01     sxp-day    10.0.0.1:4028    cgminer dry-run      every day 17:00-09:00
sxp-02     sxp-night  10.0.0.2:4028    off (monitor only)   every day 09:00-17:00

NOTE: software sleep is off for 1 of 2 miner(s):
      sxp-02
      These are polled and watchdogged, but never slept or woken.
```

`config --hours` adds a 7x24 map per distinct schedule, which is the quickest
way to check a window that crosses midnight does what you meant.

### How it interacts with the watchdog

A miner MinerWatch put to sleep reads back as `STOPPED` — exactly the condition
that makes the watchdog fire a restart. The sleep controller therefore runs
first each poll and reports whether it owns the miner's current state; when it
does, the watchdog stands down. Concretely:

| Situation | Who acts |
| --- | --- |
| Mining, outside its window | Sleep controller sends sleep |
| Mining shortly after a sleep (within `grace_seconds`) | Nobody (still winding down) |
| Stopped, outside its window, slept by us | Nobody (expected) |
| Stopped, inside its window, slept by us | Sleep controller sends wake |
| Stopped shortly after a wake (within `grace_seconds`) | Nobody (still spinning up) |
| Stopped, inside its window, **not** slept by us | Watchdog restarts it |
| Unreachable | Watchdog |
| Mining well past `grace_seconds` while we believed it asleep | Latch cleared, then watchdog |

`grace_seconds` cuts both ways, and both directions matter. A miner does not
drop to zero hashrate in the same 15-second poll that accepts the sleep
command — the rate decays over tens of seconds — so without the settle window
MinerWatch would conclude on the very next poll that the sleep had not worked,
drop its latch, and then fail to wake the miner when the window reopened. For
`bitmain_http` that is unrecoverable by restart, because `miner-mode` is
persistent: the watchdog would restart a miner that comes straight back up
asleep, three times, and then latch it for manual attention.

Every latch is rebuilt from the event log at startup — asleep, spin-up grace,
cooldown, and failure count — so a MinerWatch restart mid-cycle does not hand a
healthy, still-spinning-up miner to the watchdog.

### Dry run

`dry_run: true` runs the whole state machine — both halves of the cycle, so a
schedule can be validated against real miners — and records `would_sleep` /
`would_wake` without sending anything. Those events are deliberately **not**
read back at startup: a rehearsal must never leave behind a belief that a later
live run treats as fact and acts on. Within a running process the rehearsed
latch is remembered, so a dry run logs one `would_sleep` and one `would_wake`
per window edge rather than one per poll.

### Proving the backend before you need it

A dry run never contacts the backend — it records the intent and returns — so a
fleet can rehearse cleanly for weeks and still fail the first time it goes live,
on a web-UI password nobody ever tested. `check` closes that gap: it
authenticates and reads, and changes nothing.

```
OK   sxp-01  bitmain_http  http://10.0.0.1:80: authenticated, miner-mode=0 (normal)
FAIL sxp-04  bitmain_http  http://10.0.0.4:80: HTTP 401 - the web-UI username or
                           password is wrong (trying user 'root')
```

For `cgminer` it asks for `version`; for `bitmain_http` it fetches the miner
config. Exit status is non-zero if any backend is unreachable, so it works in a
pre-flight script.

### When a sleep is accepted but does nothing

The backends judge success from the command's own acknowledgement, because
neither protocol reports whether hashing actually stopped. A firmware that ACKs
a sleep it does not implement would therefore loop forever — sleep, settle,
"still hashing", sleep — with every attempt reporting success. MinerWatch
counts those separately from actuation failures and latches the miner for
attention after `max_failures` of them, with a reason naming the likely cause:
the wrong backend for that firmware. Check `status`, then try the other backend
or a different command chain.

### Telling a real sleep from a low-power mode

`status` shows the last hashrate each miner reported, which is the number that
answers "did the sleep actually work?" - a low-power mode also stops full-rate
mining, but only a real sleep goes to zero:

```
MINER   STATE        HASHRATE  WINDOW   POWER    ATTENTION  LAST SEEN
sxp-01  mining      95.2 TH/s  open     awake    -          2026-08-25T16:56:03+00:00
sxp-04  stopped             0  closed   asleep   -          2026-08-25T16:56:03+00:00
s19-01  unreachable         -  open     manual   -          2026-08-25T16:56:03+00:00
```

`0` and `-` are deliberately different: one means the miner reported zero, the
other that it never answered.

### Event log

Every decision lands in the `events` table, which is what makes the latches
durable: `sleep`, `wake`, `would_sleep`, `would_wake`, `awake`,
`sleep_failed`, `wake_failed`, `skipped_sleep_cooldown`,
`sleep_needs_attention`, `sleep_attention_cleared`.

## Windows notes

The Windows host differs from the Linux dev box in ways that used to break this
project silently. Each is handled in `minerwatch/compat.py`:

- **No time zone database.** Windows ships no IANA tz data, so every
  `ZoneInfo()` lookup fails. The `tzdata` wheel is a Windows-only dependency and
  `setup.ps1` verifies it took effect. A missing database is reported
  separately from a mistyped zone name.
- **Console code page.** A fresh `cmd.exe` uses a legacy code page; a non-ASCII
  log record would raise `UnicodeEncodeError` and stop the poll loop. The CLI
  forces UTF-8 on stdout/stderr before the first log record, and the launch
  scripts set `PYTHONUTF8=1`.
- **File encodings.** `open(path)` uses the locale encoding on Windows, not
  UTF-8. Config and control files are always read and written as UTF-8 with LF
  newlines, so the same file behaves identically on both platforms.
- **Working directory.** Task Scheduler starts programs in
  `C:\Windows\System32`, so a relative `db_path` would put the database
  somewhere unexpected. Relative paths are resolved against `miners.yaml`'s own
  directory.
- **`SO_REUSEADDR`.** On Windows this permits *hijacking* a port another process
  is actively listening on, rather than reusing a `TIME_WAIT` socket. The
  simulators disable it there so two instances cannot silently share a port.
- **Ctrl+C.** The Proactor event loop does not wake on `SIGINT` while idle, so
  the poll loop sleeps in short slices and shuts down within about half a
  second instead of a full poll interval. `SIGBREAK` is handled too.
- **Atomic writes.** The simulator's control file is written via `os.replace`,
  which is atomic on Windows (unlike `os.rename`, which fails when the
  destination exists), so a reader never sees a half-written file. On Windows
  atomic means *atomic or refused* — CPython opens files without
  `FILE_SHARE_DELETE`, so the rename fails with `PermissionError` while a
  scanner or editor holds the destination. Those holders are transient, so the
  rename is retried briefly.

### Known limitation

The `bitmain_http` backend runs its blocking HTTP calls on a worker thread, and
a thread cannot be cancelled. If a miner's web UI stops responding mid-request,
MinerWatch gives up after roughly `2 x timeout_seconds` and records a failure,
but the request may still complete afterwards and change `miner-mode` behind
its back. The same bound is how long Ctrl+C can be delayed while the executor
drains, so keep `timeout_seconds` modest (the default is 15).

### Running as a scheduled task

One script registers everything. Run it from an **elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Mode live-sleep
```

`-Mode` is `rehearse` (default), `as-configured`, `live-sleep`, or `live`.
`rehearse` forces `--dry-run`, so it rehearses even where `miners.yaml` sets
`dry_run: false` — a mode named rehearse must never actuate hardware. Use
`as-configured` if you want each miner's own `dry_run` to decide. Before
registering, the installer names every miner the chosen mode would actuate and
asks for confirmation. The task starts at
boot after a short delay, runs as SYSTEM so it needs no stored password and
survives logout, restarts itself every 5 minutes if it dies, and logs to
`logs\minerwatch.log`. `-Uninstall` removes it.

The installer parses `miners.yaml` before registering anything — a task whose
config has a typo would otherwise die at boot with exit code 2 and no console
to say why.

```powershell
Start-ScheduledTask -TaskName MinerWatch          # without waiting for a reboot
Get-ScheduledTask -TaskName MinerWatch | Get-ScheduledTaskInfo
Get-Content .\logs\minerwatch.log -Tail 20 -Wait
```

### Changing the config

`miners.yaml` is read **once, at startup** — there is no hot reload. Edit it,
validate with `status` (which reparses and touches no miner), then restart:

```powershell
.\.venv\Scripts\python.exe -m minerwatch -c miners.yaml status
Stop-ScheduledTask  -TaskName MinerWatch
Start-ScheduledTask -TaskName MinerWatch
```

Nothing is lost across a restart: every latch — asleep, settle timers,
cooldowns, failure counts — is rebuilt from the event log. The exception is
renaming a miner's `id`, which orphans its history; wake a miner before
renaming it.

## Simulators

### `sim/miner_sim.py` — fake Antminer TCP API

Implements the cgminer JSON subset: `summary`, `stats`, `restart`, plus the
`ascset` / `pause` / `resume` commands the sleep backends use.

```bash
.venv/bin/python -m sim.miner_sim --port 4101
.venv/bin/python -m sim.miner_sim --port 4101 --stopped     # start with hashrate 0
.venv/bin/python -m sim.miner_sim --port 4101 --sleeping    # start slept
```

**Control file.** The simulator watches `sim/state-<port>.json` and applies it
on the next request. Write a JSON file to force state:

```json
{"state": "sleeping"}
```

Valid states: `mining`, `stopped`, `restarting`, `sleeping`. Changes are
detected by *content*, not modification time, because coarse filesystem
timestamps otherwise let an edit go unnoticed.

**Null terminator.** Responses are `\x00`-terminated (authentic cgminer
protocol). Consumers **must** read until the terminator rather than taking a
single `recv()` as a whole message — replies do get split across TCP segments.
`minerwatch.api` does this for you. Use `--no-null-terminator` to disable.

### `sim/bitmain_http_sim.py` — fake Bitmain web UI

Serves `get_miner_conf.cgi` and `set_miner_conf.cgi` behind HTTP Digest auth,
so the `bitmain_http` backend can be exercised end to end.

```bash
.venv/bin/python -m sim.bitmain_http_sim --port 8080 --username root --password root
```

`--linked-port 4103` ties it to a TCP simulator, so changing `miner-mode`
actually stops that miner's hashrate — as it would on a real S19, where the web
UI and the cgminer API are two faces of one machine. Without the link the two
simulators are independent processes, and an end-to-end `bitmain_http` demo
looks like a failure: MinerWatch sets the mode, keeps seeing full hashrate, and
correctly reports that the sleep never took effect. `scripts\sim.ps1 -WithHttp`
sets the link up for you.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest             # POSIX
.\.venv\Scripts\python.exe -m pytest   # Windows
```

435 tests, no hardware required — the simulators cover both control paths.
CI runs the suite on Python 3.10 through 3.13, on Linux and Windows.

Conventions for contributors are in [AGENTS.md](AGENTS.md). The short version:
anything that differs between Linux and Windows belongs in
`minerwatch/compat.py`, and any new actuator must be rehearsed by default —
record the intent in the event log, run the full state machine in dry-run, and
send bytes only when explicitly enabled.

## Safety

MinerWatch can stop a fleet of miners. It is built so that cannot happen by
accident:

- Both `sleep.enabled: true` and `sleep.dry_run: false` are required before a
  single byte reaches hardware, and the CLI needs `--live-sleep` or `--live` on
  top of that.
- A rehearsal runs the entire state machine and records `would_sleep` /
  `would_restart` without sending anything, so a schedule can be validated
  against real miners at no risk.
- Rehearsal records are never read back as fact by a later live run.
- The scheduled-task installer names every miner the chosen mode would actuate
  and asks for confirmation before registering anything.

Read [docs/operating.md](docs/operating.md) before pointing this at hardware —
particularly the `normal_value` hazard, which is how a wake command can push a
deliberately underclocked miner back to full power.

## License

MIT — see [LICENSE](LICENSE).
