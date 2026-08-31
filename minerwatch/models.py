from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo


class State(str, Enum):
    MINING = "mining"
    STOPPED = "stopped"
    UNREACHABLE = "unreachable"


class SleepBackend(str, Enum):
    """How a miner is stopped in software.

    ``CGMINER`` drives the same JSON-over-TCP API the poller already uses.
    ``BITMAIN_HTTP`` drives Bitmain stock firmware's web UI, which exposes a
    persistent ``miner-mode`` setting (sleep) that the cgminer socket does not.
    ``NONE`` disables software power control for that miner.
    """

    CGMINER = "cgminer"
    BITMAIN_HTTP = "bitmain_http"
    NONE = "none"


class RecoverWith(str, Enum):
    """How the watchdog tries to recover a failing miner.

    ``CGMINER`` sends ``{"command":"restart"}`` over the API port. It restarts
    the mining process only, and several stock Bitmain builds do not implement
    it at all — they answer ``Invalid command``, which reads as a failed
    attempt and burns the retry budget for nothing.

    ``BITMAIN_REBOOT`` reboots the whole control board through the stock web
    UI's reboot CGI, using the HTTP credentials from the miner's ``sleep:``
    block. Heavier and slower, but it is the only software recovery that works
    on firmware without a cgminer ``restart``.

    ``AUTO`` sends the cgminer restart first and falls back to the reboot on
    the same attempt *only* when the firmware says the command does not exist.
    Any other failure — refused, unreachable, malformed — does not escalate,
    because those mean something other than "this firmware needs the heavier
    mechanism".
    """

    CGMINER = "cgminer"
    BITMAIN_REBOOT = "bitmain_reboot"
    AUTO = "auto"


@dataclass
class Range:
    start: int
    end: int

    @property
    def crosses_midnight(self) -> bool:
        return self.start > self.end


@dataclass
class Window:
    days: frozenset[int]
    ranges: list[Range]


@dataclass
class Schedule:
    timezone: ZoneInfo
    windows: list[Window]


@dataclass(frozen=True)
class Command:
    """One cgminer API call in a fallback chain."""

    command: str
    parameter: Optional[str] = None

    def __str__(self) -> str:
        return self.command if self.parameter is None else f"{self.command}:{self.parameter}"


#: Antminer firmwares disagree on how to stop hashing without cutting power, so
#: each backend tries a chain of commands and keeps the first that is accepted.
#: ``ascset|0,sleep`` is what Vnish and several other aftermarket builds expose;
#: ``pause``/``resume`` is Braiins OS+ (bosminer). The chain is overridable in
#: ``miners.yaml`` for firmware that names things differently.
DEFAULT_SLEEP_COMMANDS: tuple[Command, ...] = (
    Command("ascset", "0,sleep"),
    Command("pause"),
)
DEFAULT_WAKE_COMMANDS: tuple[Command, ...] = (
    Command("ascset", "0,wake"),
    Command("resume"),
)


@dataclass(frozen=True)
class SleepConfig:
    """Resolved software power-control settings for a single miner.

    Global defaults, group settings, and per-miner settings are merged at load
    time so that each :class:`Miner` carries a complete, self-contained policy
    and nothing has to be looked up again at runtime.
    """

    enabled: bool = False
    backend: SleepBackend = SleepBackend.CGMINER
    #: Never actuate hardware unless this is explicitly turned off.
    dry_run: bool = True
    #: Minimum seconds between two power actions on the same miner.
    cooldown_seconds: int = 300
    #: Seconds after a sleep during which a STOPPED reading is expected rather
    #: than alarming, and after a wake during which the miner is allowed to be
    #: still spinning up.
    grace_seconds: int = 180
    #: Consecutive failures before the miner is latched for manual attention.
    max_failures: int = 3
    sleep_commands: tuple[Command, ...] = DEFAULT_SLEEP_COMMANDS
    wake_commands: tuple[Command, ...] = DEFAULT_WAKE_COMMANDS
    #: cgminer API port; defaults to the miner's poll port when unset.
    api_port: Optional[int] = None
    # --- bitmain_http backend ------------------------------------------------
    http_scheme: str = "http"
    http_port: int = 80
    #: Field in the miner config that selects the power mode. Left unset the
    #: backend discovers it, which covers the firmwares that renamed it; set it
    #: explicitly for anything those candidates miss.
    mode_key: Optional[str] = None
    #: Values written to that field. 0/1 is the common Normal/Sleep pairing,
    #: but the mapping is firmware-specific and some builds use a third value
    #: for a low-power mode. Confirm against the miner's own web UI before
    #: trusting the default: set Work Mode there by hand, then run `check`,
    #: which prints the value the firmware chose.
    normal_value: int = 0
    sleep_value: int = 1
    #: How set_miner_conf.cgi wants the document: "json" (newer builds take the
    #: config back verbatim) or "form" (the older _ant_-prefixed encoding).
    post_format: str = "json"
    #: Shape of the write. "auto" sends the browser's shape when the read field
    #: has a known write alias, "mirror" always echoes the document back under
    #: the field it was read from, "browser" always uses the alias form.
    write_profile: str = "auto"
    #: Override the request Content-Type. Some CGI handlers check it, and the
    #: web UI sends text/plain even though the body is JSON.
    content_type: Optional[str] = None
    username: str = "root"
    password: str = "root"
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class WatchdogConfig:
    """Resolved restart policy for a single miner.

    Merged global → group → miner at load time, the same chain schedules and
    sleep settings use.
    """

    #: Turn restarts off entirely for this miner. It is still polled, still
    #: recorded, and still shows in `status`; nothing is ever sent to it.
    enabled: bool = True
    #: How long a miner must be *continuously* failing, inside its own working
    #: hours, before the first restart is sent. Without this the watchdog fires
    #: on the first non-mining poll, so a single dropped packet or one slow
    #: reply reboots bmminer. Time spent asleep or outside the window does not
    #: count towards it.
    fail_after_seconds: int = 1800
    #: Minimum gap between two restart attempts on the same miner.
    cooldown_seconds: int = 600
    #: Window over which ``max_restarts`` is counted.
    rate_window_seconds: int = 3600
    #: Attempts inside that window before the miner is latched for a human.
    max_restarts: int = 3
    #: Which recovery mechanism an attempt uses. Left at ``CGMINER`` the
    #: watchdog behaves exactly as it always has.
    recover_with: RecoverWith = RecoverWith.CGMINER
    #: Path to the stock firmware's reboot CGI, used by ``BITMAIN_REBOOT`` and
    #: by the ``AUTO`` fallback. Configurable because the path has moved
    #: between firmware generations.
    reboot_path: str = "/cgi-bin/reboot.cgi"


@dataclass
class Miner:
    id: str
    host: str
    port: int
    group: Optional[str] = None
    schedule: Optional[Schedule] = None
    sleep: SleepConfig = field(default_factory=SleepConfig)
    #: ``None`` means "use the Watchdog's own defaults" — the configuration
    #: file always supplies one, but a Miner built in isolation (a test, a
    #: one-off script) should not silently override a Watchdog constructed with
    #: explicit arguments.
    watchdog: Optional[WatchdogConfig] = None


@dataclass
class Event:
    ts: str
    miner: str
    state: str
    action: Optional[str] = None
    reason: Optional[str] = None
    #: Hashrate in GH/s at the moment of the poll, when the miner reported one.
    #: This is the number that separates a real sleep from a low-power mode:
    #: both stop full-rate mining, but only one goes to zero.
    ghs: Optional[float] = None
