"""Protobuf feed parsing and channel mapping for QQ guild databases.

Each entry in ``guild_feed_list_storage_table_v2`` has a blob (column 49903)
encoded as a protobuf message containing a list of posts. Each post holds the
thread id, full text, author identity (id + nickname + QQ number), and media
URLs.

Channel attribution is resolved through ``direct_node_list_table`` which maps
the feed id to a human-readable channel name.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from dataclasses import dataclass, field

from . import config


# ---------------------------------------------------------------------------
# Minimal protobuf wire-format decoder
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, i


def decode_protobuf(buf: bytes, max_depth: int = 6) -> dict:
    """Decode a protobuf buffer into ``{field_number: [values]}``.

    Each value is one of: ``int``, ``str``, ``dict`` (nested message), ``bytes``.
    """
    result: dict[int, list] = {}
    i = 0
    n = len(buf)
    while i < n:
        try:
            tag, i = _read_varint(buf, i)
        except IndexError:
            break
        field_no = tag >> 3
        wire_type = tag & 7

        if wire_type == 0:
            val, i = _read_varint(buf, i)
            result.setdefault(field_no, []).append(val)
        elif wire_type == 2:
            ln, i = _read_varint(buf, i)
            sub = buf[i : i + ln]
            i += ln
            result.setdefault(field_no, []).append(_classify_ld(sub, max_depth))
        elif wire_type == 5:
            result.setdefault(field_no, []).append(struct.unpack("<I", buf[i : i + 4])[0])
            i += 4
        elif wire_type == 1:
            result.setdefault(field_no, []).append(struct.unpack("<Q", buf[i : i + 8])[0])
            i += 8
        else:
            break
    return result


def _classify_ld(sub: bytes, max_depth: int):
    if max_depth <= 0:
        return sub
    try:
        text = sub.decode("utf-8")
        if all(c.isprintable() or c in "\n\r\t" for c in text):
            return text
    except (UnicodeDecodeError, ValueError):
        pass
    try:
        nested = decode_protobuf(sub, max_depth - 1)
        if nested:
            return nested
    except Exception:
        pass
    return sub


def _first(msg: dict, field_no: int, default=None):
    vals = msg.get(field_no)
    return vals[0] if vals else default


def _all_str(msg: dict, field_no: int) -> list[str]:
    return [v for v in msg.get(field_no, []) if isinstance(v, str)]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Post:
    thread_id: str = ""
    content: str = ""
    author_id: str = ""
    author_name: str = ""
    author_qq: str = ""
    role: str = ""
    media_urls: list[str] = field(default_factory=list)
    feed_id: str = ""
    raw_msg: dict = field(default_factory=dict)

    @property
    def is_reply(self) -> bool:
        prefix = config.get("feed_id_prefix", "B_")
        return self.thread_id.startswith(prefix) and "Son" in self.thread_id


@dataclass
class Channel:
    feed_id: str
    name: str
    channel_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feed extraction
# ---------------------------------------------------------------------------

_URL_RE = re.compile(rb"https?://[^\x00-\x1f\"'<>\\]+")


def _extract_urls(buf: bytes) -> list[str]:
    return [u.decode(errors="replace") for u in _URL_RE.findall(buf)]


def _parse_post(msg: dict) -> Post:
    post = Post()

    post.thread_id = _first(msg, 1, "") or ""

    content_parts = []
    for content_msg in msg.get(2, []):
        if isinstance(content_msg, dict):
            for inner in content_msg.get(1, []):
                if isinstance(inner, dict):
                    text = _first(inner, 3, "")
                    if isinstance(text, str) and text:
                        content_parts.append(text)
    post.content = "\n".join(content_parts).strip()

    for author_msg in msg.get(4, []):
        if isinstance(author_msg, dict):
            post.author_id = _first(author_msg, 1, "") or post.author_id
            name = _first(author_msg, 2, "")
            if isinstance(name, str):
                post.author_name = name
            avatar = _first(author_msg, 5, "")
            if isinstance(avatar, str) and avatar:
                post.media_urls.append(avatar)
            qq = _first(author_msg, 25, "")
            if isinstance(qq, str):
                post.author_qq = qq
            for nested in author_msg.get(3, []):
                if isinstance(nested, dict):
                    role = _first(nested, 7, "")
                    if isinstance(role, str) and role:
                        post.role = role

    return post


def parse_feed_blob(blob: bytes, feed_id: str = "") -> list[Post]:
    """Parse a feed blob and return all posts within it."""
    top = decode_protobuf(blob, max_depth=7)
    posts: list[Post] = []

    feed_wrapper = _first(top, 49903)
    if not isinstance(feed_wrapper, dict):
        return posts

    for post_msg in feed_wrapper.get(1, []):
        if not isinstance(post_msg, dict):
            continue
        post = _parse_post(post_msg)
        post.feed_id = feed_id

        post_bytes = _serialize_for_url_search(post_msg)
        urls = _extract_urls(post_bytes)
        for u in urls:
            if u not in post.media_urls:
                post.media_urls.append(u)

        post.raw_msg = post_msg
        posts.append(post)

    return posts


def _serialize_for_url_search(msg: dict) -> bytes:
    parts = []
    for vals in msg.values():
        for v in vals:
            if isinstance(v, bytes):
                parts.append(v)
            elif isinstance(v, str):
                parts.append(v.encode(errors="replace"))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Channel mapping
# ---------------------------------------------------------------------------

def load_channels(con: sqlite3.Connection) -> dict[str, Channel]:
    """Read ``direct_node_list_table`` to build a ``{feed_id: Channel}`` map."""
    channels: dict[str, Channel] = {}
    try:
        rows = con.execute('SELECT "42052", "42053", "40051" FROM direct_node_list_table').fetchall()
    except sqlite3.DatabaseError:
        return channels
    for feed_id, name, channel_ids_raw in rows:
        if feed_id is None:
            continue
        feed_id = str(feed_id)
        ch_ids: list[str] = []
        if isinstance(channel_ids_raw, bytes):
            decoded = decode_protobuf(channel_ids_raw, max_depth=2)
            for vals in decoded.values():
                for v in vals:
                    if isinstance(v, str):
                        ch_ids.append(v)
        elif isinstance(channel_ids_raw, (list, tuple)):
            ch_ids = [str(x) for x in channel_ids_raw]
        if feed_id not in channels:
            channels[feed_id] = Channel(
                feed_id=feed_id, name=str(name) if name else feed_id, channel_ids=ch_ids
            )
    return channels


def load_feeds(con: sqlite3.Connection) -> list[tuple[str, bytes, int]]:
    """Return ``[(feed_id, blob, timestamp)]`` from ``guild_feed_list_storage_table_v2``."""
    try:
        return con.execute(
            'SELECT "49900", "49903", "49904" FROM guild_feed_list_storage_table_v2'
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
