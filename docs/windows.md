# Windows host support

MinerWatch is developed on Linux and runs unattended on a Windows host.
Everything that differs between the two lives in `minerwatch/compat.py` rather
than inline, and `AGENTS.md` records the rules for keeping it that way.

## What breaks on Windows, and what fixes it

Each of these fails at runtime on a Windows host and passes on Linux.

| Problem | Consequence | Fix |
| --- | --- | --- |
| No IANA time zone database | Every `ZoneInfo()` lookup fails — no schedule works at all | `tzdata` as a Windows-only dependency; a missing database is reported separately from a mistyped zone name, and `setup.ps1` verifies it took effect |
| Legacy console code page | A non-ASCII log record raises `UnicodeEncodeError` and stops the poll loop | UTF-8 forced on stdout/stderr before the first record; `PYTHONUTF8=1` in the launchers; log messages kept ASCII |
| `open()` without `encoding=` | Config and control files decode as cp1252, not UTF-8 | Explicit UTF-8 everywhere, LF newlines, via `compat.read_text` / `compat.write_text_atomic` |
| Working directory | Task Scheduler starts programs in `C:\Windows\System32`, so a relative `db_path` lands somewhere unexpected | Relative paths anchored to `miners.yaml`'s own directory, via `compat.resolve_path` |
| `SO_REUSEADDR` | On Windows it permits *hijacking* a port another process is actively listening on | Disabled there, so two simulators cannot silently share a port |
| Ctrl+C under the Proactor loop | The loop does not wake on `SIGINT` while idle; shutdown took a full poll interval (measured: 600 s → 0.13 s) | The poll loop sleeps in short slices via `compat.interruptible_sleep`; `SIGBREAK` handled too |
| `os.replace` onto an open file | CPython opens without `FILE_SHARE_DELETE`, so a scanner or editor holding the destination makes the write fail outright | Brief retry before giving up — on Windows, atomic means *atomic or refused* |

Fixed alongside these: a cgminer reply split across TCP segments was misread as
unparseable, because a single `reader.read()` was treated as a whole message.
All socket traffic goes through `minerwatch/api.py`, which reads until the NUL
terminator.

## Environment notes

- **`tzdata` is mandatory on Windows.** No IANA database ships with the OS, so
  without it every schedule lookup fails. It is declared as a
  `sys_platform == 'win32'` dependency and installed automatically.
- **Python 3.10 is the floor**, verified green on 3.10, 3.11, 3.12 and 3.13. An
  earlier `requires-python = ">=3.12"` was inherited and needed by nothing; on a
  3.11 host it made `pip install -e .` refuse, which left PyYAML uninstalled,
  which surfaced three steps later as `No module named 'yaml'`.
- **`pip install -e .` does not reliably write `.venv\Scripts\minerwatch.exe`.**
  Do not depend on the console script; `-m minerwatch` always works.
- **The package does not have to be installed at all.**
  `pip install pyyaml tzdata` plus running from the repo directory is enough,
  since `-m` finds the package in the working directory.
- **PowerShell eats double quotes when handing a string to a native `.exe`.** An
  inline `python -c` probe inside `install-task.ps1` reached the interpreter as
  `print(ENABLED: + ,.join(enabled))` and crashed; with no exit-code check, the
  installer then reported "will NOT send anything to any miner" for a live
  config. Probes live in `scripts/probe_sleep.py` now, and unusable output is a
  hard refusal to register the task.

See [operating.md](operating.md#running-unattended-on-windows) for registering
and managing the scheduled task.

## Field notes: the S19 XP read/write asymmetry

This is the single fact that made live sleep work on Bitmain stock firmware, and
it is not documented anywhere by the vendor.

On S19 XP stock firmware the config API **reads and writes the power mode under
different names**:

- `get_miner_conf.cgi` returns `"bitmain-work-mode": "0"` — a *string*, and no
  `miner-mode` field exists at all.
- `set_miner_conf.cgi` only honours the value when posted as `"miner-mode": 1` —
  an *integer*, with `Content-Type: text/plain;charset=UTF-8` despite a JSON
  body.

Echoing the read field back is accepted and answered
`{"stats":"success","code":"M000","msg":"OK!"}` — and silently discarded. Six
JSON shapes, form encoding, and the whole cgminer path all failed this way,
each one being told it had succeeded.

What the code does about it:

- **`WRITE_ALIASES` maps `bitmain-work-mode` / `work-mode` → `miner-mode`.**
  `write_profile: auto` tries the conservative mirror shape first, then the
  alias shape — aliasing unconditionally breaks firmware that uses one name in
  both directions.
- **Nulls are coerced on the browser variant.** A real save sends
  `bitmain-user-ip-cat` as `"0"` where the read returns `null`.
- **`bitmain-hashrate-percent` is deliberately not sent.** A GET never returns
  it, and posting `100` would reset an underclocked miner's tuning.
- **Never trust the reply.** Every write is followed by a read-back, and success
  is claimed only when the value actually changed. This turns a "sleep OK" that
  did nothing into `sleep_failed - the setting did not persist`.
- **`diagnose <miner>`** walks every request shape and prints which one the
  firmware honours; **`check <miner>`** prints the discovered field and its
  current value. The cgminer `check` command does not exist on stock bmminer
  either, so that path falls back to probing commands directly and reading the
  refusal text — and never sends `restart` or `quit`.

Observed recovery cost: a work-mode change **reboots the mining process**. The
miner goes unreachable, then answers at 0 GH/s, and an XP takes roughly 5–10
minutes to return to full hashrate. `grace_seconds` must cover that, or the
watchdog is handed a mid-boot miner.

## Known limitation

The `bitmain_http` backend runs its blocking HTTP calls on a worker thread, and
a thread cannot be cancelled. If a miner's web UI stops responding mid-request,
MinerWatch gives up after roughly `2 × timeout_seconds` and records a failure,
but the request may still complete afterwards and change the work mode behind
its back. The same bound is how long Ctrl+C can be delayed while the executor
drains, so keep `timeout_seconds` modest — the default is 15.
