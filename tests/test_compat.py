"""Tests for the cross-platform helpers.

These encode the Windows-specific behaviours the rest of the code depends on.
Where a real Windows filesystem or console cannot be simulated on the CI box,
the test pins the *contract* (explicit encoding, atomic replace, path
anchoring) rather than the platform.
"""

import asyncio
import io
import os
import sys
import threading
from pathlib import Path

import pytest

from minerwatch import compat


class TestReadText:
    def test_reads_utf8_regardless_of_locale(self, tmp_path):
        # The whole point: never inherit the locale encoding, which is cp1252
        # on the Windows host and would mangle these bytes.
        p = tmp_path / "conf.yaml"
        p.write_bytes("id: mötör-01 — ünïcode\n".encode("utf-8"))
        assert compat.read_text(p) == "id: mötör-01 — ünïcode\n"

    def test_missing_file_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            compat.read_text(tmp_path / "nope.yaml")


class TestWriteTextAtomic:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "state.json"
        compat.write_text_atomic(p, '{"state": "sleeping"}')
        assert p.read_text(encoding="utf-8") == '{"state": "sleeping"}'

    def test_creates_parent_directories(self, tmp_path):
        p = tmp_path / "deep" / "nested" / "state.json"
        compat.write_text_atomic(p, "{}")
        assert p.exists()

    def test_overwrites_existing_file(self, tmp_path):
        # os.replace, not os.rename: rename onto an existing path fails on
        # Windows.
        p = tmp_path / "state.json"
        p.write_text("old", encoding="utf-8")
        compat.write_text_atomic(p, "new")
        assert p.read_text(encoding="utf-8") == "new"

    def test_leaves_no_temp_files_behind(self, tmp_path):
        p = tmp_path / "state.json"
        compat.write_text_atomic(p, "{}")
        assert [f.name for f in tmp_path.iterdir()] == ["state.json"]

    def test_reader_never_sees_a_partial_file(self, tmp_path):
        """The file is either fully old or fully new, never truncated."""
        p = tmp_path / "state.json"
        compat.write_text_atomic(p, "A" * 4096)
        seen = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    seen.append(len(p.read_text(encoding="utf-8")))
                except (FileNotFoundError, PermissionError):
                    # On Windows a concurrent reader can lose the race for the
                    # handle; that is a miss, not a torn read.
                    pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for _ in range(50):
                compat.write_text_atomic(p, "B" * 8192)
                compat.write_text_atomic(p, "A" * 4096)
        finally:
            stop.set()
            t.join(timeout=5)
        assert seen, "the reader thread never managed a read; the test proved nothing"
        assert set(seen) <= {4096, 8192}, f"observed a partially written file: {set(seen)}"

    def test_replace_is_retried_when_the_destination_is_locked(self, tmp_path, monkeypatch):
        """Windows refuses os.replace while another process holds the file open.

        CPython opens without FILE_SHARE_DELETE, so a virus scanner or editor
        holding state-<port>.json turns every control-file write into a
        PermissionError. Those holders are transient, so the rename retries.
        """
        p = tmp_path / "state.json"
        compat.write_text_atomic(p, "old")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst)

        monkeypatch.setattr(compat.os, "replace", flaky_replace)
        compat.write_text_atomic(p, "new")
        assert p.read_text(encoding="utf-8") == "new"
        assert calls["n"] == 3

    def test_a_permanently_locked_destination_still_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "state.json"

        def always_locked(src, dst):
            raise PermissionError(32, "locked forever")

        monkeypatch.setattr(compat.os, "replace", always_locked)
        with pytest.raises(PermissionError):
            compat.write_text_atomic(p, "new", attempts=2)
        # The temp file must not be left behind.
        assert list(tmp_path.iterdir()) == []

    def test_uses_lf_newlines(self, tmp_path):
        # newline="\n" keeps the JSON control file byte-identical across
        # platforms; the default on Windows would translate to CRLF.
        p = tmp_path / "state.json"
        compat.write_text_atomic(p, "a\nb\n")
        assert p.read_bytes() == b"a\nb\n"


class TestResolvePath:
    def test_relative_is_anchored_to_the_config_directory(self, tmp_path):
        cfg = tmp_path / "sub" / "miners.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{}", encoding="utf-8")
        out = compat.resolve_path("minerwatch.db", str(cfg))
        assert Path(out).parent == cfg.parent.resolve()

    def test_absolute_is_left_alone(self, tmp_path):
        absolute = str((tmp_path / "abs.db").resolve())
        assert compat.resolve_path(absolute, str(tmp_path / "miners.yaml")) == absolute

    def test_memory_sentinel_is_left_alone(self, tmp_path):
        assert compat.resolve_path(":memory:", str(tmp_path / "miners.yaml")) == ":memory:"

    def test_no_base_leaves_path_alone(self):
        assert compat.resolve_path("minerwatch.db", None) == "minerwatch.db"


class TestConfigureConsole:
    def test_forces_utf8_on_a_reconfigurable_stream(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        compat.configure_console(stream)
        assert stream.encoding.lower().replace("-", "") == "utf8"

    def test_non_reconfigurable_stream_is_ignored(self):
        class Plain:
            pass

        compat.configure_console(Plain())  # must not raise

    def test_writing_non_ascii_after_reconfigure(self):
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        compat.configure_console(stream)
        stream.write("— ünïcode")  # would raise UnicodeEncodeError under cp1252
        stream.flush()
        assert "ünïcode" in buf.getvalue().decode("utf-8")


class TestTzdata:
    def test_known_zone_resolves(self):
        assert compat.require_tzdata("UTC") is not None

    def test_tzdata_is_available_in_this_environment(self):
        assert compat.tzdata_available() is True

    def test_unknown_zone_still_raises_zoneinfo_error(self):
        from zoneinfo import ZoneInfoNotFoundError

        # A typo must not be reported as "install tzdata".
        with pytest.raises(ZoneInfoNotFoundError):
            compat.require_tzdata("Mars/Phobos")

    def test_missing_database_reports_install_instructions(self, monkeypatch):
        monkeypatch.setattr(compat, "tzdata_available", lambda: False)
        with pytest.raises(compat.TimezoneDataMissing, match="time zone database"):
            compat.require_tzdata("Mars/Phobos")


class TestInterruptibleSleep:
    async def test_returns_early_when_stopped(self):
        stop = threading.Event()

        async def stopper():
            await asyncio.sleep(0.05)
            stop.set()

        task = asyncio.create_task(stopper())
        loop = asyncio.get_running_loop()
        started = loop.time()
        slept_fully = await compat.interruptible_sleep(30, stop, tick=0.02)
        elapsed = loop.time() - started
        await task
        assert slept_fully is False
        # A plain asyncio.sleep(30) would leave the process looking hung.
        assert elapsed < 2

    async def test_sleeps_the_full_duration_when_not_stopped(self):
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await compat.interruptible_sleep(0.1, stop, tick=0.02) is True
        assert loop.time() - started >= 0.09

    async def test_already_set_returns_immediately(self):
        stop = threading.Event()
        stop.set()
        assert await compat.interruptible_sleep(30, stop, tick=0.02) is False


@pytest.fixture
def restore_signals():
    """Put every signal disposition back; these tests install real handlers."""
    import signal

    names = [n for n in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    yield
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)


class TestSignalHandlers:
    def test_installs_without_error_on_this_platform(self, restore_signals):
        import signal

        sentinel = lambda signum, frame: None  # noqa: E731
        compat.install_signal_handlers(sentinel)
        assert signal.getsignal(signal.SIGINT) is sentinel
        assert signal.getsignal(signal.SIGTERM) is sentinel

    def test_missing_signal_names_are_skipped(self, monkeypatch, restore_signals):
        # SIGBREAK does not exist off Windows; the helper must not blow up when
        # it takes the Windows branch on a POSIX box.
        monkeypatch.setattr(compat, "IS_WINDOWS", True)
        compat.install_signal_handlers(lambda signum, frame: None)


class TestPlatformFlags:
    def test_two_servers_cannot_share_a_port(self):
        """The behaviour ALLOW_REUSE_ADDRESS exists to protect.

        On Windows, SO_REUSEADDR lets a second process bind a port another one
        is actively listening on, so both receive a share of the connections.
        Whatever the platform, binding the same address twice must fail.
        """
        import socketserver

        from sim.miner_sim import SimServer, SimState

        first = SimServer(("127.0.0.1", 0), SimState(now=lambda: 0.0))
        try:
            addr = first.server_address
            with pytest.raises(OSError):
                second = SimServer(addr, SimState(now=lambda: 0.0))
                second.server_close()
        finally:
            first.server_close()

    def test_windows_flag_tracks_the_platform(self):
        assert compat.IS_WINDOWS is (os.name == "nt")


class TestMissingDependencyMessage:
    """The first-run mistake is the wrong interpreter, not a missing install.

    A bare `python -m minerwatch` on Windows resolves to the system Python,
    which cannot see anything installed into .venv. Telling that person to run
    pip install sends them in a circle.
    """

    def test_outside_a_venv_it_names_the_venv_interpreter(self, monkeypatch, tmp_path):
        hint = str(tmp_path / ".venv" / "bin" / "python")
        monkeypatch.setattr(compat, "running_in_venv", lambda: False)
        monkeypatch.setattr(compat, "venv_python_hint", lambda root=None: hint)
        monkeypatch.setattr(compat.sys, "executable", "/usr/bin/python3.12")

        msg = compat.missing_dependency_message("PyYAML", "yaml")
        assert "PyYAML is not available" in msg
        assert "/usr/bin/python3.12" in msg
        assert hint in msg
        assert "pip install" not in msg, "do not send them to pip; the venv already has it"

    def test_inside_a_venv_it_suggests_installing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(compat, "running_in_venv", lambda: True)
        monkeypatch.setattr(compat, "venv_python_hint", lambda root=None: str(tmp_path / "python"))

        msg = compat.missing_dependency_message("PyYAML", "yaml")
        assert "pip install -e" in msg

    def test_with_no_venv_at_all_it_points_at_setup(self, monkeypatch):
        monkeypatch.setattr(compat, "running_in_venv", lambda: False)
        monkeypatch.setattr(compat, "venv_python_hint", lambda root=None: None)

        msg = compat.missing_dependency_message("PyYAML", "yaml")
        assert "setup.ps1" in msg or "venv" in msg

    def test_venv_hint_is_none_when_there_is_no_venv(self, tmp_path):
        assert compat.venv_python_hint(tmp_path) is None

    def test_venv_hint_finds_a_platform_appropriate_interpreter(self, tmp_path):
        rel = "Scripts/python.exe" if compat.IS_WINDOWS else "bin/python"
        target = tmp_path / ".venv" / rel
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        assert compat.venv_python_hint(tmp_path) == str(target)

    def test_the_test_suite_itself_runs_in_a_venv(self):
        # Sanity check on the detector, per AGENTS.md's "use .venv" rule.
        # CI runners install into the interpreter directly, so this asserts the
        # detector agrees with the interpreter rather than asserting the
        # environment: a real check where there is a venv, skipped where there
        # is not, and never a false failure.
        if sys.prefix == sys.base_prefix:
            pytest.skip("not running in a virtual environment")
        assert compat.running_in_venv() is True


class TestPythonVersionGuard:
    """An unsupported interpreter must say so, not fail later on an import.

    The real report: a 3.11 venv against `requires-python = ">=3.12"` made
    `pip install -e .` refuse, which left PyYAML uninstalled, which surfaced as
    "No module named 'yaml'" — three steps removed from the actual cause.
    """

    def test_this_interpreter_is_supported(self):
        assert compat.python_too_old() is False

    def test_minimum_matches_the_packaging_metadata(self):
        # Drift here is what caused the original failure, so pin them together.
        root = Path(compat.__file__).resolve().parent.parent
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        expected = ">=" + ".".join(str(p) for p in compat.MIN_PYTHON)
        assert f'requires-python = "{expected}"' in pyproject

    def test_too_old_is_detected(self, monkeypatch):
        monkeypatch.setattr(compat.sys, "version_info", (3, 9, 18, "final", 0))
        assert compat.python_too_old() is True

    def test_message_names_both_versions_and_the_interpreter(self, monkeypatch):
        monkeypatch.setattr(compat.sys, "version_info", (3, 9, 18, "final", 0))
        monkeypatch.setattr(compat.sys, "executable", "/usr/bin/python3.9")
        msg = compat.python_version_message()
        assert "3.9.18" in msg
        assert "3.10" in msg
        assert "/usr/bin/python3.9" in msg
        assert "venv" in msg

    def test_a_missing_dependency_on_an_old_interpreter_blames_the_version(self, monkeypatch):
        """Otherwise the advice is circular: pip will refuse for the same reason."""
        monkeypatch.setattr(compat.sys, "version_info", (3, 9, 18, "final", 0))
        msg = compat.missing_dependency_message("PyYAML", "yaml")
        assert "3.10 or newer" in msg
        assert "pip install" not in msg

    def test_the_floor_is_the_real_one(self):
        # PEP 604 unions in runtime annotations are what rules out 3.9.
        assert compat.MIN_PYTHON == (3, 10)


class TestLogFile:
    """Unattended runs need a log file; a scheduled task has no console.

    Without this the only diagnostic left after an overnight failure is the
    events database, which records decisions but not the errors around them.
    """

    def _reset_logging(self):
        import logging

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()

    def test_writes_to_the_named_file(self, tmp_path):
        import logging

        from minerwatch.cli import _setup_logging

        self._reset_logging()
        target = tmp_path / "logs" / "minerwatch.log"
        try:
            _setup_logging(False, str(target))
            logging.getLogger("minerwatch").info("hello from the poller")
            logging.shutdown()
            assert "hello from the poller" in target.read_text(encoding="utf-8")
        finally:
            self._reset_logging()

    def test_creates_missing_parent_directories(self, tmp_path):
        import logging

        from minerwatch.cli import _setup_logging

        self._reset_logging()
        target = tmp_path / "a" / "b" / "c.log"
        try:
            _setup_logging(False, str(target))
            logging.getLogger("minerwatch").info("x")
            logging.shutdown()
            assert target.exists()
        finally:
            self._reset_logging()

    def test_log_file_is_utf8_not_the_locale_codepage(self, tmp_path):
        """cp1252 would raise on this line and lose the record."""
        import logging

        from minerwatch.cli import _setup_logging

        self._reset_logging()
        target = tmp_path / "mw.log"
        try:
            _setup_logging(False, str(target))
            logging.getLogger("minerwatch").info("rack-ü-01 — asleep")
            logging.shutdown()
            assert "rack-ü-01" in target.read_text(encoding="utf-8")
        finally:
            self._reset_logging()

    def test_an_unwritable_path_warns_and_keeps_running(self, tmp_path, caplog):
        """Losing the log must not stop the fleet being supervised.

        Note this test does *not* reset logging first: caplog works by adding
        its own root handler, and clearing the root would throw that away along
        with everything else, so the error would be raised into a void.
        """
        import logging
        from logging.handlers import RotatingFileHandler

        from minerwatch.cli import _setup_logging

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        before = list(logging.getLogger().handlers)
        try:
            with caplog.at_level(logging.ERROR):
                _setup_logging(False, str(blocker / "mw.log"))
            assert "Could not open log file" in caplog.text
            assert not any(
                isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers
            ), "a broken file handler was attached anyway"
        finally:
            root = logging.getLogger()
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)

    def test_rotation_caps_disk_use(self, tmp_path, monkeypatch):
        import logging

        from minerwatch import cli
        from minerwatch.cli import _setup_logging

        self._reset_logging()
        monkeypatch.setattr(cli, "LOG_MAX_BYTES", 2048)
        monkeypatch.setattr(cli, "LOG_BACKUPS", 2)
        target = tmp_path / "mw.log"
        try:
            _setup_logging(False, str(target))
            log = logging.getLogger("minerwatch")
            for i in range(400):
                log.info("poll %d: every miner reporting normally", i)
            logging.shutdown()
            files = sorted(p.name for p in tmp_path.iterdir())
            # The live file plus at most LOG_BACKUPS rotated ones.
            assert files == ["mw.log", "mw.log.1", "mw.log.2"], files
            assert all(p.stat().st_size < 8192 for p in tmp_path.iterdir())
        finally:
            self._reset_logging()

    def test_no_log_file_leaves_logging_alone(self, tmp_path):
        from minerwatch.cli import _setup_logging

        self._reset_logging()
        try:
            _setup_logging(False, None)
            _setup_logging(False, "")
            assert list(tmp_path.iterdir()) == []
        finally:
            self._reset_logging()
