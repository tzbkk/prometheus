#!/usr/bin/env python3
"""Backfill media + comments from inject.js ``feeds.jsonl``.

Wave 2 companion to Wave 1's inject.js ID_ONLY mode. Reads the per-guild
``data/<guild_id>/feeds.jsonl`` produced by inject.js and fills in what
ID_ONLY mode deliberately skipped:

1. **Feed media** (``images[*].picUrl``, ``videos[*].playUrl``) via
   :class:`MediaDownloader.download_feed_media`.
2. **Comments** via :class:`CommentsScraper.scrape_feed_comments`, skipping
   any feed marked complete in ``comments_fetched_ids.json``.
3. **Comment media** (``richContents`` images + stickers) automatically,
   via the ``media_downloader=`` kwarg passed to :class:`CommentsScraper`.

Idempotent: safe to re-run after an interruption.

* MediaDownloader keeps a ``_seen`` set seeded from
  ``media_index.jsonl`` + ``comment_media_index.jsonl`` at startup, so
  already-downloaded URLs are skipped on the next run.
* CommentsScraper / Store dedupe by sorted comment-id key, and the
  script additionally skips any feed marked complete in
  ``comments_fetched_ids.json`` (no API call made for those).

CLI:
    python scripts/scraper_backfill.py \\
        [--data-dir <path>] [--guild-id <id>] [--max-workers N] [-v]

If ``--guild-id`` is omitted, every numeric subdir of ``--data-dir`` that
contains a ``feeds.jsonl`` is processed sequentially.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def discover_guilds(data_dir: Path) -> list[str]:
    """Return numeric subdir names of ``data_dir`` containing ``feeds.jsonl``.

    Sorted ascending so repeated runs visit guilds in a stable order.
    """
    if not data_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in data_dir.iterdir()
        if entry.is_dir()
        and entry.name.isnumeric()
        and (entry / "feeds.jsonl").exists()
    )


def load_feeds(guild_dir: Path) -> list[dict]:
    """Read ``feeds.jsonl`` line by line, return the list of feed dicts.

    Malformed lines are skipped with a warning. Returns ``[]`` if the
    file is absent.
    """
    feeds_path = guild_dir / "feeds.jsonl"
    if not feeds_path.exists():
        return []
    feeds: list[dict] = []
    with feeds_path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                feed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "skip malformed JSON line %d in %s", line_no, feeds_path
                )
                continue
            if isinstance(feed, dict):
                feeds.append(feed)
    return feeds


def load_fully_fetched_feed_ids(guild_dir: Path) -> set[str]:
    """Build the set of feed IDs that CommentsScraper has FULLY fetched.

    Reads ``comments_fetched_ids.json`` (one ``"feed_id\\tcount"`` per
    line, written by :meth:`Store.mark_comments_fetched`). This is the
    authoritative source: ``comments.jsonl`` may contain PARTIAL passive
    captures from inject.js (only what QQ preloaded during scrolling)
    which still need a full scrape. Returns an empty set if the file is
    absent.
    """
    path = guild_dir / "comments_fetched_ids.json"
    if not path.exists():
        return set()
    feed_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            parts = line.split("\t")
            if parts and parts[0]:
                feed_ids.add(str(parts[0]))
    return feed_ids


def resolve_guild_number(guild_id: str, project_root: Path) -> str | None:
    """Look up ``guild_number`` for ``guild_id`` from conf/guilds.conf.json.

    Honors the ``PROMETHEUS_CONFIG`` env var (same resolution order as
    :class:`src.web_scraper.config.Config`): sibling of the active
    ``prometheus.conf.json`` first, then project ``conf/guilds.conf.json``.

    Returns ``None`` if the file or entry is missing.
    """
    import os

    prometheus_env = os.environ.get("PROMETHEUS_CONFIG")
    if prometheus_env:
        prometheus_conf = Path(prometheus_env).expanduser()
    else:
        prometheus_conf = project_root / "conf" / "prometheus.conf.json"

    candidates = [
        prometheus_conf.parent / "guilds.conf.json",
        project_root / "conf" / "guilds.conf.json",
    ]
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read %s: %s", cand, exc)
            continue
        for entry in data.get("guilds") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("guild_id", "")) == guild_id:
                gn = entry.get("guild_number")
                if gn:
                    return str(gn)
        return None
    return None


def _backfill_media(feeds: list[dict], downloader, max_workers: int, guild_id: str) -> int:
    """Download feed media for every feed via ThreadPoolExecutor.

    Returns total newly-downloaded file count (cached files contribute 0).
    """
    total = len(feeds)
    if total == 0:
        return 0
    new_files = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(downloader.download_feed_media, f): f for f in feeds}
        for fut in as_completed(futures):
            feed = futures[fut]
            try:
                new_files += fut.result() or 0
            except Exception:
                logger.exception(
                    "download_feed_media failed for feed=%s (guild %s)",
                    feed.get("id"), guild_id,
                )
            completed += 1
            if completed % 100 == 0 or completed == total:
                logger.info(
                    "media progress: %d/%d feeds, %d new files (guild %s)",
                    completed, total, new_files, guild_id,
                )
    return new_files


def _select_comment_targets(
    feeds: list[dict], already_done: set[str]
) -> tuple[list[str], int]:
    """Pick feed IDs that still need comment scraping.

    Skips feeds whose id is already in ``already_done`` AND feeds with
    ``commentCount <= 0`` (the QQ API returns an empty vecComment for
    them anyway, so calling it is pure waste).

    Returns ``(targets, skipped_count)``.
    """
    targets: list[str] = []
    skipped = 0
    for feed in feeds:
        feed_id = str(feed.get("id", ""))
        if not feed_id:
            continue
        if feed_id in already_done:
            skipped += 1
            continue
        try:
            cc = int(feed.get("commentCount", 0) or 0)
        except (TypeError, ValueError):
            cc = 0
        if cc <= 0:
            skipped += 1
            continue
        targets.append(feed_id)
    return targets, skipped


def backfill_guild(
    guild_dir: Path,
    guild_id: str,
    guild_number: str,
    max_workers: int,
) -> dict:
    """Process one guild: download feed media, then scrape missing comments.

    Returns a stats dict ``{"feeds": N, "media_new": N, "comments_new": N,
    "comment_targets": N}``.
    """
    from src.web_scraper.media import MediaDownloader
    from src.web_scraper.client import QQWebClient
    from src.web_scraper.store import Store
    from src.web_scraper.comments import CommentsScraper

    # --- Phase 1: feed media ---------------------------------------------
    # MediaDownloader reads CDN URLs (not pd.qq.com), so no QQWebClient yet.
    downloader = MediaDownloader(guild_dir, max_workers=max_workers)
    feeds = load_feeds(guild_dir)
    logger.info(
        "guild %s: loaded %d feeds from %s",
        guild_id, len(feeds), guild_dir / "feeds.jsonl",
    )
    media_new = _backfill_media(feeds, downloader, max_workers, guild_id)

    # --- Phase 2: missing comments + comment media -----------------------
    already_done = load_fully_fetched_feed_ids(guild_dir)
    targets, skipped = _select_comment_targets(feeds, already_done)
    logger.info(
        "guild %s: %d feeds need comments (%d skipped — already done or zero count)",
        guild_id, len(targets), skipped,
    )

    comments_new = 0
    if targets:
        client = QQWebClient(guild_id, guild_number, max_workers)
        store = Store(guild_dir)
        scraper = CommentsScraper(
            client,
            store,
            guild_number,
            max_workers=max_workers,
            media_downloader=downloader,
        )
        total_targets = len(targets)
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(scraper.scrape_feed_comments, fid): fid for fid in targets}
            for fut in as_completed(futures):
                fid = futures[fut]
                try:
                    comments_new += fut.result() or 0
                except Exception:
                    logger.exception(
                        "scrape_feed_comments failed for feed=%s (guild %s)",
                        fid, guild_id,
                    )
                completed += 1
                if completed % 100 == 0 or completed == total_targets:
                    logger.info(
                        "comment progress: %d/%d feeds, %d new pages (guild %s)",
                        completed, total_targets, comments_new, guild_id,
                    )

    return {
        "feeds": len(feeds),
        "media_new": media_new,
        "comments_new": comments_new,
        "comment_targets": len(targets),
    }


def main() -> int:
    """CLI entry point. Returns process exit code (0 = ok, 1 = error)."""
    project_root = _project_root()
    # Make ``from src.web_scraper...`` work when the script is run directly.
    sys.path.insert(0, str(project_root))

    parser = argparse.ArgumentParser(
        description=(
            "Backfill feed media + comments from data/<guild_id>/feeds.jsonl "
            "produced by inject.js ID_ONLY mode. Idempotent: safe to re-run."
        )
    )
    parser.add_argument(
        "--data-dir",
        default=str(project_root / "data"),
        help="Path to the parent data root (default: <project>/data).",
    )
    parser.add_argument(
        "--guild-id",
        default=None,
        help=(
            "Numeric guild id. If omitted, process every numeric subdir of "
            "data/ that contains a feeds.jsonl."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Thread-pool size for media + comment concurrency (default: 10).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_dir():
        logger.error("data dir %s does not exist", data_dir)
        return 1

    if args.guild_id:
        if not args.guild_id.isnumeric():
            logger.error(
                "guild_id %r is not numeric; aborting (use --guild-id <number>).",
                args.guild_id,
            )
            return 1
        guild_ids = [args.guild_id]
    else:
        guild_ids = discover_guilds(data_dir)
        if not guild_ids:
            logger.error(
                "no guild dirs with feeds.jsonl found in %s", data_dir
            )
            return 1

    grand = {"feeds": 0, "media_new": 0, "comments_new": 0, "comment_targets": 0}
    for gid in guild_ids:
        guild_dir = data_dir / gid
        feeds_path = guild_dir / "feeds.jsonl"
        if not feeds_path.exists():
            logger.warning(
                "guild %s: feeds.jsonl missing at %s — skipping",
                gid, feeds_path,
            )
            continue

        guild_number = resolve_guild_number(gid, project_root)
        if not guild_number:
            logger.error(
                "guild %s: no guild_number found in conf/guilds.conf.json — "
                "skipping (add an entry with guild_id=%s to scrape comments).",
                gid, gid,
            )
            continue

        logger.info("=== guild %s (guild_number=%s) ===", gid, guild_number)
        try:
            stats = backfill_guild(guild_dir, gid, guild_number, args.max_workers)
        except Exception:
            logger.exception("guild %s: backfill failed — continuing", gid)
            continue

        logger.info(
            "guild %s: done — %d feeds, %d new media, %d comment targets, %d new comment pages",
            gid, stats["feeds"], stats["media_new"],
            stats["comment_targets"], stats["comments_new"],
        )
        for k in grand:
            grand[k] += stats[k]

    logger.info(
        "Done. Total across %d guild(s): %d feeds, %d new media, "
        "%d comment targets, %d new comment pages.",
        len(guild_ids), grand["feeds"], grand["media_new"],
        grand["comment_targets"], grand["comments_new"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
