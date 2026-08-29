# Architecture

MinerWatch is a single-process supervision loop. Every `poll_interval_seconds`
it asks each configured miner for a `summary` over the cgminer API, classifies
the reply, records it, and hands the result to two controllers in a fixed
order.

```
                    ┌─────────────┐
   miners.yaml ───▶ │   config    │  global → group → miner inheritance
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │   poller    │  summary → mining | stopped | unreachable
                    └──────┬──────┘
                           │ every reading
                           ▼
                    ┌─────────────┐
                    │    store    │  events table (SQLite) — the only state
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
      ┌───────────────┐         ┌───────────────┐
      │    sleeper    │ ──────▶ │   watchdog    │
      │ owns state?   │  yes:   │ restart?      │
      │               │ stand   │               │
      └───────┬───────┘  down   └───────┬───────┘
              ▼                         ▼
      ┌───────────────┐         ┌───────────────┐
      │   backends    │         │      api      │
      │ cgminer /     │         │ cgminer TCP   │
      │ bitmain_http  │         │ "restart"     │
      └───────────────┘         └───────────────┘
```

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Parse and validate `miners.yaml`; merge global → group → miner; lint for the mistakes that are silent at runtime |
| `models.py` | `Miner`, `Schedule`, `Window`, `Range`, `SleepConfig`, `WatchdogConfig` — plain data, no I/O |
| `schedule.py` | `is_working_time(miner, when)` — window evaluation, including ranges that cross midnight |
| `api.py` | The cgminer wire protocol: JSON commands, NUL-terminated replies read to completion |
| `poller.py` | One poll cycle: query, classify, record |
| `store.py` | SQLite `events` table, and every query the controllers rebuild their state from |
| `sleeper.py` | The schedule-driven sleep/wake state machine and its latches |
| `watchdog.py` | The restart decision, its failure clock, cooldown, rate limit and attention latch |
| `backends.py` | `cgminer` and `bitmain_http` power control, including field discovery and write verification |
| `compat.py` | Everything that differs between Linux and Windows (see [windows.md](windows.md)) |
| `cli.py` | Subcommands, the resolved-config report, and the operator-facing output |

## The events table is the only state

MinerWatch keeps nothing important in memory. Every poll and every decision is
a row in the `events` table, and every latch — asleep, spin-up grace, cooldown,
failure count, needs-attention — is **rebuilt from those rows at startup**.

This is what makes a restart mid-cycle safe. A process that starts twenty
minutes before a scheduled wake, with no memory of having slept anything, still
knows what it owes each miner. It is also why `status` reads the database rather
than the miners: it prints the last recorded poll, not a live query.

One deliberate exception: dry-run records (`would_sleep`, `would_wake`) are
**not** read back at startup. A rehearsal must never leave behind a belief that
a later live run treats as fact and acts on.

## Why the sleeper runs before the watchdog

A miner MinerWatch deliberately put to sleep reads back as `stopped` — exactly
the condition that makes the watchdog fire a restart. So each poll the sleep
controller goes first and reports whether it owns the miner's current state
(asleep on schedule, or inside the spin-up grace after a wake). When it does,
the watchdog stands down for that cycle.

| Situation | Who acts |
| --- | --- |
| Mining, outside its window | Sleeper sends sleep |
| Mining shortly after a sleep (within `grace_seconds`) | Nobody — still winding down |
| Stopped, outside its window, slept by us | Nobody — expected |
| Stopped, inside its window, slept by us | Sleeper sends wake |
| Stopped shortly after a wake (within `grace_seconds`) | Nobody — still spinning up |
| Stopped, inside its window, **not** slept by us | Watchdog restarts it |
| Unreachable | Watchdog |
| Mining well past `grace_seconds` while we believed it asleep | Latch cleared, then watchdog |

## Two gates before anything reaches hardware

Every actuator is rehearsed by default, at two independent levels:

- **Config** — `sleep.enabled: true` turns the feature on; `sleep.dry_run: false`
  stops rehearsing. Both must be set. `enabled: false` alone is not enough
  protection, because the moment anyone flips it, a group with `dry_run: false`
  behind it goes live with no rehearsal.
- **CLI** — `run` rehearses; `--live-sleep`, `--live-watchdog` and `--live`
  each open one half or both. `--dry-run` forces a rehearsal even where the
  config says otherwise.

In a rehearsal the full state machine still runs and records `would_sleep` /
`would_restart`, so a schedule can be validated against real miners at no risk.

Any new actuator must follow the same pattern: record the intent, run the state
machine in rehearsal, and send bytes only when explicitly enabled.

## Classification

`classify` decides `mining` / `stopped` / `unreachable` from hashrate alone.
An earlier version required `Status == "Alive"` inside `SUMMARY`; stock bmminer
never sends that field — it lives in the STATUS envelope, and per-device
liveness comes from `devs` — so every real miner fell through to "unexpected
payload" while only the simulator, which invented the field, looked healthy.
`sim/miner_sim.py --stock` reproduces the real reply shape so that regression
stays caught.

The two failure modes divide cleanly, and the difference decides what can fix
them:

- **`stopped`** — the API answers, hashrate is zero. A restart can genuinely fix
  this.
- **`unreachable`** — no TCP at all. A restart cannot; the attempts fail, burn
  the retry budget, and latch for a human. Only a switched PDU or smart relay
  can power-cycle a miner that has stopped answering.
