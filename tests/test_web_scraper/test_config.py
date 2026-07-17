import os
import sys
import json
import tempfile

import pytest

from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.config import (
    Config,
    API_BASE_URL,
    SERVICE_TYPE_FEEDS,
    SERVICE_TYPE_COMMENTS,
    CLIENT_APPID,
)


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
    config_content = {
        "channel_id": "12345",
        "channel_name": "test",
        "data_dir": "/tmp/test_prometheus_data",
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(config_content, f)
        f.flush()
        temp_path = f.name

    try:
        old_env = os.environ.get("PROMETHEUS_CONFIG")
        os.environ["PROMETHEUS_CONFIG"] = temp_path
        cfg = Config.load()
        if old_env is not None:
            os.environ["PROMETHEUS_CONFIG"] = old_env
        else:
            os.environ.pop("PROMETHEUS_CONFIG")

        assert cfg.data_dir == Path("/tmp/test_prometheus_data")
    finally:
        os.unlink(temp_path)


def test_constants_exist():
    """Constants have correct values."""
    assert API_BASE_URL == "https://pd.qq.com/qunng/guild/gotrpc/noauth/trpc.qchannel.commreader.ComReader/"
    assert SERVICE_TYPE_FEEDS == 12
    assert SERVICE_TYPE_COMMENTS == 5
    assert CLIENT_APPID == "537246381"


def test_config_env_override():
    """PROMETHEUS_CONFIG env var points to a different config file."""
    config_content = {
        "channel_id": "99999",
        "channel_name": "env_test",
        "guild_number": "env_guild",
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(config_content, f)
        f.flush()
        temp_path = f.name

    try:
        old_env = os.environ.get("PROMETHEUS_CONFIG")
        os.environ["PROMETHEUS_CONFIG"] = temp_path
        cfg = Config.load()
        if old_env is not None:
            os.environ["PROMETHEUS_CONFIG"] = old_env
        else:
            os.environ.pop("PROMETHEUS_CONFIG")

        assert cfg.channel_id == "99999"
        assert cfg.channel_name == "env_test"
        assert cfg.guild_number == "env_guild"
    finally:
        os.unlink(temp_path)