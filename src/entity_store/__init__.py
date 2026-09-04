"""prometheus 实体库（entity store）。

对外露：
- paths：实体/媒体路径分片、构造、逆解析（契约 Location 模板，fail loud，纯函数）。
- lock：瞬态进程锁读写 + stale-pid 读侧判定（ProcessLock 契约，5 字段全钉）。
- writer：原子实体写者（序列化纪律 + _p 生命周期 + media 合并/晋升 + backfill 时钟冻结）。
- reader：读取投影库（六投影 + feed 侧 title_of/poster_of
  + 严格加载——投影的唯一合法居所，消费者强制 import 此处）。
- scan：派生索引启动扫描（一机制两用：per-feed comment/reply 计数 + dead-URL 集，纯内存）
  + (from,to] createTime 时间窗迭代器（archive 消费）。
"""

from src.entity_store.lock import (
    LockFormatError,
    ProcessLockData,
    is_stale,
    lock_path,
    read_lock,
    write_lock,
)
from src.entity_store.paths import (
    PathFormatError,
    ParsedEntity,
    comment_dir,
    comment_path,
    feed_dir,
    feed_path,
    media_dir,
    media_path,
    resolve,
    shard_of,
)
from src.entity_store.writer import (
    DEAD_PROMOTION_RETRIES,
    WriterError,
    write_entity,
)
from src.entity_store.reader import (
    ReaderError,
    author_of,
    created_at_of,
    kind_of,
    load_entity,
    media_of,
    poster_of,
    target_of,
    text_of,
    body_of,
    title_of,
)
from src.entity_store.scan import (
    ScanResult,
    WindowEntry,
    iter_window,
    scan,
)

__all__ = [
    "LockFormatError",
    "ProcessLockData",
    "is_stale",
    "lock_path",
    "read_lock",
    "write_lock",
    "PathFormatError",
    "ParsedEntity",
    "comment_dir",
    "comment_path",
    "feed_dir",
    "feed_path",
    "media_dir",
    "media_path",
    "resolve",
    "shard_of",
    "DEAD_PROMOTION_RETRIES",
    "WriterError",
    "write_entity",
    "ReaderError",
    "author_of",
    "created_at_of",
    "kind_of",
    "load_entity",
    "media_of",
    "poster_of",
    "target_of",
    "text_of",
    "body_of",
    "title_of",
    "ScanResult",
    "WindowEntry",
    "iter_window",
    "scan",
]
