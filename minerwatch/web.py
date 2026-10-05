"""Web dashboard: the `status` and `history` views in a browser, plus alerts.

Nothing here can change a miner or the events database. Every request opens
its own SQLite connection in read-only mode (``mode=ro``), so a bug in this
module can at worst show the wrong thing, never send a command or write a row.
That is also why there are no miner controls: the CLI cannot change the
running supervisor's mind (its latches are in-memory, hydrated once at
startup), and a web control that wrote to the events table would have exactly
the same blind spot while looking far more authoritative.

The one thing the page can write is the email alert settings file (see
:mod:`minerwatch.alerts`). Those writes are accepted only from this PC unless
``--allow-remote-settings`` is given, and only with a custom header that a
cross-site form cannot send, so another web page open in the same browser
cannot quietly redirect the alerts.

The standard library is enough for a page that one operator refreshes every
few seconds, and it keeps the Windows install to the one ``pip install`` that
already works there.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from minerwatch import alerts as alerts_mod
from minerwatch.compat import ALLOW_REUSE_ADDRESS
from minerwatch.models import Miner, State
from minerwatch.schedule import is_working_time
from minerwatch.sleeper import SleepController
from minerwatch.store import is_needs_attention, last_action_in, last_state

logger = logging.getLogger("minerwatch")

#: Poller actions: the only rows that record an *observation* of the miner.
#: Mirrors ``cli.POLL_ACTIONS``; duplicated rather than imported so the CLI
#: can import this module lazily without a cycle.
POLL_ACTIONS = ("none", "alert", "expected_off")

#: Most hashrate points sent for one chart. A day of 15s polls is ~5800 rows;
#: a sparkline a few hundred pixels wide cannot show more than this anyway.
MAX_CHART_POINTS = 360


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the events database so that writing is impossible, not just avoided.

    ``Path.as_uri()`` produces ``file:///D:/...`` on Windows, which SQLite's
    URI parser accepts; a hand-built ``file:D:\\...`` string does not survive
    the backslashes.
    """
    if db_path.startswith(":"):
        raise ValueError("the dashboard needs a database file, not an in-memory one")
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30.0, check_same_thread=False)


def snapshot(conn, miners: dict[str, Miner], poll_interval: int,
             now: datetime | None = None) -> dict:
    """Everything the dashboard's main table shows, as plain JSON-able data.

    Built from the same reads as ``cmd_status`` so the two can never disagree:
    the state and hashrate come from the newest *poll* row, and the latches
    from the same store functions the controllers hydrate from.
    """
    from minerwatch.cli import ACTION_MEANING, _diagnose

    now = now or datetime.now(timezone.utc)
    controller = SleepController(conn, miners)
    rows = []
    newest_poll: datetime | None = None
    for miner in miners.values():
        poll = last_action_in(conn, miner.id, POLL_ACTIONS)
        last = poll or last_state(conn, miner.id)
        decision = _last_decision(conn, miner.id, now)
        if not miner.sleep.enabled:
            power = "manual"
        elif controller.is_asleep(miner.id):
            power = "asleep"
        elif controller.is_uncertain(miner.id):
            power = "unsure"
        else:
            power = "awake"
        latches = []
        if is_needs_attention(conn, miner.id):
            latches.append("watchdog")
        if controller.needs_attention(miner.id):
            latches.append("sleep")
        latched_since = _latched_since(conn, miner.id) if latches else None

        seen = _parse(last.ts) if last else None
        if poll is not None:
            polled = _parse(poll.ts)
            if polled and (newest_poll is None or polled > newest_poll):
                newest_poll = polled

        reason = poll.reason if poll is not None and poll.state != State.MINING.value else None
        rows.append({
            "id": miner.id,
            "group": miner.group,
            "host": miner.host,
            "state": last.state if last else "unknown",
            "ghs": poll.ghs if poll is not None else None,
            "window": "open" if is_working_time(miner, now) else "closed",
            "power": power,
            "latches": latches,
            "latched_since": latched_since,
            "last_seen": seen.isoformat() if seen else None,
            "reason": reason,
            "diagnosis": _diagnose(reason) if reason else None,
            "last_decision": decision and {
                "ts": decision[0], "action": decision[1],
                "meaning": decision[2] or ACTION_MEANING.get(decision[1], ""),
            },
        })

    # A latch is silent by design, and so is a supervisor that has stopped
    # polling: `status` happily prints the last rows it has. Both have cost
    # this fleet days of unnoticed downtime, so the page says so at the top
    # rather than leaving it to someone reading timestamps.
    stale_after = max(3 * poll_interval, 60)
    poll_age = (now - newest_poll).total_seconds() if newest_poll else None
    return {
        "now": now.isoformat(),
        "poll_interval": poll_interval,
        "newest_poll": newest_poll.isoformat() if newest_poll else None,
        "stale": poll_age is None or poll_age > stale_after,
        "poll_age_seconds": poll_age,
        "miners": rows,
    }


def history(conn, miner_id: str, hours: float, now: datetime | None = None) -> dict:
    """Hashrate series and the controllers' decisions for one miner."""
    from minerwatch.cli import ACTION_MEANING

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" for _ in POLL_ACTIONS)
    polls = conn.execute(
        f"SELECT ts, state, ghs FROM events WHERE miner = ? AND ts >= ? "
        f"AND action IN ({placeholders}) ORDER BY ts, id",
        (miner_id, since, *POLL_ACTIONS),
    ).fetchall()
    step = max(1, -(-len(polls) // MAX_CHART_POINTS))
    # Keep the minimum of each bucket, not every n-th row: a short drop to
    # zero is the thing the chart exists to show, and sampling would skip it.
    points = []
    for i in range(0, len(polls), step):
        bucket = polls[i:i + step]
        low = min(bucket, key=lambda r: r[2] if r[2] is not None else -1)
        points.append({"ts": bucket[0][0], "state": low[1], "ghs": low[2]})

    decisions = conn.execute(
        f"SELECT ts, state, action, reason FROM events WHERE miner = ? AND ts >= ? "
        f"AND action IS NOT NULL AND action NOT IN ({placeholders}) "
        f"ORDER BY ts DESC, id DESC LIMIT 200",
        (miner_id, since, *POLL_ACTIONS),
    ).fetchall()
    return {
        "miner": miner_id,
        "hours": hours,
        "points": points,
        "decisions": [
            {"ts": ts, "state": state, "action": action,
             "why": reason or ACTION_MEANING.get(action, "")}
            for ts, state, action, reason in decisions
        ],
    }


def _last_decision(conn, miner_id: str, now: datetime):
    # Bounded by time so the (miner, ts) index does the work: a monitor-only
    # miner has never had a decision, and without the bound every refresh would
    # scan its entire history looking for one.
    since = (now - timedelta(days=14)).isoformat()
    placeholders = ",".join("?" for _ in POLL_ACTIONS)
    return conn.execute(
        f"SELECT ts, action, reason FROM events WHERE miner = ? AND ts >= ? "
        f"AND action IS NOT NULL AND action NOT IN ({placeholders}) "
        f"ORDER BY ts DESC, id DESC LIMIT 1",
        (miner_id, since, *POLL_ACTIONS),
    ).fetchone()


def _latched_since(conn, miner_id: str) -> str | None:
    row = conn.execute(
        "SELECT ts FROM events WHERE miner = ? AND action IN "
        "('needs_attention', 'sleep_needs_attention') ORDER BY ts DESC, id DESC LIMIT 1",
        (miner_id,),
    ).fetchone()
    return row[0] if row else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

#: Largest settings body accepted. The form is a few hundred bytes.
MAX_BODY_BYTES = 64 * 1024

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def make_server(host: str, port: int, db_path: str, miners: dict[str, Miner],
                poll_interval: int, alerts_path: str | None = None,
                monitor: "alerts_mod.AlertMonitor | None" = None,
                allow_remote_settings: bool = False) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MinerWatch"

        def do_GET(self):  # noqa: N802 - http.server's naming
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if url.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if url.path == "/api/status":
                    with _closing(open_readonly(db_path)) as conn:
                        return self._json(200, snapshot(conn, miners, poll_interval))
                if url.path == "/api/history":
                    miner_id = (query.get("miner") or [""])[0]
                    if miner_id not in miners:
                        return self._json(404, {"error": f"unknown miner {miner_id!r}"})
                    try:
                        hours = min(max(float((query.get("hours") or ["24"])[0]), 0.25), 24 * 14)
                    except ValueError:
                        return self._json(400, {"error": "hours must be a number"})
                    with _closing(open_readonly(db_path)) as conn:
                        return self._json(200, history(conn, miner_id, hours))
                if url.path == "/api/alerts":
                    if alerts_path is None:
                        return self._json(200, {"available": False})
                    try:
                        settings = alerts_mod.load_settings(alerts_path)
                    except (ValueError, TypeError) as exc:
                        return self._json(500, {"error": f"{alerts_path} is not valid: {exc}"})
                    return self._json(200, {
                        "available": True,
                        "path": alerts_path,
                        "settings": settings.public(),
                        "status": monitor.status if monitor else None,
                        "can_edit": allow_remote_settings or self._is_local(),
                    })
                return self._json(404, {"error": "not found"})
            except sqlite3.OperationalError as exc:
                # Usually "unable to open database file": the supervisor has not
                # created it yet, or the dashboard was pointed at the wrong config.
                return self._json(503, {"error": f"cannot read {db_path}: {exc}"})

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            if alerts_path is None or url.path not in ("/api/alerts", "/api/alerts/test"):
                return self._json(405, {"error": "the dashboard is read-only"})
            refusal = self._refuse_write()
            if refusal:
                return self._json(403, {"error": refusal})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    return self._json(413, {"error": "request too large"})
                update = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(update, dict):
                    raise ValueError("expected a JSON object")
                current = alerts_mod.load_settings(alerts_path)
                candidate = alerts_mod.merge_update(current, update)
            except (ValueError, TypeError, KeyError) as exc:
                return self._json(400, {"error": f"bad settings: {exc}"})

            if url.path == "/api/alerts/test":
                # Test exactly what is in the form, saved or not, so a typo is
                # found before it is relied on.
                problems = [p for p in alerts_mod.AlertSettings(
                    **{**candidate.__dict__, "enabled": True}).validate()]
                if problems:
                    return self._json(400, {"error": "; ".join(problems)})
                try:
                    alerts_mod.send_email(
                        candidate, "Test email",
                        "This is a test from the MinerWatch dashboard. If you can read "
                        "it, alert emails will reach you.\n")
                except Exception as exc:
                    return self._json(502, {"error": f"sending failed: {exc}"})
                return self._json(200, {"ok": True, "sent_to": candidate.to_addrs})

            problems = candidate.validate()
            if problems:
                return self._json(400, {"error": "; ".join(problems)})
            alerts_mod.save_settings(alerts_path, candidate)
            logger.info("alerts: settings saved to %s by %s", alerts_path, self.client_address[0])
            return self._json(200, {"ok": True, "settings": candidate.public()})

        def do_PUT(self):  # noqa: N802
            self._json(405, {"error": "the dashboard is read-only"})

        do_DELETE = do_PATCH = do_PUT

        def _is_local(self) -> bool:
            return self.client_address[0] in LOOPBACK

        def _refuse_write(self) -> str | None:
            if not (allow_remote_settings or self._is_local()):
                return ("settings can only be changed from the MinerWatch PC itself "
                        "(start the dashboard with --allow-remote-settings to change that)")
            # A cross-site <form> cannot set a custom header or a JSON content
            # type without a CORS preflight, which this server never approves.
            if self.headers.get("X-MinerWatch") != "1":
                return "missing X-MinerWatch header"
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return "expected application/json"
            return None

        def _json(self, code: int, body: dict):
            self._send(code, json.dumps(body).encode("utf-8"), "application/json")

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            logger.debug("web: " + fmt, *args)

    class Server(ThreadingHTTPServer):
        # HTTPServer turns SO_REUSEADDR on, which on Windows lets a second
        # process take over a port that is already serving. See AGENTS.md.
        allow_reuse_address = ALLOW_REUSE_ADDRESS
        daemon_threads = True

    return Server((host, port), Handler)


class _closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def serve(host: str, port: int, db_path: str, miners: dict[str, Miner],
          poll_interval: int, alerts_path: str | None = None,
          allow_remote_settings: bool = False) -> int:
    monitor = None
    if alerts_path is not None:
        monitor = alerts_mod.AlertMonitor(alerts_path, db_path, miners, poll_interval,
                                          snapshot, open_readonly)
    server = make_server(host, port, db_path, miners, poll_interval, alerts_path,
                         monitor, allow_remote_settings)
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    print(f"MinerWatch dashboard on http://{shown}:{server.server_address[1]}/")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print("Listening beyond this PC: anyone on the network can see the fleet's state.")
    if monitor is not None:
        print(f"Email alerts: settings in {alerts_path} (edit them on the page).")
        monitor.start()
    print("Ctrl+C to stop. The supervisor is not affected either way.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor is not None:
            monitor.stop()
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MinerWatch</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #fff; --text: #1b1f24; --muted: #5f6b7a; --line: #e2e6eb;
  --ok: #1f8a4c; --warn: #b26b00; --bad: #c62828; --info: #3559c7; --idle: #7a8594;
  --okbg: #e7f5ec; --warnbg: #fdf3e1; --badbg: #fdeaea; --infobg: #e9eefb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151a; --panel: #1b2027; --text: #e6e9ed; --muted: #97a3b2; --line: #2c333d;
    --ok: #4cc27f; --warn: #e6a23c; --bad: #ef6b6b; --info: #7d9bf0; --idle: #8792a2;
    --okbg: #17301f; --warnbg: #352915; --badbg: #3a1d1d; --infobg: #1d2640;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; align-items: baseline; gap: 16px; padding: 16px 20px 8px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; }
.muted { color: var(--muted); }
main { padding: 0 20px 24px; max-width: 1200px; }
.banner { border-radius: 8px; padding: 10px 14px; margin: 8px 0; }
.banner.bad { background: var(--badbg); color: var(--bad); }
.banner.warn { background: var(--warnbg); color: var(--warn); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 12px 0; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr.sel { background: var(--infobg); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.mining { background: var(--okbg); color: var(--ok); }
.stopped, .unreachable, .latched { background: var(--badbg); color: var(--bad); }
.asleep, .unsure { background: var(--infobg); color: var(--info); }
.other { background: var(--warnbg); color: var(--warn); }
.why { white-space: normal; color: var(--muted); font-size: 12px; max-width: 360px; }
#detail { margin-top: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
#detail h2 { font-size: 16px; margin: 0 0 8px; }
#detail .controls { float: right; }
select { background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
svg { width: 100%; height: 140px; display: block; }
.dec td { font-size: 13px; }
.dec td.why { max-width: none; }
header button, .btn { background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 12px; font: inherit; cursor: pointer; }
.btn.primary { background: #3559c7; border-color: #3559c7; color: #fff; }
.btn:disabled { opacity: .5; cursor: default; }
header .spacer { flex: 1; }
#alerts { margin: 12px 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
#alerts h2 { font-size: 16px; margin: 0 0 4px; }
#alerts h3 { font-size: 13px; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 16px; }
.grid label, .checks label { display: block; font-size: 13px; color: var(--muted); }
.grid input, .grid select { display: block; width: 100%; margin-top: 3px; padding: 6px 8px; font: inherit;
  background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; }
.checks label { color: var(--text); margin: 6px 0; }
.checks input[type=number] { width: 70px; padding: 3px 6px; background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 6px; font: inherit; }
.actions { display: flex; gap: 8px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
#amsg.ok { color: var(--ok); } #amsg.bad { color: var(--bad); }
</style>
</head>
<body>
<header>
  <h1>MinerWatch</h1>
  <span class="muted" id="updated">loading…</span>
  <span class="muted">refreshes every 15s</span>
  <span class="spacer"></span>
  <button id="alertsBtn" hidden>Email alerts</button>
</header>
<main>
  <section id="alerts" hidden>
    <h2>Email alerts</h2>
    <div class="muted" id="astatus"></div>
    <form id="aform" autocomplete="off">
      <h3>SMTP server</h3>
      <div class="grid">
        <label>Host<input name="smtp_host" placeholder="smtp.gmail.com"></label>
        <label>Port<input name="smtp_port" type="number" min="1" max="65535"></label>
        <label>Security<select name="security">
          <option value="starttls">STARTTLS (usually port 587)</option>
          <option value="ssl">SSL/TLS (usually port 465)</option>
          <option value="none">None (local relay only)</option></select></label>
        <label>Username<input name="username" autocomplete="off"></label>
        <label>Password<input name="password" type="password" autocomplete="new-password"></label>
        <label>From address<input name="from_addr" placeholder="minerwatch@example.com"></label>
        <label>Send to (comma-separated)<input name="to_addrs" placeholder="you@example.com"></label>
        <label>Subject prefix<input name="subject_prefix"></label>
      </div>
      <h3>When to email</h3>
      <div class="checks">
        <label><input type="checkbox" name="enabled"> <b>Alerts on</b></label>
        <label><input type="checkbox" name="on_latched"> A miner is latched and MinerWatch has stopped acting on it</label>
        <label><input type="checkbox" name="on_supervisor_stale"> The supervisor stops polling</label>
        <label><input type="checkbox" name="on_sleep_unsure"> A sleep failed or was not confirmed, so the miner may be asleep unnoticed</label>
        <label>A miner is not mining inside its window for <input type="number" name="not_mining_minutes" min="0"> minutes (0 = off)</label>
        <label>Repeat an active alert every <input type="number" name="repeat_hours" min="0" step="0.5"> hours (0 = once)</label>
        <label><input type="checkbox" name="send_resolved"> Email again when it clears</label>
      </div>
      <div class="actions">
        <button class="btn primary" type="submit" id="asave">Save</button>
        <button class="btn" type="button" id="atest">Send test email</button>
        <span id="amsg"></span>
      </div>
    </form>
  </section>
  <div id="banners"></div>
  <div class="tiles" id="tiles"></div>
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Miner</th><th>Group</th><th>State</th><th class="num">Hashrate</th>
        <th>Window</th><th>Power</th><th>Attention</th><th>Last seen</th><th>Last decision</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <section id="detail" hidden>
    <div class="controls">
      <select id="hours">
        <option value="6">6 hours</option><option value="24" selected>24 hours</option>
        <option value="72">3 days</option><option value="168">7 days</option>
      </select>
    </div>
    <h2 id="dtitle"></h2>
    <div id="chart"></div>
    <div class="muted" id="dwhy"></div>
    <table class="dec"><thead><tr><th>When</th><th>State</th><th>Decision</th><th>Why</th></tr></thead>
      <tbody id="decisions"></tbody></table>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let selected = null;

function rate(ghs) {
  if (ghs == null) return "–";
  if (ghs === 0) return "0";
  return ghs >= 1000 ? (ghs / 1000).toFixed(1) + " TH/s" : Math.round(ghs) + " GH/s";
}
function when(ts) {
  if (!ts) return "never";
  const d = new Date(ts);
  return d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts)) / 1000);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 172800) return (s / 3600).toFixed(1) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
function pill(text, cls) { return `<span class="pill ${cls}">${esc(text)}</span>`; }
function stateCls(s) { return ["mining","stopped","unreachable"].includes(s) ? s : "other"; }

async function refresh() {
  let data;
  try {
    const r = await fetch("api/status", {cache: "no-store"});
    data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
  } catch (e) {
    $("banners").innerHTML = `<div class="banner bad">Cannot read the dashboard data: ${esc(e.message)}</div>`;
    return;
  }
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  const banners = [];
  if (data.stale) {
    banners.push(data.newest_poll
      ? `No poll recorded for ${esc(ago(data.newest_poll).replace(" ago", ""))} (expected every ${data.poll_interval}s). The supervisor may not be running.`
      : "No poll has ever been recorded in this database. Is the supervisor running with this config?");
  }
  const latched = data.miners.filter(m => m.latches.length);
  for (const m of latched) {
    banners.push(`<b>${esc(m.id)}</b> is latched (${esc(m.latches.join(" + "))}) since ${esc(when(m.latched_since))}, ${esc(ago(m.latched_since))}. MinerWatch will not act on it until cleared, and clearing needs a supervisor restart.`);
  }
  $("banners").innerHTML = banners.map(b => `<div class="banner ${data.stale && b === banners[0] ? "warn" : "bad"}">${b}</div>`).join("");

  const ms = data.miners;
  const total = ms.reduce((a, m) => a + (m.state === "mining" && m.ghs ? m.ghs : 0), 0);
  const count = (f) => ms.filter(f).length;
  const tiles = [
    ["Fleet hashrate", rate(total)],
    ["Mining", `${count(m => m.state === "mining")} / ${ms.length}`],
    ["Not mining in window", count(m => m.state !== "mining" && m.window === "open")],
    ["Asleep", count(m => m.power === "asleep")],
    ["Latched", latched.length],
  ];
  $("tiles").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="muted">${k}</div><div class="n">${esc(v)}</div></div>`).join("");

  $("rows").innerHTML = ms.map(m => `
    <tr data-id="${esc(m.id)}" class="${m.id === selected ? "sel" : ""}">
      <td><b>${esc(m.id)}</b><div class="muted" style="font-size:12px">${esc(m.host)}</div></td>
      <td>${esc(m.group || "–")}</td>
      <td>${pill(m.state, stateCls(m.state))}${m.diagnosis || m.reason ? `<div class="why">${esc(m.diagnosis || m.reason)}</div>` : ""}</td>
      <td class="num">${rate(m.ghs)}</td>
      <td>${esc(m.window)}</td>
      <td>${["asleep","unsure"].includes(m.power) ? pill(m.power, m.power) : esc(m.power)}</td>
      <td>${m.latches.length ? pill(m.latches.join(" + "), "latched") : "–"}</td>
      <td title="${esc(m.last_seen)}">${esc(ago(m.last_seen) || "never")}</td>
      <td>${m.last_decision ? `${esc(m.last_decision.action)}<div class="why">${esc(ago(m.last_decision.ts))}</div>` : "–"}</td>
    </tr>`).join("");
  for (const tr of $("rows").querySelectorAll("tr")) tr.onclick = () => select(tr.dataset.id);
  if (selected) loadHistory();
}

function select(id) { selected = id; refresh(); }
$("hours").onchange = () => loadHistory();

async function loadHistory() {
  const hours = $("hours").value;
  const r = await fetch(`api/history?miner=${encodeURIComponent(selected)}&hours=${hours}`, {cache: "no-store"});
  const h = await r.json();
  $("detail").hidden = false;
  $("dtitle").textContent = `${h.miner}: last ${$("hours").selectedOptions[0].text}`;
  $("chart").innerHTML = chart(h.points, h.hours);
  $("dwhy").textContent = h.points.length ? "" : "No polls recorded in this period.";
  $("decisions").innerHTML = h.decisions.length ? h.decisions.map(d => `
    <tr><td>${esc(when(d.ts))}</td><td>${pill(d.state, stateCls(d.state))}</td><td>${esc(d.action)}</td><td class="why">${esc(d.why)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No decisions in this period: neither the sleep controller nor the watchdog acted on this miner.</td></tr>`;
}

function chart(points, hours) {
  if (!points.length) return "";
  const W = 1000, H = 140, pad = 4;
  const end = Date.now(), start = end - hours * 3600e3;
  const max = Math.max(1, ...points.map(p => p.ghs || 0));
  const x = (ts) => pad + (W - 2 * pad) * (new Date(ts) - start) / (end - start);
  const y = (g) => H - pad - (H - 2 * pad) * (g || 0) / max;
  const gaps = points.filter(p => p.state !== "mining").map(p =>
    `<rect x="${x(p.ts).toFixed(1)}" y="0" width="3" height="${H}" fill="var(--bad)" opacity=".18"/>`).join("");
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.ghs).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="hashrate">
    ${gaps}<path d="${line}" fill="none" stroke="var(--ok)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    </svg><div class="muted" style="font-size:12px">peak ${rate(max)} · red bands: not mining</div>`;
}

// ---- email alerts -------------------------------------------------------
const form = $("aform");
const BOOLS = ["enabled","on_latched","on_supervisor_stale","on_sleep_unsure","send_resolved"];
let alertsInfo = null;

async function loadAlerts(fillForm) {
  const r = await fetch("api/alerts", {cache: "no-store"});
  const a = await r.json();
  if (!a.available) return;
  alertsInfo = a;
  $("alertsBtn").hidden = false;
  const st = a.status || {};
  const bits = [a.settings.enabled ? "On" : "Off", `settings file: ${a.path}`];
  if (st.last_sent) bits.push("last email " + ago(st.last_sent));
  if (st.active && st.active.length) bits.push(`${st.active.length} active: ${st.active.map(x => x.title).join("; ")}`);
  $("astatus").innerHTML = esc(bits.join(" · ")) +
    (st.last_error ? `<div style="color:var(--bad)">Last error: ${esc(st.last_error)}</div>` : "") +
    (a.can_edit ? "" : `<div style="color:var(--warn)">View only: settings can be changed from the MinerWatch PC itself.</div>`);
  if (!fillForm) return;
  const s = a.settings;
  for (const el of form.elements) {
    if (!el.name) continue;
    if (BOOLS.includes(el.name)) el.checked = !!s[el.name];
    else if (el.name === "to_addrs") el.value = (s.to_addrs || []).join(", ");
    else if (el.name === "password") { el.value = ""; el.placeholder = s.password_set ? "saved - leave blank to keep" : ""; }
    else el.value = s[el.name] ?? "";
    el.disabled = !a.can_edit;
  }
  $("asave").disabled = $("atest").disabled = !a.can_edit;
}

function formData() {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    out[el.name] = BOOLS.includes(el.name) ? el.checked : el.value;
  }
  return out;
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-MinerWatch": "1"}});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
function say(text, ok) { $("amsg").textContent = text; $("amsg").className = ok ? "ok" : "bad"; }

form.onsubmit = async (e) => {
  e.preventDefault();
  try { await post("api/alerts", formData()); say("Saved.", true); loadAlerts(true); }
  catch (err) { say(err.message, false); }
};
$("atest").onclick = async () => {
  say("Sending…", true);
  try { const d = await post("api/alerts/test", formData()); say("Test email sent to " + d.sent_to.join(", ") + ".", true); }
  catch (err) { say(err.message, false); }
};
$("alertsBtn").onclick = () => {
  const sec = $("alerts");
  sec.hidden = !sec.hidden;
  if (!sec.hidden) loadAlerts(true);
};

refresh();
loadAlerts(false);
setInterval(() => { refresh(); if (!$("alerts").hidden) loadAlerts(false); }, 15000);
</script>
</body>
</html>
"""
