#!/usr/bin/env python3
"""Backfill comment images from already-scraped ``comments.jsonl`` files.

Scans ``data/<guild_id>/comments.jsonl`` line by line, extracts
``richContents.images[*].picUrl`` and ``richContents.sticker.custom_face.origin_image_url``
from every comment (and nested ``vecReply[*]``), and downloads each via
:class:`MediaDownloader.download_comment_media`.

Idempotent: :class:`MediaDownloader` keeps a ``_seen`` set seeded from
``media_index.jsonl`` AND ``comment_media_index.jsonl`` at startup, so
already-downloaded URLs are skipped. Safe to re-run after interruptions.

CLI:
    python scripts/backfill_comment_media.py [--data-dir <path>] [--guild-id <id>] [-v]

If ``--guild-id`` is omitted, every numeric subdir of ``data/`` that contains
a ``comments.jsonl`` is processed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _process_guild(data_dir: Path, guild_id: str) -> tuple[int, int]:
    """Process one guild's ``comments.jsonl``.

    Returns ``(comments_processed, images_downloaded)``. ``images_downloaded``
    counts mappings written this run (newly fetched OR already on disk) — see
    :meth:`MediaDownloader._download_comment_one` for the "always-index" rule.
    """
    from src.web_scraper.media import MediaDownloader

    guild_dir = data_dir / guild_id
    comments_path = guild_dir / "comments.jsonl"
    if not comments_path.exists():
        logger.warning("comments.jsonl not found for guild %s at %s", guild_id, comments_path)
        return (0, 0)

    downloader = MediaDownloader(guild_dir)
    total_comments = 0
    total_images = 0

    with comments_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("invalid JSON line in %s", comments_path)
                continue

            d = record.get("d") or {}
            feed_id = str(d.get("feedId", ""))
            for comment in (d.get("vecComment") or []):
                if not isinstance(comment, dict):
                    continue
                try:
                    count = downloader.download_comment_media(comment, feed_id=feed_id)
                except Exception:
                    logger.exception(
                        "download_comment_media failed for feed=%s comment_id=%s",
                        feed_id,
                        comment.get("id"),
                    )
                    count = 0
                total_images += count
                total_comments += 1
                if total_comments % 1000 == 0:
                    logger.info(
                        "Processed %d comments, downloaded %d images (guild %s)",
                        total_comments,
                        total_images,
                        guild_id,
                    )

    return (total_comments, total_images)


def main() -> int:
    """CLI entry point. Returns process exit code (0 = ok, 1 = error)."""
    project_root = _project_root()
    # Make ``from src.web_scraper.media import MediaDownloader`` work when the
    # script is run directly (no PYTHONPATH=. required).
    sys.path.insert(0, str(project_root))

    parser = argparse.ArgumentParser(
        description=(
            "Backfill comment images from data/<guild_id>/comments.jsonl. "
            "Idempotent: safe to re-run after an interruption."
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
            "data/ that contains a comments.jsonl."
        ),
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
        guild_ids = sorted(
            entry.name
            for entry in data_dir.iterdir()
            if entry.is_dir()
            and entry.name.isnumeric()
            and (entry / "comments.jsonl").exists()
        )
        if not guild_ids:
            logger.error(
                "no guild dirs with comments.jsonl found in %s", data_dir
            )
            return 1

    grand_comments = 0
    grand_images = 0
    for gid in guild_ids:
        logger.info("Processing guild %s", gid)
        c, i = _process_guild(data_dir, gid)
        grand_comments += c
        grand_images += i
        logger.info(
            "Guild %s: %d comments, %d image mappings", gid, c, i
        )

    logger.info(
        "Done. Total: %d comments, %d image mappings across %d guild(s).",
        grand_comments,
        grand_images,
        len(guild_ids),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
