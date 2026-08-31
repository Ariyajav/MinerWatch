import os
import tempfile

import pytest
import yaml

from minerwatch.config import ConfigError, load_config
from minerwatch.models import RecoverWith

from .fixtures import MINERS_YAML


def test_load_config():
    poll_interval, db_path, default_tz, miners = load_config(MINERS_YAML)
    assert poll_interval == 15
    # db_path is anchored to the config file's directory so the database does
    # not follow the working directory around (Task Scheduler starts services
    # in C:\Windows\System32).
    assert os.path.isabs(db_path)
    assert os.path.basename(db_path) == "minerwatch.db"
    assert default_tz == "UTC"
    assert set(miners.keys()) == {"miner-01", "miner-02", "miner-03"}
    assert miners["miner-01"].host == "127.0.0.1"
    assert miners["miner-01"].port == 4101
    assert miners["miner-01"].group == "farm-a"
    assert miners["miner-03"].group == "solo"
    assert miners["miner-03"].schedule is None


def test_duplicate_id_raises():
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "UTC",
        "db_path": ":memory:",
        "groups": {},
        "miners": [
            {"id": "a", "host": "127.0.0.1", "port": 4101},
            {"id": "a", "host": "127.0.0.1", "port": 4102},
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name
    with pytest.raises(ConfigError, match="Duplicate miner id"):
        load_config(path)


def test_invalid_port_raises():
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "UTC",
        "db_path": ":memory:",
        "groups": {},
        "miners": [{"id": "a", "host": "127.0.0.1", "port": 99999}],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name
    with pytest.raises(ConfigError, match="out of range"):
        load_config(path)


def test_unknown_group_raises():
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "UTC",
        "db_path": ":memory:",
        "groups": {"farm-a": {"schedule": None}},
        "miners": [{"id": "a", "host": "127.0.0.1", "port": 4101, "group": "farm-b"}],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name
    with pytest.raises(ConfigError, match="Unknown group"):
        load_config(path)


def test_unknown_timezone_raises():
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "Mars/Phobos",
        "db_path": ":memory:",
        "groups": {},
        "miners": [],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name
    with pytest.raises(ConfigError, match="Unknown .* timezone"):
        load_config(path)


def test_miner_schedule_resolution():
    poll_interval, db_path, default_tz, miners = load_config(MINERS_YAML)
    m1 = miners["miner-01"]
    m2 = miners["miner-02"]
    m3 = miners["miner-03"]

    assert m1.schedule is not None
    assert len(m1.schedule.windows) == 1
    assert m1.schedule.windows[0].ranges[0].start == 9 * 60
    assert m1.schedule.windows[0].ranges[0].end == 17 * 60

    assert m2.schedule is not None
    assert len(m2.schedule.windows) == 1
    assert m2.schedule.windows[0].ranges[0].start == 22 * 60
    assert m2.schedule.windows[0].ranges[0].end == 23 * 60

    assert m3.schedule is None


def test_malformed_range_raises():
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "UTC",
        "db_path": ":memory:",
        "groups": {},
        "miners": [
            {
                "id": "a",
                "host": "127.0.0.1",
                "port": 4101,
                "schedule": {"windows": [{"days": ["mon"], "ranges": ["25:00-26:00"]}]},
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name
    with pytest.raises(ConfigError, match="Invalid range format|Range out of bounds"):
        load_config(path)


# ---------------------------------------------------------------------------
# Software power control (sleep) configuration
# ---------------------------------------------------------------------------

from minerwatch.models import Command, SleepBackend, SleepConfig  # noqa: E402


def write_config(raw) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump(raw, f)
        return f.name


def base_config(**extra):
    raw = {
        "poll_interval_seconds": 15,
        "default_timezone": "UTC",
        "db_path": ":memory:",
        "groups": {},
        "miners": [{"id": "a", "host": "10.0.0.5", "port": 4028}],
    }
    raw.update(extra)
    return raw


class TestSleepDefaults:
    def test_absent_block_leaves_control_disabled(self):
        _, _, _, miners = load_config(write_config(base_config()))
        assert miners["a"].sleep == SleepConfig()
        assert miners["a"].sleep.enabled is False

    def test_defaults_are_safe(self):
        cfg = SleepConfig()
        # Nothing reaches hardware until someone opts in twice: enable it, and
        # turn off the rehearsal.
        assert cfg.enabled is False
        assert cfg.dry_run is True


class TestSleepOverrideChain:
    def test_global_applies_to_every_miner(self):
        raw = base_config(sleep={"enabled": True, "backend": "cgminer"})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.enabled is True
        assert miners["a"].sleep.backend is SleepBackend.CGMINER

    def test_group_overrides_global(self):
        raw = base_config(
            sleep={"enabled": True, "username": "root", "password": "s3cret"},
            groups={"farm": {"sleep": {"backend": "bitmain_http"}}},
            miners=[{"id": "a", "host": "10.0.0.5", "group": "farm"}],
        )
        _, _, _, miners = load_config(write_config(raw))
        cfg = miners["a"].sleep
        assert cfg.backend is SleepBackend.BITMAIN_HTTP
        # Merged, not replaced: the global credentials survive.
        assert cfg.password == "s3cret"
        assert cfg.enabled is True

    def test_miner_overrides_group(self):
        raw = base_config(
            sleep={"enabled": True, "backend": "cgminer", "cooldown_seconds": 300},
            groups={"farm": {"sleep": {"cooldown_seconds": 600}}},
            miners=[{"id": "a", "group": "farm", "sleep": {"cooldown_seconds": 60}}],
        )
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.cooldown_seconds == 60

    def test_one_miner_can_opt_out(self):
        raw = base_config(
            sleep={"enabled": True},
            miners=[{"id": "a"}, {"id": "b", "sleep": {"enabled": False}}],
        )
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.enabled is True
        assert miners["b"].sleep.enabled is False


class TestSleepCommandParsing:
    def test_string_form(self):
        raw = base_config(
            sleep={"enabled": True, "sleep_commands": ["ascset:0,sleep", "pause"]}
        )
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.sleep_commands == (
            Command("ascset", "0,sleep"),
            Command("pause", None),
        )

    def test_mapping_form(self):
        raw = base_config(
            sleep={
                "enabled": True,
                "wake_commands": [{"command": "ascset", "parameter": "0,wake"}],
            }
        )
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.wake_commands == (Command("ascset", "0,wake"),)

    def test_only_the_first_colon_splits(self):
        # cgminer parameters contain colons and commas of their own.
        raw = base_config(sleep={"enabled": True, "sleep_commands": ["ascset:0,mode:low,2"]})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.sleep_commands == (Command("ascset", "0,mode:low,2"),)

    def test_non_list_rejected(self):
        raw = base_config(sleep={"enabled": True, "sleep_commands": "ascset:0,sleep"})
        with pytest.raises(ConfigError, match="must be a list"):
            load_config(write_config(raw))

    def test_empty_entry_rejected(self):
        raw = base_config(sleep={"enabled": True, "sleep_commands": [""]})
        with pytest.raises(ConfigError, match="Empty command"):
            load_config(write_config(raw))


class TestSleepValidation:
    def test_unknown_backend_lists_the_valid_ones(self):
        raw = base_config(sleep={"enabled": True, "backend": "carrier-pigeon"})
        with pytest.raises(ConfigError, match="Unknown sleep backend.*cgminer"):
            load_config(write_config(raw))

    def test_enabled_with_backend_none_is_contradictory(self):
        raw = base_config(sleep={"enabled": True, "backend": "none"})
        with pytest.raises(ConfigError, match="backend is 'none'"):
            load_config(write_config(raw))

    def test_non_boolean_enabled_rejected(self):
        raw = base_config(sleep={"enabled": "yes please"})
        with pytest.raises(ConfigError, match="must be true or false"):
            load_config(write_config(raw))

    def test_http_port_range_enforced(self):
        raw = base_config(sleep={"enabled": True, "http_port": 70000})
        with pytest.raises(ConfigError, match="out of range"):
            load_config(write_config(raw))

    def test_api_port_range_enforced(self):
        raw = base_config(sleep={"enabled": True, "api_port": 0})
        with pytest.raises(ConfigError, match="must be >= 1|out of range"):
            load_config(write_config(raw))

    def test_scheme_restricted_to_http_and_https(self):
        raw = base_config(sleep={"enabled": True, "http_scheme": "ftp"})
        with pytest.raises(ConfigError, match="http or https"):
            load_config(write_config(raw))

    def test_negative_cooldown_rejected(self):
        raw = base_config(sleep={"enabled": True, "cooldown_seconds": -1})
        with pytest.raises(ConfigError, match="must be >= 0"):
            load_config(write_config(raw))

    def test_error_names_the_offending_miner(self):
        raw = base_config(miners=[{"id": "rack-7", "sleep": {"backend": "nope"}}])
        with pytest.raises(ConfigError, match="rack-7"):
            load_config(write_config(raw))

    def test_timeout_must_be_positive(self):
        raw = base_config(sleep={"enabled": True, "timeout_seconds": 0})
        with pytest.raises(ConfigError, match="must be positive"):
            load_config(write_config(raw))


class TestConfigRobustness:
    def test_utf8_config_is_read_regardless_of_locale(self, tmp_path):
        # Written as UTF-8 bytes; must not be decoded with the platform locale.
        path = tmp_path / "miners.yaml"
        path.write_bytes(
            b"db_path: ':memory:'\nminers:\n  - id: 'rack-\xc3\xbc-01'\n    host: 10.0.0.9\n"
        )
        _, _, _, miners = load_config(str(path))
        assert "rack-ü-01" in miners

    def test_end_of_day_range_is_accepted(self):
        # 24:00 is the natural way to write "until midnight".
        raw = base_config(
            miners=[{"id": "a", "schedule": {"windows": [{"days": ["mon"], "ranges": ["18:00-24:00"]}]}}]
        )
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].schedule.windows[0].ranges[0].end == 24 * 60

    def test_empty_range_rejected(self):
        raw = base_config(
            miners=[{"id": "a", "schedule": {"windows": [{"days": ["mon"], "ranges": ["09:00-09:00"]}]}}]
        )
        with pytest.raises(ConfigError, match="Empty range"):
            load_config(write_config(raw))

    def test_miner_without_id_rejected(self):
        raw = base_config(miners=[{"host": "10.0.0.1"}])
        with pytest.raises(ConfigError, match="missing a string 'id'"):
            load_config(write_config(raw))

    def test_zero_poll_interval_rejected(self):
        raw = base_config(poll_interval_seconds=0)
        with pytest.raises(ConfigError, match="poll_interval_seconds"):
            load_config(write_config(raw))

    def test_empty_config_file_is_not_a_crash(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        poll, db, tz, miners = load_config(str(path))
        assert miners == {} and poll == 15


class TestModeKeyConfig:
    def test_mode_key_is_parsed(self):
        raw = base_config(sleep={"enabled": True, "backend": "bitmain_http",
                                 "mode_key": "bitmain-work-mode"})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.mode_key == "bitmain-work-mode"

    def test_mode_key_defaults_to_discovery(self):
        raw = base_config(sleep={"enabled": True, "backend": "bitmain_http"})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.mode_key is None

    def test_blank_mode_key_means_discovery(self):
        raw = base_config(sleep={"enabled": True, "backend": "bitmain_http", "mode_key": "  "})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.mode_key is None

    def test_mode_key_merges_down_from_a_group(self):
        raw = base_config(
            sleep={"enabled": True, "backend": "bitmain_http"},
            groups={"farm": {"sleep": {"mode_key": "work-mode"}}},
            miners=[{"id": "a", "host": "10.0.0.5", "group": "farm"}])
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.mode_key == "work-mode"


class TestModeValues:
    def test_values_are_parsed(self):
        raw = base_config(sleep={"enabled": True, "backend": "bitmain_http",
                                 "sleep_value": 3, "normal_value": 0})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.sleep_value == 3
        assert miners["a"].sleep.normal_value == 0

    def test_values_default_to_the_common_pairing(self):
        cfg = SleepConfig()
        assert (cfg.normal_value, cfg.sleep_value) == (0, 1)

    def test_a_negative_value_is_rejected(self):
        raw = base_config(sleep={"enabled": True, "sleep_value": -1})
        with pytest.raises(ConfigError, match="must be >= 0"):
            load_config(write_config(raw))


class TestPostFormat:
    def test_default_is_json(self):
        assert SleepConfig().post_format == "json"

    def test_form_is_accepted(self):
        raw = base_config(sleep={"enabled": True, "backend": "bitmain_http",
                                 "post_format": "form"})
        _, _, _, miners = load_config(write_config(raw))
        assert miners["a"].sleep.post_format == "form"

    def test_anything_else_is_rejected(self):
        raw = base_config(sleep={"enabled": True, "post_format": "xml"})
        with pytest.raises(ConfigError, match="must be json or form"):
            load_config(write_config(raw))


class TestRecoverWith:
    """The watchdog's recovery mechanism is configurable, and validated.

    A typo here is silent at runtime and only shows up as a miner that never
    recovers, so every wrong value is refused with the alternatives named.
    """

    @staticmethod
    def _cfg(**watchdog):
        base = {"enabled": True, "cooldown_seconds": 900, "rate_window_seconds": 3600,
                "max_restarts": 3}
        return base_config(watchdog={**base, **watchdog})

    def _load(self, raw):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
            path = f.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_the_default_is_the_cgminer_restart(self):
        _, _, _, miners = self._load(self._cfg())
        assert miners["a"].watchdog.recover_with is RecoverWith.CGMINER

    def test_each_mechanism_parses(self):
        for name, expected in (
            ("cgminer", RecoverWith.CGMINER),
            ("bitmain_reboot", RecoverWith.BITMAIN_REBOOT),
            ("auto", RecoverWith.AUTO),
        ):
            _, _, _, miners = self._load(self._cfg(recover_with=name))
            assert miners["a"].watchdog.recover_with is expected

    def test_an_unknown_mechanism_names_the_alternatives(self):
        with pytest.raises(ConfigError, match="bitmain_reboot"):
            self._load(self._cfg(recover_with="reboot"))

    def test_a_relative_reboot_path_is_refused(self):
        with pytest.raises(ConfigError, match="absolute path"):
            self._load(self._cfg(recover_with="bitmain_reboot", reboot_path="cgi-bin/reboot.cgi"))

    def test_a_restart_length_cooldown_is_refused_for_a_reboot(self):
        """A rebooted miner is down for minutes and spins up for several more.

        At a restart-sized cooldown the second attempt lands on a miner that is
        already recovering, reads as a failure, and spends the retry budget.
        """
        with pytest.raises(ConfigError, match="too short for"):
            self._load(self._cfg(recover_with="bitmain_reboot", cooldown_seconds=600))

    def test_that_floor_does_not_apply_to_the_cgminer_default(self):
        _, _, _, miners = self._load(self._cfg(cooldown_seconds=600))
        assert miners["a"].watchdog.cooldown_seconds == 600
