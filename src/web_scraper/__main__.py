"""Entry point for the web scraper module.

Assembles per-guild entity contexts（scan 启动派生 → EntityStore 种子），
runs either a single scan (--once) or continuous daemon mode (default).

Usage:
    python -m src.web_scraper              # daemon mode
    python -m src.web_scraper --once       # single scan, then exit
    python -m src.web_scraper --port 9430  # port 覆盖（统一惯例）
"""

import argparse
import ctypes
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.entity_store import is_stale, lock_path, read_lock, scan, write_lock
from src.entity_store.lock import ProcessLockData
from src.web_scraper.api_server import ScraperService
from src.web_scraper.client import QQWebClient
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.config import Config, Guild
from src.web_scraper.daemon import Daemon
from src.web_scraper.feeds import FeedsScraper
from src.web_scraper.media import MediaDownloadPool, MediaDownloader
from src.web_scraper.store import EntityStore

_PR_SET_PDEATHSIG = 1
_LOG_BUFFER_MAX = 500
_MEDIA_DRAIN_TIMEOUT_SEC = 120  # SIGTERM 停机 drain 上限；--once 全量 drain（None）


@dataclass
class GuildContext:
    """Per-guild components for one scan cycle (+ bottom_reached).

    Per-guild recheck state (cursor / batch / workers) lives HERE,
    not on the Daemon — otherwise N guilds would interleave a single
    cursor across different feed-id arrays and break the round-robin.
    """

    guild: Guild
    client: "QQWebClient"
    store: "EntityStore"
    feeds_scraper: "FeedsScraper"
    comments_scraper: "CommentsScraper"
    media_downloader: "MediaDownloader"
    bottom_reached: bool = False
    _recheck_cursor: int = 0
    _recheck_batch_size: int = 150
    _recheck_workers: int = 8


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


def _acquire_process_lock(data_dir: Path, logger) -> bool:
    """entity_store.lock 启动获取：stale 覆写；活锁冲突 exit 2."""
    path = lock_path(data_dir)
    existing = read_lock(path)
    if existing is not None and existing.dirty and not is_stale(existing):
        logger.error("Lock acquire failed: another instance running (pid=%d)", existing.pid)
        return False
    if existing is not None and is_stale(existing):
        logger.warning("Crash recovery: stale lock from dead pid=%s", existing.pid)
    write_lock(
        path,
        ProcessLockData(
            pid=os.getpid(), dirty=True, cycle=0, ts=int(time.time()), bottomReached=False
        ),
    )
    return True


def _build_components(config: Config, api_port=None):
    """Create and wire all per-guild components; returns
    ``(daemon, service, stats, media_pool)`` or ``(None, None, None, None)``
    when zero guilds survive（failing guilds are skipped; abort only
    when none survive）.

    ONE bounded MediaDownloadPool shared across ALL guild contexts
    （max_workers = scraper_max_workers）——attempt_entity_media 变投递，
    列表观测零阻塞；启动扫描发现的存量 pending/failed 媒体即入池。
    """
    logger = logging.getLogger(__name__)
    guild_contexts: list[GuildContext] = []

    # ONE global rate-limiting Semaphore shared across ALL
    # guild contexts — QQ rate limits are per-IP/per-appid. Held only around
    # API/HTTP calls, never during entity writes.
    rate_semaphore = threading.Semaphore(config.scraper_max_workers)

    media_pool = MediaDownloadPool(max_workers=config.scraper_max_workers)

    for guild in config.guilds:
        try:
            client = QQWebClient(guild.guild_id, guild.guild_number, config.scraper_max_workers)
            scan_result = scan(config.data_dir, guild.guild_id)
            if scan_result.skipped:
                logger.warning(
                    "startup scan for guild %s skipped %d malformed file(s)",
                    guild.guild_id,
                    scan_result.skipped,
                )
            store = EntityStore(
                config.data_dir,
                guild.guild_id,
                dead_urls=scan_result.dead_urls,
            )
            store.seed_comment_counts(
                scan_result.comment_counts, scan_result.reply_counts
            )
            media_downloader = MediaDownloader(
                store, semaphore=rate_semaphore, pool=media_pool
            )
            backlog = media_pool.enqueue_all(
                media_downloader, scan_result.pending_media
            )
            if backlog:
                logger.info(
                    "media pool: %d backlog entit(ies) with pending/failed "
                    "media enqueued from startup scan (guild %s)",
                    backlog,
                    guild.guild_id,
                )
            feeds_scraper = FeedsScraper(client, store, guild.guild_id)
            comments_scraper = CommentsScraper(
                client,
                store,
                config.scraper_max_workers,
                shared_semaphore=rate_semaphore,
                media_downloader=media_downloader,
            )
            guild_contexts.append(
                GuildContext(
                    guild=guild,
                    client=client,
                    store=store,
                    feeds_scraper=feeds_scraper,
                    comments_scraper=comments_scraper,
                    media_downloader=media_downloader,
                )
            )
            logger.info(
                "Built context for guild %s (%s): scan found %d feed(s), "
                "%d dead url(s), %d comment-bearing feed(s)",
                guild.guild_id,
                guild.name,
                scan_result.feeds,
                len(scan_result.dead_urls),
                len(scan_result.comment_counts),
            )
        except Exception:
            logger.exception(
                "Failed to build context for guild %s — skipping", guild.guild_id
            )

    if not guild_contexts:
        logger.error("No guild contexts built — all guilds failed")
        media_pool.shutdown(timeout=_MEDIA_DRAIN_TIMEOUT_SEC)
        return None, None, None, None

    stats = {
        "scanned_feeds": 0,
        "feeds": 0,
        "comments": 0,
        "replies": 0,
        "media": 0,
        "last_scan_ts": 0,
        "daemon_running": False,
        "log_buffer": [],
        "guilds": {},
    }

    effective_port = api_port if api_port is not None else config.scraper_api_port
    config_state = {
        "apiVersion": "2",
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
        "scraper_api_port": effective_port,
    }

    daemon = Daemon(
        guild_contexts,
        interval_sec=config.scraper_daemon_interval_sec,
        stats=stats,
        lock_path=lock_path(config.data_dir),
    )

    service = ScraperService(
        stats=stats,
        config_state=config_state,
        trigger_callback=daemon.run_once_guarded,
        port=effective_port,
    )

    return daemon, service, stats, media_pool


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prometheus web scraper")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit")
    parser.add_argument(
        "--daemon", action="store_true", default=True, help="Run in daemon mode (default)"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override scraper_api_port from conf/prometheus.conf.json",
    )
    args = parser.parse_args(argv)

    _set_pdeathsig()

    config = Config.load()

    _project_root = Path(__file__).resolve().parent.parent.parent
    _setup_logging(_project_root / "log" / "prometheus")

    logger = logging.getLogger(__name__)
    logger.info("Starting web scraper (entity tree)")

    # empty-guilds exit — nothing to scrape, no point continuing.
    if not config.guilds:
        logger.error(
            "No guilds configured — check conf/guilds.conf.json or "
            "channel_id in prometheus.conf.json"
        )
        sys.exit(1)

    daemon, service, stats, media_pool = _build_components(config, api_port=args.port)

    # zero surviving guilds — even though config.guilds was non-empty,
    # every QQWebClient construction may have failed (network/geo-block).
    if daemon is None:
        logger.error("No guild contexts could be built — exiting")
        sys.exit(1)
    assert service is not None and stats is not None

    buffer_handler = _BufferLogHandler(stats["log_buffer"])
    logging.getLogger().addHandler(buffer_handler)

    if not _acquire_process_lock(config.data_dir, logger):
        media_pool.shutdown(timeout=_MEDIA_DRAIN_TIMEOUT_SEC)
        sys.exit(2)  # exit code 2 = lock conflict, launcher should not auto-restart
    logger.info("Lock acquired (pid=%d)", os.getpid())

    if args.once:
        logger.info("Running single scan (--once mode)")
        daemon.run_once()
        logger.info(
            "Single scan complete: %d feeds, %d comments, %d replies, %d media",
            stats["feeds"],
            stats["comments"],
            stats["replies"],
            stats["media"],
        )
        done, cancelled, running = media_pool.shutdown()  # 全量 drain（None）
        logger.info(
            "Media pool drained: %d done, %d cancelled, %d still running",
            done,
            cancelled,
            running,
        )
        _release_lock_cleanly(config.data_dir)
        return

    service.start()
    logger.info("API server listening on port %d", service.port)

    logger.info("Starting daemon (interval=%ss)", config.scraper_daemon_interval_sec)
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")
    finally:
        service.stop()
        daemon.wait_idle(timeout=60)  # 半途 trigger 周期投递全落池后再关门
        done, cancelled, running = media_pool.shutdown(
            timeout=_MEDIA_DRAIN_TIMEOUT_SEC
        )
        logger.info(
            "Media pool drained: %d done, %d cancelled, %d still running",
            done,
            cancelled,
            running,
        )
        _release_lock_cleanly(config.data_dir)
        logger.info("Web scraper stopped")


def _release_lock_cleanly(data_dir: Path):
    write_lock(
        lock_path(data_dir),
        ProcessLockData(
            pid=os.getpid(), dirty=False, cycle=0, ts=int(time.time()), bottomReached=False
        ),
    )


if __name__ == "__main__":
    main()
