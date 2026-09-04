"""launcher `config` 动词——键注册表路由（方案 B，MD-149..155）。

背景：``config show/set`` 曾只碰 launcher 三键却装作全局配置——
采集参数在 conf/prometheus.conf.json 够不着、无键校验、无生效时机
提示。现为一键一主路由：launcher → 守护单写者/宿主原子写；
scraper/viewer → 各自 conf 原子合并（scraper 运行中顺带同步 live
回显，行为仍须 restart）；show 分组呈现 + 凭证掩码 + live 漂移。
"""

from __future__ import annotations

import json

from src.launcher.commands import CommandParser, Dispatcher

P_SKEY = "1788123499abcdefwxyz"


class _FakePm:
    def __init__(self, states):
        self._states = states

    def status_of(self, name):
        return {"state": self._states.get(name, "stopped")}


class _FakeRemote:
    def __init__(self):
        self.set_calls = []

    def config_get(self):
        return {"launcher_port": 9421}

    def config_set(self, key, value):
        self.set_calls.append((key, value))
        return {"launcher_port": 9421, key: value}


def _make_tree(tmp_path):
    root = tmp_path / "conf"
    root.mkdir()
    (root / "launcher.conf.json").write_text(
        json.dumps({"launcher_port": 9421, "max_restarts": 5}), encoding="utf-8")
    (root / "prometheus.conf.json").write_text(json.dumps({
        "_comment": "keep me",
        "scraper_max_workers": 10,
        "scraper_api_port": 9420,
        "guild_number": "Takagi3channel",
    }, ensure_ascii=False), encoding="utf-8")
    (root / "viewer.conf.json").write_text(
        json.dumps({"db_path": "db/viewer.db", "page_size": 20}),
        encoding="utf-8")
    (root / "guilds.conf.json").write_text(json.dumps({"guilds": [
        {"guild_id": "7743321643036658", "guild_number": "Takagi3channel",
         "name": "擅长捉弄的高木同学"}]}, ensure_ascii=False), encoding="utf-8")
    (root / "deepbackfill.conf.json").write_text(json.dumps(
        {"uin": 3557670458, "p_skey": P_SKEY, "minted_at": 1788404094138}),
        encoding="utf-8")
    return root


def _dispatcher(root, states=None):
    return Dispatcher(
        pm=_FakePm(states or {}),
        config=json.loads(
            (root / "launcher.conf.json").read_text(encoding="utf-8")),
        config_path=str(root / "launcher.conf.json"),
        scraper_conf_path=str(root / "prometheus.conf.json"),
        viewer_conf_path=str(root / "viewer.conf.json"),
        guilds_conf_path=str(root / "guilds.conf.json"),
        deepbackfill_conf_path=str(root / "deepbackfill.conf.json"),
    )


def _set(d, key, value):
    return d.dispatch(CommandParser().parse(
        "config set {0} {1}".format(key, value)))


def test_config_routing_unknown_key_lists_domains(tmp_path):
    d = _dispatcher(_make_tree(tmp_path))

    res = _set(d, "bogus_key", "1")

    assert res["ok"] is False
    assert "Unknown config key: 'bogus_key'" in res["message"]
    for domain, sample in (("launcher", "max_restarts"),
                           ("scraper", "scraper_max_workers"),
                           ("viewer", "page_size")):
        assert domain in res["message"]
        assert sample in res["message"]


def test_config_set_launcher_host_writes_atomically(tmp_path):
    root = _make_tree(tmp_path)
    d = _dispatcher(root)

    res = _set(d, "max_restarts", "8")

    assert res["ok"] is True
    assert "launcher: max_restarts = 8  [live]" in res["message"]
    on_disk = json.loads(
        (root / "launcher.conf.json").read_text(encoding="utf-8"))
    assert on_disk["max_restarts"] == 8
    assert on_disk["launcher_port"] == 9421


def test_config_set_launcher_remote_uses_daemon_writer(tmp_path):
    root = _make_tree(tmp_path)
    remote = _FakeRemote()
    d = _dispatcher(root)
    d.remote = remote

    res = _set(d, "viewer_port", "9432")

    assert res["ok"] is True
    assert remote.set_calls == [("viewer_port", 9432)]
    assert "launcher: viewer_port = 9432  [next viewer start]" in (
        res["message"])
    disk = json.loads(
        (root / "launcher.conf.json").read_text(encoding="utf-8"))
    assert "viewer_port" not in disk


def test_config_set_scraper_merges_disk_and_live_syncs_when_running(
        tmp_path, monkeypatch):
    root = _make_tree(tmp_path)
    d = _dispatcher(root, states={"scraper": "running"})
    synced = []
    monkeypatch.setattr(d, "_live_put_scraper",
                        lambda key, value: synced.append((key, value)) or True)

    res = _set(d, "scraper_max_workers", "16")

    assert res["ok"] is True
    assert synced == [("scraper_max_workers", 16)]
    assert "scraper: scraper_max_workers = 16" in res["message"]
    assert "[restart scraper]" in res["message"]
    assert "live echo synced" in res["message"]
    disk = json.loads(
        (root / "prometheus.conf.json").read_text(encoding="utf-8"))
    assert disk["scraper_max_workers"] == 16
    assert disk["_comment"] == "keep me"
    assert disk["scraper_api_port"] == 9420


def test_config_set_scraper_stopped_skips_live_sync(tmp_path, monkeypatch):
    root = _make_tree(tmp_path)
    d = _dispatcher(root, states={"scraper": "stopped"})
    monkeypatch.setattr(
        d, "_live_put_scraper",
        lambda *a: (_ for _ in ()).throw(AssertionError("stopped 不该同步")))

    res = _set(d, "scraper_daemon_interval_sec", "90")

    assert res["ok"] is True
    assert "live" not in res["message"].split("→")[1]
    disk = json.loads(
        (root / "prometheus.conf.json").read_text(encoding="utf-8"))
    assert disk["scraper_daemon_interval_sec"] == 90


def test_config_set_viewer_writes_viewer_conf(tmp_path):
    root = _make_tree(tmp_path)
    d = _dispatcher(root)

    res = _set(d, "page_size", "50")

    assert res["ok"] is True
    assert "viewer: page_size = 50" in res["message"]
    disk = json.loads(
        (root / "viewer.conf.json").read_text(encoding="utf-8"))
    assert disk == {"db_path": "db/viewer.db", "page_size": 50}


def test_config_show_groups_masks_and_marks_defaults(tmp_path):
    root = _make_tree(tmp_path)
    d = _dispatcher(root)

    res = d.dispatch(CommandParser().parse("config show"))

    msg = res["message"]
    assert "launcher (" in msg
    assert "viewer_port" in msg and "(default)" in msg
    assert "scraper (" in msg
    assert "guild_number" in msg and '"Takagi3channel"' in msg
    assert "viewer (" in msg and "page_size" in msg and "20" in msg
    assert "guilds (" in msg and "7743321643036658  Takagi3channel" in msg
    assert "deepbackfill credentials" in msg
    assert P_SKEY not in msg
    assert "{0}****{1}".format(P_SKEY[:4], P_SKEY[-4:]) in msg
    assert "scraper stopped — live view unavailable" in msg


def test_config_show_reports_live_drift(tmp_path, monkeypatch):
    root = _make_tree(tmp_path)
    d = _dispatcher(root, states={"scraper": "running"})
    monkeypatch.setattr(
        d, "_scraper_live_config",
        lambda timeout=3.0: {"scraper_max_workers": 32,
                             "scraper_api_port": 9420})

    res = d.dispatch(CommandParser().parse("config show"))

    assert "live drift — scraper_max_workers: disk 10 → live 32" in (
        res["message"])
