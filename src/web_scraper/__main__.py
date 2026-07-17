"""Entry point for the web scraper module.

Assembles all components (client, store, scrapers, API server, daemon)
from configuration and runs either a single scan (--once) or continuous
daemon mode (default).

Usage:
    python -m src.web_scraper              # daemon mode
    python -m src.web_scraper --once       # single scan, then exit
    python -m src.web_scraper --help       # show usage
"""
import argparse
import ctypes
import logging
import logging.handlers
import os
import signal
import sys
import threading
from pathlib import Path

from src.web_scraper.client import QQWebClient
from src.web_scraper.config import Config
from src.web_scraper.store import Store
from src.web_scraper.feeds import FeedsScraper
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.media import MediaDownloader
from src.web_scraper.api_server import APIServer
from src.web_scraper.daemon import Daemon
from src.web_scraper.lock import Lock, LockError

_PR_SET_PDEATHSIG = 1
_LOG_BUFFER_MAX = 500


class _BufferLogHandler(logging.Handler):
    """Feeds /logs endpoint — entries must have seq/level/msg/ts keys."""

    def __init__(self, buffer: list, max_lines: int = _LOG_BUFFER_MAX):
        super().__init__(level=logging.INFO)
        self._buffer = buffer
        self._max = max_lines
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "level": record.levelname,
                "msg": record.getMessage(),
                "ts": logging.Formatter.formatTime(
                    logging.Formatter(), record
                ),
            }
            self._buffer.append(entry)
            if len(self._buffer) > self._max:
                del self._buffer[: len(self._buffer) - self._max]


def _set_pdeathsig():
    """Set PR_SET_PDEATHSIG so we die when parent dies."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass  # non-Linux or no libc


def _setup_logging(log_dir: Path):
    """Configure logging to file + console."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "prometheus.log"
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fmt = logging.Formatter("[%(levelname)s] %(asctime)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)


def _build_components(config: Config):
    """Create and wire all components. Returns (daemon, api_server, stats)."""
    client = QQWebClient(
        config.channel_id, config.guild_number, config.scraper_max_workers
    )
    store = Store(config.data_dir)
    feeds_scraper = FeedsScraper(client, store, config.channel_id)
    comments_scraper = CommentsScraper(
        client, store, config.guild_number, config.scraper_max_workers
    )
    media_downloader = MediaDownloader(config.data_dir, config.scraper_max_workers)

    stats = {
        "scanned_feeds": 0,
        "feeds_count": len(store._feed_ids),
        "comments_count": len(store._comment_keys),
        "media_count": len(media_downloader._seen),
        "last_scan_ts": 0,
        "daemon_running": False,
        "log_buffer": [],
        "config": {
            "apiVersion": "1",
            "channel_id": config.channel_id,
            "guild_number": config.guild_number,
            "scraper_max_workers": config.scraper_max_workers,
            "scraper_daemon_interval_sec": config.scraper_daemon_interval_sec,
            "scraper_api_port": config.scraper_api_port,
        },
    }

    daemon = Daemon(
        feeds_scraper,
        comments_scraper,
        media_downloader,
        store,
        interval_sec=config.scraper_daemon_interval_sec,
        stats=stats,
    )

    api_server = APIServer(store, stats, port=config.scraper_api_port)
    api_server.set_trigger_callback(daemon.run_once)

    return daemon, api_server, stats


def main():
    parser = argparse.ArgumentParser(description="Prometheus web scraper")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit")
    parser.add_argument(
        "--daemon", action="store_true", default=True, help="Run in daemon mode (default)"
    )
    args = parser.parse_args()

    _set_pdeathsig()

    config = Config.load()

    _project_root = Path(__file__).resolve().parent.parent.parent
    _setup_logging(_project_root / "log" / "prometheus")

    logger = logging.getLogger(__name__)
    logger.info("Starting web scraper (channel=%s)", config.channel_id)

    daemon, api_server, stats = _build_components(config)

    buffer_handler = _BufferLogHandler(stats["log_buffer"])
    logging.getLogger().addHandler(buffer_handler)

    lock = Lock(config.data_dir, logger=logger)
    recovery = lock.check_and_recover()
    if recovery:
        logger.warning(
            "Crash recovery: dirty lock from cycle %s (ts=%s)",
            recovery.get("cycle"),
            recovery.get("ts"),
        )
    try:
        lock.acquire(cycle=0)
    except LockError as e:
        logger.error("Lock acquire failed: %s", e)
        sys.exit(1)
    logger.info("Lock acquired (pid=%d)", os.getpid())

    if args.once:
        logger.info("Running single scan (--once mode)")
        daemon.run_once()
        logger.info(
            "Single scan complete: %d feeds, %d comments",
            stats["feeds_count"],
            stats["comments_count"],
        )
        lock.release()
        return

    api_server.start()
    logger.info("API server listening on port %d", api_server.port)

    logger.info("Starting daemon (interval=%ss)", config.scraper_daemon_interval_sec)
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")
    finally:
        api_server.stop()
        lock.release()
        logger.info("Web scraper stopped")


if __name__ == "__main__":
    main()