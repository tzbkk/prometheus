#!/usr/bin/env python3
"""Migrate flat ``data/`` layout to per-guild ``data/<guild_id>/`` layout.

This module is BOTH:

* A standalone CLI — ``python scripts/migrate_multi_guild.py [--data-dir D]
  [--guild-id G]`` — for one-off / manual migrations.
* An importable function ``migrate(data_dir, guild_id) -> bool`` used by
  ``src.web_scraper.__main__.main()`` for auto-migration on startup.

The migration is **idempotent**: every entry is moved individually and an
interrupted run can be resumed safely. ``prometheus.lock`` is intentionally
LEFT at the parent ``data/`` directory — it is the process lock, not per-guild
state (plan §3.1).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Flat-file entries that live directly under data/ and must move to
# data/<guild_id>/. Kept as a module-level constant so callers / tests can
# inspect the canonical list.
_FLAT_ENTRIES = [
    "feeds.jsonl",
    "comments.jsonl",
    "ids.json",
    "comment_keys.json",
    "comments_fetched_ids.json",
    "media_index.jsonl",
    "media_index.jsonl.bak",
    "state.json",
    "dead_media.jsonl",
    "dead_media_permanent.jsonl",
]

# The media directory. Handled separately from _FLAT_ENTRIES because it must be
# merged file-by-file (os.replace onto an existing dir raises IsADirectoryError).
_MEDIA_DIR = "media"


def _merge_media_dir(src_media: Path, dst_media: Path) -> bool:
    """Merge ``src_media`` into ``dst_media`` file-by-file (idempotent).

    Existing target files are NEVER overwritten — if a name already exists at
    the destination the source copy is left in place (treated as already
    migrated on a prior partial run). The source directory is removed if it
    ends up empty; otherwise it is left alone (non-fatal).

    Returns True if at least one file was moved during this call.
    """
    moved_any = False
    dst_media.mkdir(parents=True, exist_ok=True)
    for entry in src_media.iterdir():
        dst = dst_media / entry.name
        if dst.exists():
            # Idempotent: don't clobber an already-migrated file.
            continue
        entry.rename(dst)
        moved_any = True
    # Drop the now-empty source directory. Non-fatal: collisions or external
    # readers may keep it populated.
    try:
        src_media.rmdir()
    except OSError:
        pass
    return moved_any


def migrate(data_dir: Path, guild_id: str) -> bool:
    """Migrate the flat ``data_dir`` layout into ``data_dir/guild_id/``.

    Parameters
    ----------
    data_dir:
        Path to the PARENT data root (e.g. project ``data/``), NOT the
        per-guild directory.
    guild_id:
        Numeric guild identifier as a string (e.g. ``"7743321643036658"``).

    Returns
    -------
    bool
        ``True`` if any files were moved OR the target already held entries
        from a prior (partial or complete) run. ``False`` if there was nothing
        to migrate (no source entries and an empty/absent target) or if
        ``guild_id`` failed validation.

    Notes
    -----
    * G10 — ``guild_id`` is validated up front as a non-empty numeric string;
      on failure the function logs an error and returns ``False`` WITHOUT
      creating ``data//`` or ``data/<junk>/``.
    * ``prometheus.lock`` is deliberately NOT in :data:`_FLAT_ENTRIES`; it
      stays at the parent ``data/`` directory.
    """
    # G10 — validate guild_id before any filesystem mutation so we never
    # create ``data//`` (empty) or ``data/abc/`` (non-numeric junk).
    if not guild_id or not guild_id.isnumeric():
        logger.error(
            "migrate: invalid guild_id %r (must be a non-empty numeric string); "
            "aborting migration.",
            guild_id,
        )
        return False

    data_dir = Path(data_dir)
    target = data_dir / guild_id
    target.mkdir(parents=True, exist_ok=True)

    # encountered_any is True iff we found an entry at EITHER the source or the
    # destination. It distinguishes "already migrated / migrated now" (True)
    # from "nothing to do at all" (False, e.g. empty data_dir).
    encountered_any = False

    for name in _FLAT_ENTRIES:
        src = data_dir / name
        dst = target / name
        if src.exists():
            encountered_any = True
            if dst.exists():
                # Both present — leave the source alone rather than risk
                # clobbering an already-migrated file. (Rare; normally only
                # the destination exists after a move.)
                logger.warning(
                    "migrate: both src and dst exist for %s; leaving src in place.",
                    name,
                )
                continue
            logger.info("migrate: moving %s -> %s", src, dst)
            shutil.move(str(src), str(dst))
        elif dst.exists():
            encountered_any = True

    src_media = data_dir / _MEDIA_DIR
    dst_media = target / _MEDIA_DIR
    if src_media.exists() and src_media.is_dir():
        encountered_any = True
        _merge_media_dir(src_media, dst_media)
    elif dst_media.exists() and dst_media.is_dir():
        encountered_any = True

    if encountered_any:
        logger.info("migrate: guild %s migration complete (data_dir=%s).", guild_id, data_dir)
    else:
        logger.info(
            "migrate: nothing to migrate for guild %s (data_dir=%s).",
            guild_id,
            data_dir,
        )

    return encountered_any


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_guild_id(project_root: Path) -> str | None:
    """Resolve a default guild_id from conf files WITHOUT importing Config.

    Keeps the script standalone-runnable. Resolution mirrors
    :class:`src.web_scraper.config.Config`:

    1. ``guilds.conf.json`` — sibling of the active ``prometheus.conf.json``
       (honoring ``PROMETHEUS_CONFIG``), then project ``conf/guilds.conf.json``.
       Take ``guilds[0].guild_id``.
    2. Legacy fallback: ``prometheus.conf.json`` ``channel_id`` field.
    """
    prometheus_env = os.environ.get("PROMETHEUS_CONFIG")
    if prometheus_env:
        prometheus_conf = Path(prometheus_env).expanduser()
    else:
        prometheus_conf = project_root / "conf" / "prometheus.conf.json"

    guilds_candidates = [
        prometheus_conf.parent / "guilds.conf.json",
        project_root / "conf" / "guilds.conf.json",
    ]
    for cand in guilds_candidates:
        try:
            if cand.is_file():
                data = json.loads(cand.read_text(encoding="utf-8"))
                guilds = data.get("guilds") or []
                if guilds and guilds[0].get("guild_id"):
                    return str(guilds[0]["guild_id"])
                break  # file exists but has no guilds — stop guilds search
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("migrate: could not read %s: %s", cand, exc)

    try:
        if prometheus_conf.is_file():
            data = json.loads(prometheus_conf.read_text(encoding="utf-8"))
            channel_id = data.get("channel_id")
            if channel_id:
                return str(channel_id)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("migrate: could not read %s: %s", prometheus_conf, exc)

    return None


def main() -> int:
    """CLI entry point. Returns process exit code (0 = ok, 1 = error)."""
    parser = argparse.ArgumentParser(
        description=(
            "Migrate a flat data/ layout into the per-guild data/<guild_id>/ "
            "layout. Idempotent: safe to re-run after an interruption."
        )
    )
    project_root = _project_root()
    parser.add_argument(
        "--data-dir",
        default=str(project_root / "data"),
        help="Path to the parent data root (default: <project>/data).",
    )
    parser.add_argument(
        "--guild-id",
        default=None,
        help=(
            "Numeric guild id. If omitted, read from conf/guilds.conf.json "
            "(first guild) then conf/prometheus.conf.json channel_id."
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

    guild_id = args.guild_id
    if not guild_id:
        guild_id = _resolve_guild_id(project_root)
    if not guild_id:
        logger.error(
            "main: could not resolve a guild_id (no --guild-id given and no "
            "conf/guilds.conf.json or conf/prometheus.conf.json channel_id found)."
        )
        return 1
    if not guild_id.isnumeric():
        logger.error(
            "main: guild_id %r is not numeric; aborting (use --guild-id <number>).",
            guild_id,
        )
        return 1

    data_dir = Path(args.data_dir).expanduser()
    logger.info("main: migrating data_dir=%s guild_id=%s", data_dir, guild_id)
    ok = migrate(data_dir, str(guild_id))
    logger.info("main: %s.", "migration OK" if ok else "nothing to migrate")
    # Both "moved files" and "nothing to migrate" are success exit codes;
    # only resolution/validation failures are errors (handled above).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
