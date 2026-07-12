# 数据格式与查询

## 文件总览

| 文件 | 格式 | 内容 |
|------|------|------|
| `feeds.jsonl` | JSON Lines | 帖子归档（去重，跨轮幂等） |
| `comments.jsonl` | JSON Lines | 评论归档（去重，跨轮幂等） |
| `media_index.jsonl` | JSON Lines | 媒体索引（url → file/source/type/size） |
| `media/` | 二进制文件 | 下载的图片/视频（SHA256 前 16 位命名） |
| `ids.json` | 纯文本 | 帖子 ID 去重表（每行一个 ID，异步写入，不参与 hash 校验） |
| `dead_media.jsonl` | JSON Lines | 临时失效媒体队列（重试中，跨重启持久化） |
| `dead_media_permanent.jsonl` | JSON Lines | 永久失效 URL（3 次重试失败） |
| `state.json` | JSON | 守护状态快照（feeds hash、触底标记） |
| `prometheus.lock` | JSON | daemon 周期锁 + 崩溃恢复状态 |
| `prometheus.log` | 纯文本 | 运行日志（`log/prometheus/` 目录） |

## feeds.jsonl 字段（每条 80+）

| 字段 | 说明 |
|------|------|
| `id` | 帖子 ID（B_ 前缀） |
| `poster` | 作者对象（nick/icon/medal/level 等 35 子字段） |
| `createTime` / `createTimeNs` | Unix 时间戳 / 纳秒时间戳 |
| `contents` / `title` | 富文本正文 |
| `images[]` | 图片 CDN 链接 + 多分辨率 URL + 尺寸 |
| `videos[]` | 视频信息（playUrl/cover/vecVideoUrl） |
| `channelInfo` | 频道元数据（guild_name / icon / sign） |
| `total_like` / `total_collect` / `commentCount` | 互动数据 |
| `external_comment_list` | 预加载评论 |

## comments.jsonl 字段

每条记录包含 `_s`（来源标记）、`ts`（时间戳）、`d`（数据体）。`d` 包含 `feedId`、`totalNum`、`vecComment`（评论数组，每条 35+ 子字段）。

## 常用查询

```bash
# 帖子条数
wc -l data/feeds.jsonl

# 评论条数
wc -l data/comments.jsonl

# 已下载媒体
ls data/media/ | wc -l
du -sh data/media/
```

## prometheus.lock 格式

锁文件**始终存在**（release 后不删除）。所有写操作通过 temp + rename 原子完成。

| 字段 | 说明 |
|------|------|
| `pid` | 进程 PID |
| `dirty` | true=daemon 周期进行中，false=空闲 |
| `cycle` | daemon 周期编号 |
| `bottomReached` | 时间线是否已到底（底部检测后立即原子写入） |
| `pending_media` | 下载中的媒体列表 `[{url, file, expected_size}]` |
| `ts` | 最后更新时间戳 |

### 崩溃恢复行为

| 场景 | 恢复方式 |
|------|----------|
| dirty=true（周期中崩溃） | 从 lock 读 `bottomReached` + 重下 `pending_media` |
| dirty=false（空闲时崩溃） | lock 有完整的 `bottomReached`，正常启动 |
| lock 文件不存在 | fallback 到 `state.json` 的 `bottomReached` |

## state.json 格式

| 字段 | 说明 |
|------|------|
| `bottomReached` | 时间线是否到底 |
| `feeds` | 已捕获帖子数 |
| `hash` | `feeds.jsonl` 的 SHA-256（仅校验，不匹配不丢弃 state） |
| `hashFiles` | `['feeds.jsonl']`（不含 ids.json） |
| `bottomTime` | 到底时间 |

## 日志格式

文件格式：`[LEVEL] 2026-01-15T10:30:00.000Z message`

轮转规则：
- 触发条件：10MB
- 保留文件数：3 个
- 命名：`log/prometheus/prometheus.log` → `.log.1` → `.log.2` → `.log.3`
