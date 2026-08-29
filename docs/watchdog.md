# Monitoring and automatic restarts

## What monitoring does

Every `poll_interval_seconds` MinerWatch asks each miner for a `summary` over
the cgminer API and classifies the reply as `mining`, `stopped`, or
`unreachable`, recording the hashrate alongside it. Every poll is written to the
events table — that table is the only durable state MinerWatch keeps, and every
latch it holds is rebuilt from it on startup.

Each non-mining poll then goes to two controllers, in order: the sleep
controller decides whether it owns the miner's state, and if it does the
watchdog stands down for that cycle. Otherwise the watchdog decides whether to
restart. See [architecture.md](architecture.md#why-the-sleeper-runs-before-the-watchdog).

## The restart decision, in order

| # | Condition | Recorded as |
| --- | --- | --- |
| 1 | mining | nothing |
| 2 | restarts disabled for this miner | `skipped_watchdog_disabled` |
| 3 | outside its working hours | `skipped_outside_hours` |
| 4 | latched for manual attention | `skipped_needs_attention` |
| 5 | failing for less than `fail_after_seconds` | `waiting_to_restart` |
| 6 | inside `cooldown_seconds` of the last attempt | `skipped_cooldown` |
| 7 | `max_restarts` reached inside `rate_window_seconds` | `needs_attention` (latched) |
| 8 | rehearsing | `would_restart` |
| 9 | — | `restart` / `restart_failed` |

Step 3 sits before step 4 deliberately: with the latch checked first, a latched
miner wrote `skipped_needs_attention` all night, and that action counts towards
the failure clock.

## Configuration

```yaml
watchdog:
  enabled: true
  fail_after_seconds: 1800   # continuous in-window failure before the first restart
  cooldown_seconds: 600      # between attempts
  rate_window_seconds: 3600
  max_restarts: 3            # then latch for a human
```

Merged global → group → miner, the same chain as schedules and sleep.
`enabled: false` keeps a miner monitored and recorded but never restarted.
`config` shows the resolved policy in a RESTART column and names any miner with
restarts off.

`cooldown_seconds × (max_restarts − 1)` must stay under `rate_window_seconds`,
or the oldest attempt is always evicted before the newest arrives, the limit is
never reached, and a miner that restarting cannot fix is restarted forever
instead of latching. The config refuses such a policy with an explanation.

Whether restarts are *sent* is separate from configuration: `run --live` sends
them, anything else rehearses them as `would_restart`. There is no per-miner
dry-run for restarts the way there is for sleep.

## The failure clock

`fail_after_seconds` measures **continuous in-window failure**, derived on each
poll from the events table rather than held in memory. The rules that survived
two adversarial review rounds, each with a regression test in
`tests/test_watchdog_delay.py`:

- **Leaving the working window resets it.** Measuring plain wall-clock time from
  the first failure meant one dropped packet at 17:59 kept ticking through a
  twelve-hour overnight shutdown, so every miner arrived at its window past the
  delay and was restarted mid-spin-up — the whole fleet at once, since they
  share a window, every morning.
- **Recovery requires *sustained* mining**, not one good poll. Clearing on any
  single mining reading meant a miner hashing 1% of the time — a dying
  hashboard — reset its clock every few minutes and was never restarted at all,
  which is worse than the immediate restarts the delay replaced.
- **A gap in the record is not evidence of mining.** One lucky reading, a
  five-minute service restart, one more lucky reading used to count as five
  minutes of continuous hashing. Observations in a recovery run must be no
  further apart than a few poll intervals.
- **Only the poller's own observations count as mining.** The controllers write
  bookkeeping rows carrying whatever state they assumed — a manual `sleep`
  preview records `state='mining'` for a miner that is unreachable — so an
  operator could hold off a dying miner's restart indefinitely just by looking
  at it.
- **History is bounded by row count, not elapsed time.** A time horizon looks
  equivalent and is not: the clock is built from rows, so with
  `poll_interval_seconds: 7200` consecutive failure rows sit further apart than
  any horizon and a miner dead for three days read as "first failure" on every
  poll — never restarted. The same held for any MinerWatch outage longer than
  the horizon.
- **Rows stamped in the future are ignored.** `attention_cleared` uses the real
  wall clock, and a forward clock step NTP later corrects left a classified row
  ahead of `now` that wiped the clock.
- **The clock survives a service restart**, because a host that crash-loops
  every twenty minutes would otherwise hand every miner a fresh delay each time
  and silently disable the watchdog exactly when it is needed.

## The limit worth knowing

The restart is `{"command":"restart"}` over the cgminer TCP API — it restarts
the mining process, it does not reboot the box, and **it needs a working TCP
connection**. An `unreachable` miner cannot be restarted in software at all: the
attempts fail, burn the retry budget, and latch for a human after
`max_restarts`. That latch is the correct outcome — it is MinerWatch saying
someone has to walk over there — but it is not a fix. Only a switched PDU or a
smart relay can power-cycle a miner that has stopped answering.

## The latch, seen in anger

Worth recording because the symptom was "nothing works" and the cause was the
system behaving exactly as designed.

The **whole fleet went unreachable within seconds of each other**, and the
poller itself stalled for about three minutes. Each of the twelve miners burned
its three restart attempts and latched `needs_attention`. Both controllers then
stood down on every miner — for hours — and every event row read
`skipped_needs_attention`. Restarts appeared broken; scheduled sleep appeared
broken. Neither was.

Twelve miners failing simultaneously is a switch, a breaker or the network, not
twelve miner faults, and no amount of restarting fixes it. The latch is the
system declining to paper over that. The miners recovered on their own;
`clear-attention all` released the latches and the fleet returned to full
hashrate immediately.

The operational lessons:

- **`ATTENTION` in `status` is the column that tells you.** A fleet-wide latch
  is invisible everywhere else until you look at `history`.
- **Diagnose before clearing.** Clearing without knowing why they stopped just
  means re-latching an hour later.
- **This is the argument for leaving restarts rehearsed.** Under a live mode
  this event would have fired twelve real restarts into a network fault they
  could not fix, then latched anyway.

## Reading what happened

```bash
python -m minerwatch -c miners.yaml history <miner|all> --decisions --hours 24
```

Replays the events table with a legend, collapsing runs of identical decisions.
`--decisions` hides the routine per-poll readings. It distinguishes "never
polled" from "polled all day and never acted on" — those look identical in a
silent log and are different faults.

## Recovering a latched miner

```bash
python -m minerwatch -c miners.yaml clear-attention <miner|all>
```

This releases both the watchdog and sleep latches and restores the retry
budget. The restore is applied against that miner's own `max_restarts`;
trimming against a different value made clearing a no-op for any miner
configured below the default, leaving restarts off permanently with nothing in
the CLI to show it.
