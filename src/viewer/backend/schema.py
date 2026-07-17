"""SQLite schema for the viewer backend (feeds / feeds_fts / media / comments / meta)."""

import sqlite3

# Column-level DDL kept as module constants so T6 (ingestion) and tests can
# introspect the schema without re-parsing SQL.

_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id             TEXT PRIMARY KEY,
    create_time    INTEGER,
    title_text     TEXT,
    author_nick    TEXT,
    author_id      TEXT,
    author_avatar  TEXT,
    like_count     INTEGER DEFAULT 0,
    comment_count  INTEGER DEFAULT 0,
    image_count    INTEGER DEFAULT 0,
    video_count    INTEGER DEFAULT 0,
    raw_json       TEXT,
    indexed_at     TEXT
)
"""

_CREATE_FEEDS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS feeds_fts USING fts5(
    feed_id    UNINDEXED,
    title_text,
    raw_json,
    tokenize = 'unicode61'
)
"""

_CREATE_MEDIA = """
CREATE TABLE IF NOT EXISTS media (
    feed_id  TEXT,
    file     TEXT,
    url      TEXT,
    type     TEXT,
    size     INTEGER,
    FOREIGN KEY (feed_id) REFERENCES feeds(id)
)
"""

_CREATE_COMMENTS = """
CREATE TABLE IF NOT EXISTS comments (
    id             TEXT PRIMARY KEY,
    feed_id        TEXT NOT NULL,
    parent_id      TEXT,
    create_time    INTEGER,
    author_nick    TEXT,
    author_avatar  TEXT,
    content_text   TEXT,
    ip_location    TEXT,
    like_count     INTEGER DEFAULT 0,
    reply_count    INTEGER DEFAULT 0,
    sequence       INTEGER
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
)
"""

_CREATE_INDEX_FEEDS_CREATE_TIME = (
    "CREATE INDEX IF NOT EXISTS idx_feeds_create_time ON feeds(create_time DESC)"
)

_CREATE_INDEX_MEDIA_FEED_ID = (
    "CREATE INDEX IF NOT EXISTS idx_media_feed_id ON media(feed_id)"
)

_CREATE_INDEX_COMMENTS_FEED_ID = (
    "CREATE INDEX IF NOT EXISTS idx_comments_feed_id ON comments(feed_id)"
)


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Probe FTS5 support by creating a throwaway virtual table.

    Some distro SQLite builds compile FTS5 out; the probe keeps init_db robust.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE __fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE __fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def init_db(db_path: str) -> sqlite3.Connection:
    """Create all viewer tables/indexes at ``db_path`` if absent (idempotent).

    FTS5 is created only when supported; the rest of the schema still loads so
    the viewer degrades gracefully on FTS5-less SQLite builds. Returns the open
    connection (caller owns its lifetime).
    """
    conn = sqlite3.connect(db_path)
    # Enforce FK constraints (off by default in SQLite).
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(_CREATE_FEEDS)
    conn.execute(_CREATE_MEDIA)
    conn.execute(_CREATE_COMMENTS)
    conn.execute(_CREATE_META)

    if fts5_available(conn):
        conn.execute(_CREATE_FEEDS_FTS)

    conn.execute(_CREATE_INDEX_FEEDS_CREATE_TIME)
    conn.execute(_CREATE_INDEX_MEDIA_FEED_ID)
    conn.execute(_CREATE_INDEX_COMMENTS_FEED_ID)

    conn.commit()
    return conn
