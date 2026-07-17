"""SQLite schema for the viewer backend (feeds / feeds_fts / media / comments / meta / guilds)."""

import sqlite3

# Column-level DDL kept as module constants so T6 (ingestion) and tests can
# introspect the schema without re-parsing SQL.

_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id             TEXT PRIMARY KEY,
    guild_id       TEXT,
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
    guild_id TEXT,
    FOREIGN KEY (feed_id) REFERENCES feeds(id)
)
"""

_CREATE_COMMENTS = """
CREATE TABLE IF NOT EXISTS comments (
    id             TEXT PRIMARY KEY,
    feed_id        TEXT NOT NULL,
    guild_id       TEXT,
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

_CREATE_GUILDS = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id      TEXT PRIMARY KEY,
    guild_number  TEXT,
    name          TEXT,
    feeds         INTEGER DEFAULT 0,
    indexed_at    TEXT
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

_CREATE_INDEX_FEEDS_GUILD_ID = (
    "CREATE INDEX IF NOT EXISTS idx_feeds_guild_id ON feeds(guild_id)"
)

_CREATE_INDEX_MEDIA_GUILD_ID = (
    "CREATE INDEX IF NOT EXISTS idx_media_guild_id ON media(guild_id)"
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Add ``column`` to ``table`` via ALTER TABLE if absent (idempotent upgrade).

    On fresh DBs the column already exists in CREATE TABLE — PRAGMA finds it and
    no ALTER fires. On legacy DBs (pre-multi-guild) the column is added with NULL
    for existing rows; the indexer's rebuild (plan G11) populates it afterwards.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(db_path: str) -> sqlite3.Connection:
    """Create all viewer tables/indexes at ``db_path`` if absent (idempotent).

    FTS5 is created only when supported; the rest of the schema still loads so
    the viewer degrades gracefully on FTS5-less SQLite builds. Returns the open
    connection (caller owns its lifetime).

    For existing databases created before multi-guild support, guild_id columns
    are added in-place via ALTER TABLE (data preserved, NULL for legacy rows).
    """
    conn = sqlite3.connect(db_path)
    # Enforce FK constraints (off by default in SQLite).
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(_CREATE_FEEDS)
    conn.execute(_CREATE_MEDIA)
    conn.execute(_CREATE_COMMENTS)
    conn.execute(_CREATE_META)
    conn.execute(_CREATE_GUILDS)

    # Idempotent upgrade path: existing DBs (pre-multi-guild) get guild_id
    # columns added via ALTER TABLE. On fresh DBs these are no-ops.
    _ensure_column(conn, "feeds", "guild_id", "TEXT")
    _ensure_column(conn, "comments", "guild_id", "TEXT")
    _ensure_column(conn, "media", "guild_id", "TEXT")

    if fts5_available(conn):
        conn.execute(_CREATE_FEEDS_FTS)

    conn.execute(_CREATE_INDEX_FEEDS_CREATE_TIME)
    conn.execute(_CREATE_INDEX_MEDIA_FEED_ID)
    conn.execute(_CREATE_INDEX_COMMENTS_FEED_ID)
    conn.execute(_CREATE_INDEX_FEEDS_GUILD_ID)
    conn.execute(_CREATE_INDEX_MEDIA_GUILD_ID)

    conn.commit()
    return conn
