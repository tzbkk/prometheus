import json
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.config import (
    API_BASE_URL,
    CLIENT_APPID,
    SERVICE_TYPE_COMMENTS,
    SERVICE_TYPE_FEEDS,
    Config,
    Guild,
)


@contextmanager
def _override_config(prometheus_conf: dict, guilds_conf: dict | None = None):
    """Write an isolated prometheus.conf.json (+ optional sibling guilds.conf.json)
    into a temp dir, point PROMETHEUS_CONFIG at it, and restore env on exit."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "prometheus.conf.json"
        cfg_path.write_text(json.dumps(prometheus_conf), encoding="utf-8")
        if guilds_conf is not None:
            (Path(d) / "guilds.conf.json").write_text(
                json.dumps(guilds_conf), encoding="utf-8"
            )
        old = os.environ.get("PROMETHEUS_CONFIG")
        os.environ["PROMETHEUS_CONFIG"] = str(cfg_path)
        try:
            yield cfg_path
        finally:
            if old is not None:
                os.environ["PROMETHEUS_CONFIG"] = old
            else:
                os.environ.pop("PROMETHEUS_CONFIG", None)


# -------------------- existing tests (preserved) --------------------


def test_config_loads_from_real_file():
    """Config.load() succeeds with the real config file."""
    cfg = Config.load()
    assert cfg.channel_id == "7743321643036658"


def test_config_has_guild_number():
    """Config has guild_number from the config file."""
    cfg = Config.load()
    assert cfg.guild_number == "Takagi3channel"


def test_config_scraper_defaults():
    """Scraper-specific keys have correct values."""
    cfg = Config.load()
    assert cfg.scraper_max_workers == 10
    assert cfg.scraper_daemon_interval_sec == 120
    assert cfg.scraper_api_port == 9420


def test_config_data_dir_default():
    """data_dir defaults to _PROJECT_ROOT/data when null."""
    cfg = Config.load()
    expected = Path(_PROJECT_ROOT) / "data"
    assert cfg.data_dir == expected


def test_config_data_dir_explicit():
    """data_dir resolves correctly when set."""
    with _override_config(
        prometheus_conf={
            "channel_id": "12345",
            "channel_name": "test",
            "guild_number": "testguild",
            "data_dir": "/tmp/test_prometheus_data",
        },
        guilds_conf={
            "guilds": [
                {"guild_id": "12345", "guild_number": "testguild", "name": "test"}
            ]
        },
    ):
        cfg = Config.load()
        assert cfg.data_dir == Path("/tmp/test_prometheus_data")


def test_constants_exist():
    """Constants have correct values."""
    assert API_BASE_URL == "https://pd.qq.com/qunng/guild/gotrpc/noauth/trpc.qchannel.commreader.ComReader/"
    assert SERVICE_TYPE_FEEDS == 12
    assert SERVICE_TYPE_COMMENTS == 5
    assert CLIENT_APPID == "537246381"


def test_config_env_override():
    """PROMETHEUS_CONFIG env var points to a different config file."""
    with _override_config(
        prometheus_conf={
            "channel_id": "99999",
            "channel_name": "env_test",
            "guild_number": "env_guild",
        },
        guilds_conf={
            "guilds": [
                {"guild_id": "99999", "guild_number": "env_guild", "name": "env_test"}
            ]
        },
    ):
        cfg = Config.load()
        assert cfg.channel_id == "99999"
        assert cfg.channel_name == "env_test"
        assert cfg.guild_number == "env_guild"


# -------------------- new multi-guild tests --------------------


def test_config_loads_guilds_from_guilds_conf():
    """When guilds.conf.json is present, cfg.guilds is populated from it."""
    with _override_config(
        prometheus_conf={"channel_id": "99999", "guild_number": "env_guild"},
        guilds_conf={
            "guilds": [
                {"guild_id": "111", "guild_number": "alpha", "name": "Alpha Guild"},
                {"guild_id": "222", "guild_number": "beta", "name": "Beta Guild"},
            ]
        },
    ):
        cfg = Config.load()
        assert len(cfg.guilds) == 2
        assert isinstance(cfg.guilds[0], Guild)
        assert cfg.guilds[0].guild_id == "111"
        assert cfg.guilds[0].guild_number == "alpha"
        assert cfg.guilds[0].name == "Alpha Guild"
        assert cfg.guilds[1].guild_id == "222"
        assert cfg.guilds[1].guild_number == "beta"


def test_config_guilds_legacy_fallback(monkeypatch):
    """When guilds.conf.json is absent, exactly one Guild is built from legacy fields."""
    # Isolate from G9's project-level conf/guilds.conf.json fallback so the
    # legacy branch actually fires.
    import src.web_scraper.config as cfg_mod

    with tempfile.TemporaryDirectory() as isolated_root:
        monkeypatch.setattr(cfg_mod, "_PROJECT_ROOT", Path(isolated_root))
        with _override_config(
            prometheus_conf={
                "channel_id": "777",
                "channel_name": "legacy name",
                "guild_number": "legacyslug",
            },
            guilds_conf=None,
        ):
            cfg = Config.load()
            assert len(cfg.guilds) == 1
            g = cfg.guilds[0]
            assert g.guild_id == "777"
            assert g.guild_number == "legacyslug"
            assert g.name == "legacy name"


def test_config_guilds_dedup():
    """Two entries with the same guild_id are deduped to one (first wins)."""
    with _override_config(
        prometheus_conf={},
        guilds_conf={
            "guilds": [
                {"guild_id": "555", "guild_number": "first", "name": "First"},
                {"guild_id": "555", "guild_number": "second", "name": "Second"},
            ]
        },
    ):
        cfg = Config.load()
        assert len(cfg.guilds) == 1
        assert cfg.guilds[0].guild_number == "first"
        assert cfg.guilds[0].name == "First"


def test_config_guilds_reject_missing_fields(caplog):
    """Entries missing guild_id or guild_number are dropped with a warning."""
    with _override_config(
        prometheus_conf={},
        guilds_conf={
            "guilds": [
                {"guild_number": "nonumber", "name": "No ID"},
                {"guild_id": "888", "name": "No Slug"},
                {"guild_id": "999", "guild_number": "ok", "name": "OK"},
            ]
        },
    ):
        with caplog.at_level(logging.WARNING):
            cfg = Config.load()
        assert len(cfg.guilds) == 1
        assert cfg.guilds[0].guild_id == "999"
        messages = " ".join(r.message for r in caplog.records)
        assert "empty guild_id" in messages
        assert "empty guild_number" in messages


def test_config_guilds_reject_non_numeric_id(caplog):
    """Entries with non-numeric or empty guild_id are dropped."""
    with _override_config(
        prometheus_conf={},
        guilds_conf={
            "guilds": [
                {"guild_id": "abc", "guild_number": "s1", "name": "Alpha ID"},
                {"guild_id": "456", "guild_number": "ok", "name": "OK"},
            ]
        },
    ):
        with caplog.at_level(logging.WARNING):
            cfg = Config.load()
        assert len(cfg.guilds) == 1
        assert cfg.guilds[0].guild_id == "456"
        messages = " ".join(r.message for r in caplog.records)
        assert "non-numeric" in messages


def test_config_legacy_fields_from_first_guild():
    """Legacy channel_id/channel_name/guild_number mirror the first guild."""
    with _override_config(
        prometheus_conf={"channel_id": "99999", "guild_number": "env_guild"},
        guilds_conf={
            "guilds": [
                {"guild_id": "111", "guild_number": "alpha", "name": "Alpha"},
                {"guild_id": "222", "guild_number": "beta", "name": "Beta"},
            ]
        },
    ):
        cfg = Config.load()
        assert cfg.channel_id == cfg.guilds[0].guild_id == "111"
        assert cfg.guild_number == cfg.guilds[0].guild_number == "alpha"
        assert cfg.channel_name == cfg.guilds[0].name == "Alpha"


def test_config_legacy_fallback_empty_channel_id_yields_no_guilds(monkeypatch):
    """Legacy fallback with empty channel_id produces no guilds (raw values kept)."""
    import src.web_scraper.config as cfg_mod

    with tempfile.TemporaryDirectory() as isolated_root:
        monkeypatch.setattr(cfg_mod, "_PROJECT_ROOT", Path(isolated_root))
        with _override_config(
            prometheus_conf={"channel_id": "", "channel_name": "", "guild_number": ""},
            guilds_conf=None,
        ):
            cfg = Config.load()
            assert cfg.guilds == []
            assert cfg.channel_id == ""
            assert cfg.guild_number == ""
            assert cfg.channel_name == ""
