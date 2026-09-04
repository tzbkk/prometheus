"""deepbackfill 入口：``python -m src.deepbackfill``（契约 Runs 字段）。

组装（scraper __main__ 同形惯例——数据面五键全同）：

- conf 面：web_scraper Config.load()（guilds + data_dir，PROMETHEUS_CONFIG
  可覆写）+ CredentialStore（conf/deepbackfill.conf.json 四键，
  PROMETHEUS_DEEPBACKFILL_CONF 可覆写——纯网扫码登录写入）。
- 每 guild：noauth QQWebClient（评论面）→ scan 种子 → EntityStore →
  池化 MediaDownloader（共享 MediaDownloadPool）→ CommentsScraper →
  AuthClient（guild_number + CredentialManager——重铸缝在位）。
- 服务面：:9424 五端点（DeepbackfillService）+ 纯网登录三端点
  （/auth/qr.png 原图直出、/auth/status 状态机、/auth/page 内嵌 HTML）+
  AuthSessionManager（凭证探测懒启动：首个 /auth/status 查询/trigger
  活测触发；缺/失效自动起 QR 会话）+ trigger 起 BackfillRunner 后台线程。
- 进程锁：data_root/prometheus.lock（scraper 同款——活锁冲突 exit 2，
  launcher 不自愈重启；两服务同树不并存）。

停机序列（scraper 语义同化）：SIGTERM/SIGINT → service.stop() →
runner.join(60)（半途周期投递全落池）→ media_pool.shutdown(120s)
（drain 干净，同 scraper 停机序列）→ 释放锁。

Usage:
    python -m src.deepbackfill              # 默认 :9424
    python -m src.deepbackfill --port 9434  # port 覆盖（统一惯例）
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
from pathlib import Path
from types import SimpleNamespace

from src.entity_store import is_stale, lock_path, read_lock, scan, write_lock
from src.entity_store.lock import ProcessLockData
from src.web_scraper.client import QQWebClient
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.config import Config
from src.web_scraper.media import MediaDownloadPool, MediaDownloader
from src.web_scraper.store import EntityStore

from src.deepbackfill.auth import AuthClient
from src.deepbackfill.credentials import (
    CONF_ENV_VAR,
    CredentialManager,
    CredentialStore,
)
from src.deepbackfill.runner import BackfillRunner
from src.deepbackfill.service import AuthSessionManager, DeepbackfillService

_PR_SET_PDEATHSIG = 1
_LOG_BUFFER_MAX = 500
_MEDIA_DRAIN_TIMEOUT_SEC = 120
_RUNNER_JOIN_TIMEOUT_SEC = 60


class _BufferLogHandler(logging.Handler):
    """Feeds /logs endpoint——entries must have seq/level/msg/ts keys（scraper 同形）."""

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
                "ts": logging.Formatter.formatTime(logging.Formatter(), record),
            }
            self._buffer.append(entry)
            if len(self._buffer) > self._max:
                del self._buffer[: len(self._buffer) - self._max]


def _set_pdeathsig():
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def _setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "deepbackfill.log"
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


def _release_lock_cleanly(data_dir: Path):
    write_lock(
        lock_path(data_dir),
        ProcessLockData(
            pid=os.getpid(), dirty=False, cycle=0, ts=int(time.time()), bottomReached=False
        ),
    )


def _build_components(config: Config, port):
    """组装 runner + service；返回 (runner, service, stats, media_pool)。

    凭据缺失不阻止组装（AuthClient 凭据惰性取用），只记 warning：
    纯网路线下探测懒启动（首个 /auth/status 查询——launcher
    start 流或浏览器页轮询——即 boot 面；trigger 另有前置活测），
    缺/失效自动起 QR 会话（AuthSessionManager）。
    """
    logger = logging.getLogger(__name__)
    contexts = []
    rate_semaphore = threading.Semaphore(config.scraper_max_workers)
    media_pool = MediaDownloadPool(max_workers=config.scraper_max_workers)

    cred_store = CredentialStore(os.environ.get(CONF_ENV_VAR) or "conf/deepbackfill.conf.json")
    if cred_store.load() is None:
        logger.warning(
            "no credentials at %s — the first /auth/status poll (e.g. 'start "
            "deepbackfill' in the launcher shell) starts a QR login session",
            cred_store.path,
        )

    def _probe() -> str:
        # 每次现组 CredentialManager/AuthClient——QR 会话写盘后即刻可读
        # （进程内缓存不复用已写盘 conf）。
        manager = CredentialManager(cred_store)
        AuthClient(config.guilds[0].guild_number, manager).get_feeds("")
        return manager.credentials().uin

    for guild in config.guilds:
        try:
            noauth_client = QQWebClient(
                guild.guild_id, guild.guild_number, config.scraper_max_workers
            )
            scan_result = scan(config.data_dir, guild.guild_id)
            if scan_result.skipped:
                logger.warning(
                    "startup scan for guild %s skipped %d malformed file(s)",
                    guild.guild_id,
                    scan_result.skipped,
                )
            store = EntityStore(
                config.data_dir, guild.guild_id, dead_urls=scan_result.dead_urls
            )
            store.seed_comment_counts(scan_result.comment_counts, scan_result.reply_counts)
            media_downloader = MediaDownloader(
                store, semaphore=rate_semaphore, pool=media_pool
            )
            backlog = media_pool.enqueue_all(
                media_downloader, scan_result.pending_media
            )
            if backlog:
                logger.info(
                    "media pool: %d backlog entit(ies) enqueued from startup scan (guild %s)",
                    backlog,
                    guild.guild_id,
                )
            contexts.append(
                SimpleNamespace(
                    guild=SimpleNamespace(guild_id=guild.guild_id),
                    auth_client=AuthClient(
                        guild.guild_number,
                        CredentialManager(cred_store),
                        guild_id=guild.guild_id,
                    ),
                    store=store,
                    comments_scraper=CommentsScraper(
                        noauth_client,
                        store,
                        shared_semaphore=rate_semaphore,
                        media_downloader=media_downloader,
                    ),
                    media_downloader=media_downloader,
                )
            )
            logger.info(
                "Built context for guild %s (%s): scan found %d feed(s), %d dead url(s)",
                guild.guild_id,
                guild.name,
                scan_result.feeds,
                len(scan_result.dead_urls),
            )
        except Exception:
            logger.exception(
                "Failed to build context for guild %s — skipping", guild.guild_id
            )

    if not contexts:
        logger.error("No guild contexts built — all guilds failed")
        media_pool.shutdown(timeout=_MEDIA_DRAIN_TIMEOUT_SEC)
        return None, None, None, None, None

    stop_event = threading.Event()
    stats = {
        "scanned_feeds": 0,
        "pages": 0,
        "feeds": 0,
        "comments": 0,
        "replies": 0,
        "media": 0,
        "running": False,
        "log_buffer": [],
        "guilds": {},
    }
    runner = BackfillRunner(contexts, stats=stats, stop_event=stop_event)

    def _auto_backfill() -> None:
        if runner.start_background():
            logger.info("auth ok → full-history backfill auto-started (see /stats)")
        else:
            logger.info("auth ok → backfill already running (auto-start skipped)")

    auth_session = AuthSessionManager(
        store=cred_store, probe=_probe, on_ready=_auto_backfill
    )

    effective_port = port if port is not None else 9424
    config_state = {
        "apiVersion": "2",
        "guilds": [
            {"guild_id": g.guild_id, "guild_number": g.guild_number, "name": g.name}
            for g in config.guilds
        ],
        "deepbackfill_api_port": effective_port,
    }
    service = DeepbackfillService(
        stats=stats,
        config_state=config_state,
        trigger=runner.start_background,
        stats_view=runner.live_stats,
        port=effective_port,
        auth_session=auth_session,
    )
    return runner, service, stats, media_pool, auth_session


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prometheus deepbackfill: full-history capture service (auth channel)",
    )
    parser.add_argument(
        "--port", type=int, default=9424,
        help="HTTP port (default %(default)s; 0 = OS-assigned ephemeral port)",
    )
    args = parser.parse_args(argv)

    _set_pdeathsig()

    config = Config.load()

    project_root = Path(__file__).resolve().parent.parent.parent
    _setup_logging(project_root / "log" / "deepbackfill")

    logger = logging.getLogger(__name__)
    logger.info("Starting deepbackfill (pure-web QR auth channel)")

    if not config.guilds:
        logger.error(
            "No guilds configured — check conf/guilds.conf.json or "
            "channel_id in prometheus.conf.json"
        )
        sys.exit(1)

    runner, service, stats, media_pool, auth_session = _build_components(
        config, port=args.port
    )
    if runner is None:
        logger.error("No guild contexts could be built — exiting")
        sys.exit(1)
    assert service is not None and stats is not None

    logging.getLogger().addHandler(_BufferLogHandler(stats["log_buffer"]))

    if not _acquire_process_lock(config.data_dir, logger):
        media_pool.shutdown(timeout=_MEDIA_DRAIN_TIMEOUT_SEC)
        sys.exit(2)  # exit code 2 = lock conflict, launcher should not auto-restart
    logger.info("Lock acquired (pid=%d)", os.getpid())

    service.start()
    logger.info("API server listening on port %d", service.port)
    auth_session.status()  # 开机即踢探测：凭证有效直接点火，缺/失效起 QR 会话
    logger.info(
        "Ready — full-history backfill auto-starts once auth is ok "
        "(POST /action/trigger-daemon remains as the manual override)"
    )

    stop = threading.Event()

    def _on_signal(signum, frame):
        logger.info("Received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")
    finally:
        service.stop()
        auth_session.stop()
        runner.request_stop()
        if not runner.join(timeout=_RUNNER_JOIN_TIMEOUT_SEC):
            logger.warning("backfill thread did not finish within %ss", _RUNNER_JOIN_TIMEOUT_SEC)
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
        logger.info("deepbackfill stopped")


if __name__ == "__main__":
    main()
