# MinerWatch — Agent Notes

## Testing

Run tests with the project's virtual environment:

```bash
.venv/bin/python -m pytest              # Linux / macOS
.\.venv\Scripts\python.exe -m pytest    # Windows
```

## Python Environment

The virtual environment (`.venv/`) must be used for all Python commands —
including `pip`, `python`, and `pytest`. Never use system-level Python
or pip for this project.

The interpreter path is platform-dependent: `.venv/bin/python` on POSIX,
`.venv\Scripts\python.exe` on Windows. `scripts/setup.ps1` creates the
environment on a Windows host and verifies that the time zone database is
present.

## Cross-platform rules

This code runs on a Linux dev box and a Windows host. Anything that differs
between them belongs in `minerwatch/compat.py`, not inline. In particular:

- **Never call `open()` without `encoding=`.** The locale default is UTF-8 on
  Linux and cp1252 on Windows. Use `compat.read_text` /
  `compat.write_text_atomic`.
- **Never log non-ASCII characters.** A legacy console code page turns an em
  dash into a `UnicodeEncodeError` that stops the poll loop. Use `-` in log
  messages and comments that end up in log output.
- **Never assume the working directory.** Task Scheduler starts in
  `C:\Windows\System32`. Resolve config-relative paths with
  `compat.resolve_path`.
- **Never enable `SO_REUSEADDR` unconditionally.** On Windows it allows
  hijacking a live port. Use `compat.ALLOW_REUSE_ADDRESS`.
- **Never `await asyncio.sleep(long_interval)` in a supervision loop.** Use
  `compat.interruptible_sleep` so Ctrl+C is observed promptly under the
  Proactor event loop.
- **Never treat one `reader.read()` as a whole cgminer message.** Use
  `minerwatch.api`, which reads until the NUL terminator.

## Safety rules

Power control and restarts are dry-run by default and stay that way unless the
config sets `dry_run: false` or the CLI is given `--live*`. Any new actuator
must follow the same pattern: record the intent in the event log, run the full
state machine in rehearsal, and only send bytes when explicitly enabled.
