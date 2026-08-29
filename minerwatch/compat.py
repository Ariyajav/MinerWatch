"""Cross-platform helpers.

MinerWatch is developed on POSIX and deployed on a Windows host, so every
place where the two platforms disagree is funnelled through this module
instead of being sprinkled through the code base.

Windows differences that matter here:

* **No IANA time zone database.** ``zoneinfo`` on Linux reads
  ``/usr/share/zoneinfo``; Windows has no such directory, so ``ZoneInfo("UTC")``
  raises :class:`~zoneinfo.ZoneInfoNotFoundError` unless the ``tzdata`` wheel is
  installed. :func:`require_tzdata` turns that into an actionable message.
* **Console code page.** A fresh ``cmd.exe`` / PowerShell session uses a legacy
  code page (cp1252 on most Western installs). Writing a non-ASCII character to
  a log stream raises :class:`UnicodeEncodeError` and kills the poll loop.
  :func:`configure_console` forces UTF-8 with replacement.
* **Default text encoding.** ``open(path)`` uses the locale encoding on Windows,
  not UTF-8, so a config file with non-ASCII text decodes to mojibake or blows
  up. Always pass ``encoding=`` explicitly; :func:`read_text` does.
* **``SO_REUSEADDR`` semantics.** On Windows it permits *hijacking* a port that
  another process already holds, instead of merely reusing a socket in
  ``TIME_WAIT``. The simulator must not set it there.
* **Ctrl+C under the Proactor event loop.** The loop does not wake on
  ``SIGINT`` while it is idle, so a KeyboardInterrupt is not observed until the
  next I/O completion. :func:`install_signal_handlers` plus a short polling
  interval keeps shutdown responsive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

#: ``socketserver`` sets ``SO_REUSEADDR`` when this is true. On Windows that
#: flag lets a second process steal a live port, which silently produces two
#: simulators answering on the same address, so it must stay off there.
ALLOW_REUSE_ADDRESS = not IS_WINDOWS

#: How long the poll loop may block before it re-checks the stop flag. Windows
#: needs a bounded wait for Ctrl+C to be noticed promptly.
SHUTDOWN_POLL_SECONDS = 0.5


#: Lowest interpreter this code actually runs on. PEP 604 unions (``X | None``)
#: appear in runtime annotations, which 3.9 cannot evaluate. Keep in sync with
#: ``requires-python`` in pyproject.toml.
MIN_PYTHON = (3, 10)


class TimezoneDataMissing(RuntimeError):
    """Raised when ``zoneinfo`` cannot find the IANA database."""


def python_too_old() -> bool:
    return sys.version_info[:2] < MIN_PYTHON


def python_version_message() -> str:
    """Explain an unsupported interpreter in terms of what to do about it."""
    running = ".".join(str(p) for p in sys.version_info[:3])
    required = ".".join(str(p) for p in MIN_PYTHON)
    return (
        f"MinerWatch needs Python {required} or newer, but this interpreter is "
        f"{running} ({sys.executable}).\n"
        "Create the virtual environment with a newer interpreter, e.g.:\n"
        + (
            "    py -3.12 -m venv .venv\n"
            if IS_WINDOWS
            else "    python3.12 -m venv .venv\n"
        )
    )


def running_in_venv() -> bool:
    """True when the current interpreter is a virtual environment's."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def venv_python_hint(root: str | os.PathLike[str] | None = None) -> str | None:
    """Path to this project's venv interpreter, if one exists next to the code.

    Returns ``None`` when there is no ``.venv`` to point at, so callers can fall
    back to generic advice instead of naming a file that is not there.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    candidate = base / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    return str(candidate) if candidate.exists() else None


def missing_dependency_message(package: str, module: str) -> str:
    """Explain a missing dependency in terms of the likely cause.

    The usual cause on the Windows host is not a missing install but the wrong
    interpreter: ``python -m minerwatch`` resolves to the system Python, which
    cannot see anything installed into ``.venv``. Saying "run pip install" to
    someone who already did is a dead end, so check which interpreter is
    running before deciding what to suggest.
    """
    lines = [f"{package} is not available to this interpreter (no module named {module!r})."]
    hint = venv_python_hint()
    if python_too_old():
        # The dependency is missing *because* pip refused to install into an
        # interpreter this package does not support. Saying "run pip install"
        # here would send the reader in a circle: pip will refuse again.
        lines += ["", python_version_message().rstrip()]
    elif not running_in_venv() and hint:
        lines += [
            "",
            f"You are running {sys.executable}, which is not this project's virtual",
            "environment. Dependencies were installed into .venv, so use its interpreter:",
            "",
            f"    {hint} -m minerwatch -c miners.yaml status",
            "",
            "The launch scripts (scripts\\run.ps1, scripts\\run.bat) already do this."
            if IS_WINDOWS
            else "The launch scripts already do this.",
        ]
    elif hint:
        lines += [
            "",
            "Install the project's dependencies into this environment:",
            "",
            f"    {sys.executable} -m pip install -e \".[dev]\"",
        ]
    else:
        lines += [
            "",
            "Create the virtual environment and install dependencies first:",
            "",
            "    powershell -ExecutionPolicy Bypass -File scripts\\setup.ps1"
            if IS_WINDOWS
            else "    python3 -m venv .venv && .venv/bin/pip install -e \".[dev]\"",
        ]
    return "\n".join(lines)


#: Zones that exist in every build of the IANA database. If none of them
#: resolve, the database itself is missing rather than the requested key.
_TZ_SENTINELS = ("UTC", "Etc/UTC", "America/New_York")


def tzdata_available() -> bool:
    """True when ``zoneinfo`` can find the IANA database at all."""
    for sentinel in _TZ_SENTINELS:
        try:
            ZoneInfo(sentinel)
            return True
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            continue
    return False


def require_tzdata(name: str = "UTC") -> ZoneInfo:
    """Return ``ZoneInfo(name)``, distinguishing a bad key from a missing db.

    On a stock Windows install the tz database is simply not present, so
    *every* zone lookup fails identically. Reporting that as "unknown timezone
    'UTC'" sends the operator hunting for a typo that does not exist, so the
    two cases are separated: a genuinely unknown key still raises
    :class:`~zoneinfo.ZoneInfoNotFoundError` for the caller to translate, while
    a missing database raises :class:`TimezoneDataMissing` with install
    instructions.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if tzdata_available():
            raise
        if IS_WINDOWS:
            raise TimezoneDataMissing(
                "No IANA time zone database found. Windows does not ship one; "
                "install it into this environment with:\n"
                "    .venv\\Scripts\\python.exe -m pip install tzdata\n"
                f"(original error: {exc})"
            ) from exc
        raise TimezoneDataMissing(
            f"No IANA time zone database found for {name!r}: {exc}"
        ) from exc


def configure_console(stream=None) -> None:
    """Force UTF-8 on the given text stream when the platform allows it.

    ``TextIOWrapper.reconfigure`` exists on 3.7+, but a stream may be a pipe or
    a captured buffer that does not support it, so failures are ignored.
    """
    for target in (stream,) if stream is not None else (sys.stdout, sys.stderr):
        reconfigure = getattr(target, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - stream-dependent
            pass


def read_text(path: str | os.PathLike[str], encoding: str = "utf-8") -> str:
    """Read a text file with an explicit encoding.

    Never rely on the locale default: it is UTF-8 on the Linux dev box and
    cp1252 on the Windows host, which makes the same file parse differently on
    the two platforms.
    """
    return Path(path).read_text(encoding=encoding)


#: How many times to retry the final rename, and how long to wait between
#: attempts. See :func:`write_text_atomic`.
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF_SECONDS = 0.05


def write_text_atomic(
    path: str | os.PathLike[str],
    text: str,
    encoding: str = "utf-8",
    attempts: int = REPLACE_ATTEMPTS,
) -> None:
    """Write *text* to *path* atomically.

    A reader that opens the file midway through a plain ``open(path, "w")``
    sees a truncated document. Writing to a sibling temp file and calling
    :func:`os.replace` avoids that; ``os.replace`` is atomic on both POSIX and
    Windows (unlike ``os.rename``, which fails on Windows if the destination
    exists).

    On Windows "atomic" means *atomic or refused*, not atomic or blocked.
    CPython opens files without ``FILE_SHARE_DELETE``, so while any other
    process has the destination open — a virus scanner, an Explorer preview, a
    text editor — ``os.replace`` fails with ``PermissionError``
    (``ERROR_SHARING_VIOLATION``). Those holders are transient, so the rename is
    retried briefly before giving up rather than failing on the first collision.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(1, max(1, attempts) + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt >= attempts:
                    raise
                logger.debug(
                    "Rename onto %s refused (attempt %d/%d); another process has it open",
                    path,
                    attempt,
                    attempts,
                )
                time.sleep(REPLACE_BACKOFF_SECONDS * attempt)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_path(path: str, base: str | os.PathLike[str] | None) -> str:
    """Resolve a possibly-relative config path against *base*.

    A Windows scheduled task starts in ``C:\\Windows\\System32``, so a relative
    ``db_path`` from ``miners.yaml`` would put the database somewhere the
    service account may not even be able to write. Anchoring relative paths to
    the config file's own directory makes the deployment location-independent.

    Sentinel SQLite paths (``:memory:`` and anything else starting with ``:``)
    and absolute paths are returned unchanged.
    """
    if not path or path.startswith(":") or base is None:
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((Path(base).parent / p).resolve())


def install_signal_handlers(handler) -> None:
    """Register *handler* for the termination signals this platform supports.

    ``SIGTERM`` exists on Windows but is never delivered by the OS; registering
    it is harmless and keeps the POSIX path identical. ``SIGBREAK`` is the
    signal Windows actually sends for Ctrl+Break and for a console-close, and
    it is the one a ``taskkill`` without ``/F`` on a console app produces.
    """
    names = ["SIGINT", "SIGTERM"]
    if IS_WINDOWS:
        names.append("SIGBREAK")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover
            # Not the main thread, or unsupported on this build.
            logger.debug("Could not install handler for %s", name)


async def interruptible_sleep(seconds: float, stop_event, tick: float = SHUTDOWN_POLL_SECONDS) -> bool:
    """Sleep up to *seconds*, waking early once *stop_event* is set.

    Implemented as a chain of short sleeps rather than one long one for two
    reasons: a ``threading.Event`` cannot be awaited, and on Windows the
    Proactor loop only notices Ctrl+C when it returns from a wait. With a
    15-second poll interval a single ``asyncio.sleep(15)`` would make the
    process appear hung for up to 15 seconds after Ctrl+C.

    Returns ``True`` if it slept the full duration, ``False`` if interrupted.
    """
    remaining = seconds
    while remaining > 0:
        if stop_event.is_set():
            return False
        await asyncio.sleep(min(tick, remaining))
        remaining -= tick
    return not stop_event.is_set()
