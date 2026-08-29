import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from minerwatch.models import Event


def init_db(path: str) -> sqlite3.Connection:
    # Create the parent directory first: on the Windows host the database
    # usually lives beside miners.yaml in a folder that may not exist yet, and
    # sqlite3.connect() reports a bare "unable to open database file" for a
    # missing directory, which is a confusing first-run failure.
    if path and not path.startswith(":"):
        parent = Path(path).expanduser().parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
    # timeout: SQLite's default 5s lock wait is short for a Windows host where
    # a virus scanner may briefly hold the file open.
    conn = sqlite3.connect(path, timeout=30.0)
    # WAL is unavailable on network shares and on :memory:; both are legitimate
    # deployments, so degrade to the default journal instead of failing.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:  # pragma: no cover - filesystem dependent
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            miner TEXT NOT NULL,
            state TEXT NOT NULL,
            action TEXT,
            reason TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_miner_ts ON events (miner, ts)")
    # Additive migration for databases created before hashrate was recorded.
    # ALTER TABLE ADD COLUMN is the one schema change SQLite does cheaply and
    # without rewriting the table, and an existing deployment must keep its
    # history rather than start over.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "ghs" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN ghs REAL")
    conn.commit()
    return conn


def record_event(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason, ghs) VALUES (?, ?, ?, ?, ?, ?)",
        (event.ts, event.miner, event.state, event.action, event.reason, event.ghs),
    )
    conn.commit()


def last_state(conn: sqlite3.Connection, miner: str) -> Event | None:
    row = conn.execute(
        "SELECT ts, miner, state, action, reason, ghs FROM events WHERE miner = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (miner,),
    ).fetchone()
    if row is None:
        return None
    return Event(ts=row[0], miner=row[1], state=row[2], action=row[3], reason=row[4],
                 ghs=row[5] if len(row) > 5 else None)


def is_needs_attention(conn: sqlite3.Connection, miner_id: str) -> bool:
    """Check if miner is latched as needs_attention (most recent action)."""
    row = conn.execute(
        "SELECT action FROM events WHERE miner = ? AND action IN ('needs_attention', 'attention_cleared') ORDER BY ts DESC, id DESC LIMIT 1",
        (miner_id,),
    ).fetchone()
    if row is None:
        return False
    return row[0] == "needs_attention"


def clear_needs_attention(conn: sqlite3.Connection, miner_id: str, now: datetime | None = None) -> None:
    """Record an attention_cleared event, releasing the latch."""
    if now is None:
        now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason) VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(), miner_id, "unknown", "attention_cleared", None),
    )
    conn.commit()


def get_last_event(conn: sqlite3.Connection, miner_id: str, action: str) -> Event | None:
    """Get the most recent event for a miner with a specific action."""
    row = conn.execute(
        "SELECT ts, miner, state, action, reason, ghs FROM events WHERE miner = ? AND action = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (miner_id, action),
    ).fetchone()
    if row is None:
        return None
    return Event(ts=row[0], miner=row[1], state=row[2], action=row[3], reason=row[4],
                 ghs=row[5] if len(row) > 5 else None)


def last_action_in(
    conn: sqlite3.Connection, miner_id: str, actions: "list[str] | tuple[str, ...]"
) -> Event | None:
    """Most recent event for *miner_id* whose action is one of *actions*.

    Used to recover latched state (asleep, needs-attention) after a restart:
    the events table is the only durable record MinerWatch keeps, so whatever
    it decided before a reboot has to be readable back out of it.
    """
    if not actions:
        return None
    placeholders = ",".join("?" for _ in actions)
    row = conn.execute(
        f"SELECT ts, miner, state, action, reason, ghs FROM events "
        f"WHERE miner = ? AND action IN ({placeholders}) ORDER BY ts DESC, id DESC LIMIT 1",
        (miner_id, *actions),
    ).fetchone()
    if row is None:
        return None
    return Event(ts=row[0], miner=row[1], state=row[2], action=row[3], reason=row[4],
                 ghs=row[5] if len(row) > 5 else None)


@dataclass(frozen=True)
class ClockRules:
    """How to read a failure clock out of the events table.

    The caller owns the meaning of each action; this module only walks rows.
    """

    #: Actions that start and sustain the clock.
    failure_actions: frozenset
    #: Actions that clear it: the miner is legitimately off, or unwatched.
    reset_actions: frozenset
    #: Actions whose ``state='mining'`` rows are trustworthy evidence of hashing.
    mining_actions: frozenset
    #: Actions that prove the miner is *not* hashing without themselves being a
    #: counted failure. These break a recovery run without starting the clock.
    not_mining_actions: frozenset
    #: How long a miner must mine continuously to count as recovered.
    recovery_seconds: int
    #: Largest gap between two mining observations that still counts as one
    #: continuous run. Longer than this and the run restarts, because silence
    #: is not evidence of hashing.
    max_gap_seconds: int
    #: Rows of history to consider. Bounded by count rather than by time on
    #: purpose - see :func:`failure_clock_start`.
    max_rows: int


def failure_clock_start(
    conn: sqlite3.Connection,
    miner_id: str,
    now: datetime,
    rules: ClockRules,
) -> str | None:
    """When *miner_id*'s current run of in-window failure began.

    Returns ``None`` when the miner is not currently in such a run — the
    failure, if this poll is one, starts now.

    Walks the miner's recent history forward. The clock starts at the first
    in-window failure and is cleared by a *reset*, of which there are two kinds,
    both learned from a concrete way a simpler version of this went wrong.

    **Leaving the working window resets it.** Measuring plain wall-clock time
    from the first failure meant one dropped packet at 17:59 kept ticking
    through a twelve-hour overnight shutdown, so every miner arrived at its
    window past the delay and was restarted during spin-up — every morning,
    across the whole fleet at once, since they share a window.

    **Sustained mining resets it; a single good poll does not.** Clearing on any
    one mining reading meant a miner hashing 1% of the time — a dying hashboard,
    an intermittent PSU — reset the clock every few minutes and was never
    restarted at all, which is worse than the behaviour this replaced. Recovery
    requires *continuous* mining for ``recovery_seconds``: a gap wider than
    ``max_gap_seconds`` starts the run over, because a poll that never happened
    is not evidence the miner was hashing, and an observation that it was *not*
    hashing (``not_mining_actions``) breaks the run outright.

    Only ``mining_actions`` rows count as evidence. The controllers write
    bookkeeping rows carrying whatever state they assumed at the time — a manual
    ``sleep`` preview records ``state='mining'`` for a miner that is unreachable
    — and letting those count would let an operator hold off a dying miner's
    restart indefinitely just by looking at it.

    History is bounded by **row count, not by elapsed time**. A time horizon
    looks equivalent and is not: the clock is built from rows, so with a long
    ``poll_interval_seconds`` a miner dead for days produces rows spaced further
    apart than the horizon and every one of them reads as the first failure —
    it would never be restarted at all. The same held for any MinerWatch outage
    longer than the horizon, which is exactly the invariant this function exists
    to protect.

    Rows stamped after *now* are excluded. ``attention_cleared`` is written with
    the real wall clock rather than the caller's, and a forward clock step that
    NTP later corrects would otherwise leave a classified row in the future that
    silently wipes the clock.
    """
    if not rules.failure_actions:
        return None
    rows = conn.execute(
        "SELECT ts, state, action FROM events WHERE miner = ? AND ts <= ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (miner_id, now.isoformat(), max(int(rules.max_rows), 1)),
    ).fetchall()
    rows.reverse()

    clock_start: str | None = None
    run_start: datetime | None = None
    run_last: datetime | None = None

    for ts_str, state, action in rows:
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):  # pragma: no cover - malformed row
            continue

        if action in rules.reset_actions:
            clock_start = None
            run_start = run_last = None
        elif state == "mining" and action in rules.mining_actions:
            if run_start is None or run_last is None or (
                (ts - run_last).total_seconds() > rules.max_gap_seconds
            ):
                # Either the first mining row, or too long since the last one
                # for the gap to count as continuous hashing.
                run_start = ts
            run_last = ts
            if (ts - run_start).total_seconds() >= rules.recovery_seconds:
                clock_start = None
        elif action in rules.failure_actions:
            run_start = run_last = None
            if clock_start is None:
                clock_start = ts_str
        elif action in rules.not_mining_actions:
            # Not a counted failure, but positive evidence the miner is not
            # hashing, so it cannot be part of a recovery run.
            run_start = run_last = None

    return clock_start


def miners_with_actions(
    conn: sqlite3.Connection, actions: "list[str] | tuple[str, ...]"
) -> list[str]:
    """Distinct miner ids that have ever recorded one of *actions*."""
    if not actions:
        return []
    placeholders = ",".join("?" for _ in actions)
    rows = conn.execute(
        f"SELECT DISTINCT miner FROM events WHERE action IN ({placeholders})",
        tuple(actions),
    ).fetchall()
    return [r[0] for r in rows]
