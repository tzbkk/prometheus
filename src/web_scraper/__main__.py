"""Entry point for the web scraper module.

Assembles all components (client, store, scrapers, API server, daemon)
from configuration and runs either a single scan (--once) or continuous
daemon mode (default).

Multi-guild (plan §2): one :class:`GuildContext` per configured guild is
built in :func:`_build_components` and handed to a single
:class:`~src.web_scraper.daemon.Daemon` which scans them sequentially
per cycle.

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
from dataclasses import dataclass
from pathlib import Path

from src.web_scraper.client import QQWebClient
from src.web_scraper.config import Config, Guild
from src.web_scraper.store import Store
from src.web_scraper.feeds import FeedsScraper
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.media import MediaDownloader
from src.web_scraper.api_server import APIServer
from src.web_scraper.daemon import Daemon
from src.web_scraper.lock import Lock, LockError
from scripts.migrate_multi_guild import migrate

_PR_SET_PDEATHSIG = 1
_LOG_BUFFER_MAX = 500


@dataclass
class GuildContext:
    """All per-guild components needed for one scan cycle.

    G3: per-guild recheck state (cursor / batch / workers) lives HERE,
    not on the Daemon — otherwise N guilds would interleave a single
    cursor across different feed-id arrays and break the round-robin.
    """

    guild: Guild
    client: "QQWebClient"
    store: "Store"
    feeds_scraper: "FeedsScraper"
    comments_scraper: "CommentsScraper"
    media_downloader: "MediaDownloader"
    _recheck_cursor: int = 0
    _recheck_batch_size: int = 50
    _recheck_workers: int = 3


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
    """Create and wire all per-guild components.

    Returns ``(daemon, api_server, stats)`` or ``(None, None, None)`` if
    not a single guild could be constructed (B3: resilient — failing
    guilds are skipped, we only abort when zero survive).
    """
    logger = logging.getLogger(__name__)
    guild_contexts: list[GuildContext] = []

    # I1 (plan §2.1a): ONE global rate-limiting Semaphore shared across ALL
    # guild contexts. QQ rate limits are per-IP/per-appid, NOT per-session —
    # N guilds × max_workers threads each would multiply aggregate concurrency
    # by N and trigger SSL handshake timeouts. The semaphore value is the
    # SAME as the single-guild budget (NOT N×) so global concurrency matches
    # the original single-guild behaviour. Held only around API/HTTP calls,
    # never during bookkeeping.
    rate_semaphore = threading.Semaphore(config.scraper_max_workers)

    for guild in config.guilds:
        try:
            client = QQWebClient(
                guild.guild_id, guild.guild_number, config.scraper_max_workers
            )
            store = Store(config.data_dir / guild.guild_id)
            feeds_scraper = FeedsScraper(client, store, guild.guild_id)
            comments_scraper = CommentsScraper(
                client,
                store,
                guild.guild_number,
                config.scraper_max_workers,
                shared_semaphore=rate_semaphore,
            )
            media_downloader = MediaDownloader(
                config.data_dir / guild.guild_id,
                config.scraper_max_workers,
                shared_semaphore=rate_semaphore,
            )
            ctx = GuildContext(
                guild=guild,
                client=client,
                store=store,
                feeds_scraper=feeds_scraper,
                comments_scraper=comments_scraper,
                media_downloader=media_downloader,
            )
            guild_contexts.append(ctx)
            logger.info(
                "Built context for guild %s (%s)", guild.guild_id, guild.name
            )
        except Exception:
            logger.exception(
                "Failed to build context for guild %s — skipping", guild.guild_id
            )

    if not guild_contexts:
        logger.error("No guild contexts built — all guilds failed")
        return None, None, None

    stats = {
        "scanned_feeds": 0,
        "feeds_count": sum(
            len(getattr(ctx.store, "_feed_ids", set())) for ctx in guild_contexts
        ),
        "comments_count": sum(
            len(getattr(ctx.store, "_comment_keys", set())) for ctx in guild_contexts
        ),
        "media_count": sum(
            len(getattr(ctx.media_downloader, "_seen", set())) for ctx in guild_contexts
        ),
        "last_scan_ts": 0,
        "daemon_running": False,
        "log_buffer": [],
        "guilds": {},
        "config": {
            "apiVersion": "2",
            "channel_id": config.channel_id,
            "guild_number": config.guild_number,
            "channel_name": config.channel_name,
            "guilds": [
                {
                    "guild_id": g.guild_id,
                    "guild_number": g.guild_number,
                    "name": g.name,
                }
                for g in config.guilds
            ],
            "scraper_max_workers": config.scraper_max_workers,
            "scraper_daemon_interval_sec": config.scraper_daemon_interval_sec,
            "scraper_api_port": config.scraper_api_port,
        },
    }

    daemon = Daemon(
        guild_contexts,
        interval_sec=config.scraper_daemon_interval_sec,
        stats=stats,
    )

    # G6 (drop store fallback) is P1c. Until then, store=None is safe: the
    # api_server._handle_stats fallback uses getattr(None, "_feed_ids", ())
    # which returns () when stats feeds_count != 0 (the multi-guild case).
    api_server = APIServer(None, stats, port=config.scraper_api_port)
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

    # G15: empty-guilds exit — nothing to scrape, no point continuing.
    if not config.guilds:
        logger.error(
            "No guilds configured — check conf/guilds.conf.json or legacy "
            "channel_id in prometheus.conf.json"
        )
        sys.exit(1)

    # B1: Auto-migrate flat data/ → data/<guild_id>/ for the first/legacy
    # guild. Idempotent — safe no-op if already migrated or no flat data.
    legacy_guild_id = config.guilds[0].guild_id
    flat_feeds = config.data_dir / "feeds.jsonl"
    guild_feeds = config.data_dir / legacy_guild_id / "feeds.jsonl"
    if flat_feeds.exists() and not guild_feeds.exists():
        logger.info("Auto-migrating flat data/ to data/%s/", legacy_guild_id)
        try:
            migrate(config.data_dir, legacy_guild_id)
        except Exception:
            logger.exception("Auto-migration failed — continuing anyway")

    daemon, api_server, stats = _build_components(config)

    # B3: zero surviving guilds — even though config.guilds was non-empty,
    # every QQWebClient construction may have failed (network/geo-block).
    if daemon is None:
        logger.error("No guild contexts could be built — exiting")
        sys.exit(1)
    assert api_server is not None and stats is not None

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