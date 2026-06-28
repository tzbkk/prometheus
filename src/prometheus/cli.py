"""Prometheus — archive QQ guild channel posts before they vanish.

Usage::

    python -m prometheus run              # extract + archive now
    python -m prometheus run --media      # also copy local media cache
    python -m prometheus run --pseudonymize  # hash author identities
    python -m prometheus export           # write Markdown from the archive

The tool reads the QQ client's own local databases. Run it as the same user
that owns the QQ process — no root required.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import textwrap
from pathlib import Path

from . import cipher, config, feed, keyring, media


def find_qq_account_dir() -> Path:
    """Locate ``~/.config/QQ/nt_qq_<hash>`` automatically."""
    config = Path.home() / ".config" / "QQ"
    if not config.is_dir():
        raise RuntimeError(f"QQ config dir not found at {config}")
    candidates = sorted(
        p for p in config.iterdir() if p.is_dir() and p.name.startswith("nt_qq_")
    )
    if not candidates:
        raise RuntimeError("No nt_qq_* account directory found. Is QQ logged in?")
    nt_db = candidates[0] / "nt_db"
    if not nt_db.is_dir():
        raise RuntimeError(f"nt_db not found under {candidates[0]}")
    return candidates[0]


# ---------------------------------------------------------------------------
# Archive database
# ---------------------------------------------------------------------------

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    thread_id   TEXT PRIMARY KEY,
    feed_id     TEXT NOT NULL,
    channel     TEXT,
    content     TEXT,
    author_id   TEXT,
    author_name TEXT,
    author_qq   TEXT,
    role        TEXT,
    media_urls  TEXT,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    feed_id     TEXT PRIMARY KEY,
    name        TEXT,
    channel_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_feed ON posts(feed_id);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id);
"""


def open_archive(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.executescript(ARCHIVE_SCHEMA)
    return con


def _pseudonymize(value: str, salt: str | None = None) -> str:
    if not value:
        return value
    s: str = salt or config.get("pseudonymize_salt") or "prometheus"
    return "anon_" + hashlib.sha256((s + value).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Run cycle
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    account_dir = find_qq_account_dir()
    nt_db_dir = account_dir / "nt_db"
    nt_data_dir = account_dir / "nt_data"
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / "_decrypted"
    work_dir.mkdir(exist_ok=True)

    print(f"[1/5] 定位 QQ 数据目录: {account_dir}")

    print("[2/5] 从 QQ 进程内存提取数据库密钥...")
    pid = keyring.find_qq_main_pid()
    print(f"      QQ 主进程 PID: {pid}")
    keys = keyring.extract_keys(nt_db_dir, pid)
    if not keys:
        print("      错误: 未提取到任何密钥。QQ 是否已登录?")
        sys.exit(1)

    print("[3/5] 解密 guild_msg.db ...")
    guild_db = nt_db_dir / "guild_msg.db"
    guild_salt = None
    with open(guild_db, "rb") as f:
        f.seek(1024)
        guild_salt = f.read(16).hex()
    if guild_salt not in keys:
        print(f"      错误: guild_msg.db 的 salt ({guild_salt[:16]}...) 未找到密钥")
        sys.exit(1)
    dec_path = work_dir / "guild_msg.db"
    cipher.decrypt_file(guild_db, dec_path, keys[guild_salt].key)
    con = cipher.open_decrypted(dec_path)
    print(f"      解密成功: {dec_path}")

    print("[4/5] 解析帖子 + 频道 ...")
    channels = feed.load_channels(con)
    feeds = feed.load_feeds(con)
    all_posts: list[feed.Post] = []
    for feed_id, blob, _ts in feeds:
        posts = feed.parse_feed_blob(blob, str(feed_id))
        all_posts.extend(posts)
    print(f"      频道: {len(channels)} 个")
    for ch in channels.values():
        print(f"        • {ch.name}  (feed_id={ch.feed_id})")
    print(f"      帖子: {len(all_posts)} 条")

    print("[5/5] 写入归档 ...")
    archive_path = output_dir / "archive.db"
    acon = open_archive(archive_path)

    for ch in channels.values():
        acon.execute(
            "INSERT OR REPLACE INTO channels VALUES (?,?,?)",
            (ch.feed_id, ch.name, json.dumps(ch.channel_ids)),
        )

    new_count = 0
    skip_count = 0
    for post in all_posts:
        if not post.thread_id:
            continue
        ch = channels.get(post.feed_id)
        channel_name = ch.name if ch else post.feed_id

        author_id = post.author_id
        author_name = post.author_name
        author_qq = post.author_qq
        if args.pseudonymize:
            author_id = _pseudonymize(author_id)
            author_name = _pseudonymize(author_name)
            author_qq = _pseudonymize(author_qq)

        cur = acon.execute(
            "INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                post.thread_id,
                post.feed_id,
                channel_name,
                post.content,
                author_id,
                author_name,
                author_qq,
                post.role,
                json.dumps(post.media_urls),
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        if cur.rowcount > 0:
            new_count += 1
        else:
            skip_count += 1

    acon.commit()
    print(f"      新增 {new_count} 条, 跳过已归档 {skip_count} 条")
    print(f"      归档数据库: {archive_path}")

    if args.media:
        print("\n[+] 复制本地媒体缓存 ...")
        media_dest = output_dir / "media"
        files, total = media.copy_local_cache(nt_data_dir, media_dest)
        print(f"      复制 {len(files)} 个文件 ({total / 1024 / 1024:.1f} MB) -> {media_dest}")

    acon.close()
    con.close()
    print("\n完成。下次运行会自动增量归档(按 thread_id 去重)。")


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def export(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    archive_path = output_dir / "archive.db"
    if not archive_path.exists():
        print(f"归档数据库不存在: {archive_path}\n请先运行: python -m prometheus run")
        sys.exit(1)

    md_dir = output_dir / "markdown"
    md_dir.mkdir(exist_ok=True)
    acon = open_archive(archive_path)

    channels = {
        row[0]: row[1]
        for row in acon.execute("SELECT feed_id, name FROM channels").fetchall()
    }

    for feed_id, ch_name in channels.items():
        safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", ch_name)[:40] or feed_id
        md_path = md_dir / f"{safe}.md"
        rows = acon.execute(
            "SELECT * FROM posts WHERE feed_id=? ORDER BY archived_at", (feed_id,)
        ).fetchall()
        if not rows:
            continue
        with open(md_path, "w") as f:
            f.write(f"# {ch_name}\n\n")
            f.write(f"> feed_id: `{feed_id}`  |  帖子数: {len(rows)}\n\n---\n\n")
            for row in rows:
                _tid, _fid, _ch, content, aid, aname, aqq, role, urls_json, ts = row
                f.write(f"## {content[:60] or '(无正文)'}\n\n")
                f.write(f"- **作者**: {aname or aid}")
                if role:
                    f.write(f"  ({role})")
                f.write("\n")
                if aqq and not args.pseudonymize:
                    f.write(f"- **QQ**: {aqq}\n")
                f.write(f"- **thread_id**: `{_tid}`\n")
                f.write(f"- **归档时间**: {ts}\n\n")
                if content:
                    f.write(textwrap.indent(content, "> ") + "\n\n")
                urls = json.loads(urls_json) if urls_json else []
                if urls:
                    f.write("**媒体**:\n")
                    for u in urls:
                        f.write(f"- {u}\n")
                    f.write("\n")
                f.write("---\n\n")
        print(f"  导出: {md_path} ({len(rows)} 帖)")

    acon.close()
    print(f"\nMarkdown 导出完成 -> {md_dir}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="prometheus",
        description="Archive QQ guild channel posts from local databases.",
    )
    parser.add_argument(
        "--config",
        help="path to prometheus.conf.json (default: project root)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="extract and archive posts")
    p_run.add_argument("--output", "-o", default=None, help="output directory (default: <project>/output)")
    p_run.add_argument("--media", action="store_true", help="also copy local media cache")
    p_run.add_argument("--pseudonymize", action="store_true", help="hash author identities")
    p_run.set_defaults(func=run)

    p_exp = sub.add_parser("export", help="export Markdown from archive")
    p_exp.add_argument("--output", "-o", default=None, help="output directory (default: <project>/output)")
    p_exp.add_argument("--pseudonymize", action="store_true", help="hash author identities")
    p_exp.set_defaults(func=export)

    args = parser.parse_args()
    if getattr(args, "config", None):
        os.environ["PROMETHEUS_CONFIG"] = args.config
        config.reset_cache()
    if getattr(args, "output", None) is None:
        args.output = str(config.output_dir())
    args.func(args)


if __name__ == "__main__":
    main()
