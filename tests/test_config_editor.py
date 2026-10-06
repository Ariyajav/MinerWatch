"""The config editor saves only what the supervisor would accept, and never loses a file."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from minerwatch import config_editor as ce
from minerwatch.config import load_config
from minerwatch.store import init_db
from minerwatch.web import make_server

BASE = """\
poll_interval_seconds: 15
db_path: events.db
groups:
  hall:
    # comments must survive a save
    schedule:
      windows:
        - days: [mon, tue, wed, thu, fri, sat, sun]
          ranges: ["08:00-20:00"]
    sleep:
      enabled: true
      dry_run: true
      backend: cgminer
miners:
  - { id: m1, host: 10.0.0.1, port: 4028, group: hall }
  - { id: m2, host: 10.0.0.2, port: 4028, group: hall }
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "miners.yaml"
    path.write_text(BASE, encoding="utf-8")
    return str(path)


class TestCheck:
    def test_the_saved_file_checks_clean_with_no_changes(self, cfg):
        c = ce.check(BASE, cfg)
        assert c["ok"] and c["errors"] == [] and c["changes"] == []
        assert [m["id"] for m in c["miners"]] == ["m1", "m2"]
        assert c["miners"][0]["sleep"] == "cgminer dry-run"

    def test_a_yaml_error_names_the_line(self, cfg):
        c = ce.check(BASE + "  - { id: m3, host: [\n", cfg)
        assert not c["ok"] and "line" in c["errors"][0]

    def test_a_config_error_is_the_supervisor_s_own_message(self, cfg):
        c = ce.check(BASE.replace("group: hall }", "group: nope }", 1), cfg)
        assert not c["ok"] and "Unknown group 'nope'" in c["errors"][0]

    def test_a_group_edit_that_goes_live_is_flagged_on_every_miner(self, cfg):
        c = ce.check(BASE.replace("dry_run: true", "dry_run: false"), cfg)
        live = [x for x in c["changes"] if x["field"] == "sleep"]
        assert [x["miner"] for x in live] == ["m1", "m2"]
        assert all(x["risky"] and "real sleep" in x["why"] for x in live)
        assert c["risky"] is True

    def test_added_and_removed_miners_are_listed(self, cfg):
        text = BASE.replace("  - { id: m2, host: 10.0.0.2, port: 4028, group: hall }\n",
                            "  - { id: m3, host: 10.0.0.3, port: 4028 }\n")
        c = ce.check(text, cfg)
        rows = {(x["miner"], x["after"]) for x in c["changes"]}
        assert ("m2", "removed") in rows and ("m3", "added") in rows

    def test_lint_warnings_come_through_once(self, cfg):
        c = ce.check(BASE.replace("10.0.0.2, port: 4028", "10.0.0.2, port: 80"), cfg)
        assert c["ok"]
        assert len([w for w in c["warnings"] if "m2" in w and "80" in w]) == 1

    def test_checking_leaves_no_temp_files_and_does_not_touch_the_config(self, cfg, tmp_path):
        ce.check("miners: oops", cfg)
        ce.check(BASE.replace("08:00", "07:00"), cfg)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["miners.yaml"]
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8") == BASE


class TestSave:
    def test_save_backs_up_the_old_file_and_keeps_comments(self, cfg, tmp_path):
        new = BASE.replace("08:00-20:00", "07:00-21:00")
        out = ce.save(new, cfg, ce.digest(BASE), now=datetime(2026, 10, 6, 9, 30, 0))
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8") == new
        assert "# comments must survive a save" in new
        assert out["backup"] == "miners-20261006-093000.yaml"
        assert ce.read_backup(cfg, out["backup"]) == BASE
        assert load_config(cfg)[3]["m1"].schedule.windows[0].ranges[0].start == 7 * 60

    def test_an_invalid_file_is_never_written(self, cfg, tmp_path):
        with pytest.raises(ce.ConfigError):
            ce.save("miners: 5\n", cfg, ce.digest(BASE))
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8") == BASE
        assert not (tmp_path / ce.BACKUP_DIR).exists()

    def test_an_edit_made_elsewhere_is_not_overwritten(self, cfg, tmp_path):
        (tmp_path / "miners.yaml").write_text(BASE + "# edited in Notepad\n", encoding="utf-8")
        with pytest.raises(ce.EditConflict):
            ce.save(BASE.replace("08:00", "07:00"), cfg, ce.digest(BASE))
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8").endswith("# edited in Notepad\n")

    def test_two_saves_in_one_second_keep_both_backups(self, cfg):
        when = datetime(2026, 10, 6, 9, 30, 0)
        a = BASE.replace("08:00", "07:00")
        ce.save(a, cfg, ce.digest(BASE), now=when)
        ce.save(BASE, cfg, ce.digest(a), now=when)
        assert len(ce.list_backups(cfg)) == 2

    def test_old_backups_are_pruned(self, cfg, monkeypatch):
        monkeypatch.setattr(ce, "KEEP_BACKUPS", 3)
        text = BASE
        for i in range(5):
            new = BASE + f"# save {i}\n"
            ce.save(new, cfg, ce.digest(text), now=datetime(2026, 10, 6, 9, 30, i))
            text = new
        assert len(ce.list_backups(cfg)) == 3

    def test_a_backup_name_cannot_reach_another_file(self, cfg):
        for name in ("../miners.yaml", "miners.yaml", "..\\miners.yaml", ""):
            with pytest.raises(FileNotFoundError):
                ce.read_backup(cfg, name)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def server(cfg, tmp_path):
    poll, db, _, miners = load_config(cfg)
    conn = init_db(db)
    srv = make_server("127.0.0.1", 0, db, miners, poll, config_path=cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", miners
    srv.shutdown()
    srv.server_close()
    conn.close()


def call(url, body=None, headers=None):
    hdrs = {"X-MinerWatch": "1", "Content-Type": "application/json"}
    hdrs.update(headers or {})
    hdrs = {k: v for k, v in hdrs.items() if v is not None}
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


class TestHttp:
    def test_load_check_save_and_restore(self, server, cfg, tmp_path):
        base, miners = server
        code, d = call(base + "/api/config")
        assert code == 200 and d["text"] == BASE and d["check"]["ok"]

        new = BASE.replace("  - { id: m2, host: 10.0.0.2, port: 4028, group: hall }\n",
                           "  - { id: m9, host: 10.0.0.9, port: 4028, group: hall }\n")
        code, c = call(base + "/api/config/check", {"text": new})
        assert code == 200 and c["ok"]
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8") == BASE

        code, s = call(base + "/api/config/save", {"text": new, "base_sha": d["sha"]})
        assert code == 200 and s["ok"] and s["backup"]
        # The dashboard follows the new miner list straight away.
        assert sorted(miners) == ["m1", "m9"]
        code, st = call(base + "/api/status")
        assert [m["id"] for m in st["miners"]] == ["m1", "m9"]

        code, b = call(base + "/api/config/backups")
        assert [x["name"] for x in b["backups"]] == [s["backup"]]
        code, old = call(base + "/api/config/backup?name=" + s["backup"])
        assert old["text"] == BASE

    def test_a_stale_base_is_a_conflict(self, server):
        base, _ = server
        code, d = call(base + "/api/config/save", {"text": BASE, "base_sha": "0" * 64})
        assert code == 409 and "changed by something else" in d["error"]

    def test_an_invalid_save_is_refused(self, server, cfg, tmp_path):
        base, _ = server
        code, d = call(base + "/api/config/save", {"text": "miners: 5", "base_sha": ce.digest(BASE)})
        assert code == 400 and "not valid" in d["error"]
        assert (tmp_path / "miners.yaml").read_text(encoding="utf-8") == BASE

    def test_reads_and_writes_need_the_header(self, server):
        base, _ = server
        assert call(base + "/api/config", headers={"X-MinerWatch": None})[0] == 403
        code, _ = call(base + "/api/config/save", {"text": BASE, "base_sha": ce.digest(BASE)},
                       headers={"X-MinerWatch": None})
        assert code == 403

    def test_a_rebinding_host_name_is_refused(self, server):
        base, _ = server
        code, d = call(base + "/api/config", headers={"Host": "evil.example:8787"})
        assert code == 403 and "localhost" in d["error"]
        assert call(base + "/api/config", headers={"Host": "localhost:8787"})[0] == 200

    def test_a_form_post_cannot_save(self, server):
        base, _ = server
        code, _ = call(base + "/api/config/save", {"text": BASE, "base_sha": ce.digest(BASE)},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert code == 403

    def test_without_a_config_path_the_editor_is_absent(self, tmp_path):
        db = str(tmp_path / "e.db")
        init_db(db).close()
        srv = make_server("127.0.0.1", 0, db, {}, 15)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            assert call(base + "/api/config")[1] == {"available": False}
            assert call(base + "/api/config/save", {"text": ""})[0] == 404
        finally:
            srv.shutdown()
            srv.server_close()


def test_remote_clients_are_refused(cfg, monkeypatch):
    """Simulated: the handler sees a LAN address instead of loopback."""
    import minerwatch.web as web

    monkeypatch.setattr(web, "LOOPBACK", ("10.9.9.9",))
    poll, db, _, miners = load_config(cfg)
    init_db(db).close()
    srv = make_server("127.0.0.1", 0, db, miners, poll, config_path=cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        code, d = call(base + "/api/config")
        assert code == 403 and d["can_edit"] is False and "text" not in d
    finally:
        srv.shutdown()
        srv.server_close()
