"""Configuration and constants for the web scraper module.

Reads from the same conf/prometheus.conf.json as the legacy scraper,
adding web-scraper-specific keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# API endpoints (base URL for pd.qq.com public web APIs)
API_BASE_URL = "https://pd.qq.com/qunng/guild/gotrpc/noauth/trpc.qchannel.commreader.ComReader/"
GUILD_PAGE_URL = "https://pd.qq.com/g/{guild_id}"

# API paths
API_GET_FEEDS = "GetGuildFeeds"
API_GET_FEED_DETAIL = "GetFeedDetail"
API_GET_COMMENTS = "GetFeedComments"

# Service types per API (empirically verified)
SERVICE_TYPE_FEEDS = 12
SERVICE_TYPE_DETAIL = 5
SERVICE_TYPE_COMMENTS = 5

# Headers
CLIENT_APPID = "537246381"

# Defaults
DEFAULT_MAX_WORKERS = 10
DEFAULT_DAEMON_INTERVAL_SEC = 120
DEFAULT_API_PORT = 9420


class Config:
    """Parsed configuration for the web scraper."""

    def __init__(self, raw: dict):
        self.channel_id = raw.get("channel_id", "")
        self.channel_name = raw.get("channel_name", "")
        self.guild_number = raw.get("guild_number", "")
        self.scraper_max_workers = raw.get("scraper_max_workers", DEFAULT_MAX_WORKERS)
        self.scraper_daemon_interval_sec = raw.get(
            "scraper_daemon_interval_sec", DEFAULT_DAEMON_INTERVAL_SEC
        )
        self.scraper_api_port = raw.get("scraper_api_port", DEFAULT_API_PORT)
        # data_dir: use config value, expand ~, or default to <project>/data
        dd = raw.get("data_dir")
        if dd:
            self.data_dir = Path(dd).expanduser()
            if not self.data_dir.is_absolute():
                self.data_dir = _PROJECT_ROOT / self.data_dir
        else:
            self.data_dir = _PROJECT_ROOT / "data"

    @classmethod
    def load(cls) -> "Config":
        """Load config from PROMETHEUS_CONFIG env or default path."""
        config_path = os.environ.get("PROMETHEUS_CONFIG")
        if config_path:
            p = Path(config_path)
        else:
            # Try conf/prometheus.conf.json, then project root
            p = _PROJECT_ROOT / "conf" / "prometheus.conf.json"
            if not p.exists():
                p = _PROJECT_ROOT / "prometheus.conf.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        # Strip _comment keys
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        return cls(raw)