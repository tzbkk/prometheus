# 数据格式与查询

## 文件总览（多频道布局）

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

### 每频道目录结构

```
data/
├── prometheus.lock              # 进程锁（保持在父级，不随频道目录移动）
├── 7743321643036658/            # 频道目录（guild_id 作为 key）
│   ├── feeds.jsonl
│   ├── comments.jsonl
│   ├── ids.json
│   ├── comment_keys.json
│   ├── comments_fetched_ids.json
│   ├── media_index.jsonl
│   ├── media_index.jsonl.bak
│   ├── media/
│   │   └── <hash>.jpg
│   ├── state.json
│   ├── dead_media.jsonl
│   └── dead_media_permanent.jsonl
└── <other_guild_id>/
    └── ... (相同布局)
```

### 媒体 URL 变更（Breaking Change）

- **旧版**：`/media/<filename>` → `data/media/<filename>`
- **新版**：`/media/<guild_id>/<filename>` → `data/<guild_id>/media/<filename>`

Viewer 从 `data/<guild_id>/media/<filename>` 服务媒体文件。

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

每条记录是一个 JSON 对象，包含 `_s`（来源标记）、`ts`（时间戳）、`d`（数据体）。

### 记录类型

| `_s` | 说明 |
|------|------|
| `web_api` | 通过 pd.qq.com API 抓取的评论 |
| `vue_router` | Vue Router 状态（注入捕获，非评论数据） |
| `vq_cache` | 缓存数据 |

### web_api 评论结构

`d` 字段包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `feedId` | string | 帖子 ID |
| `totalNum` | int | 评论总数 |
| `vecComment` | array | 评论列表，每条 35+ 字段 |

`vecComment` 中每条评论的关键字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 评论 ID（`c_` 前缀 = 主评论，`r_` 前缀 = 回复） |
| `createTime` | int | Unix 时间戳（秒） |
| `postUser.nick` | string | 评论者昵称 |
| `postUser.icon.iconUrl` | string | 评论者头像 URL |
| `richContents.contents[].text_content.text` | string | 评论文本（结构化文本段落） |
| `richContents.ip_location_province` | string | IP 属地 |
| `likeInfo.count` | int | 点赞数 |
| `replyCount` | int | 回复数 |
| `sequence` | int | 显示顺序 |
| `vecReply` | array | 嵌套回复（结构同上，无 `vecReply`） |

### Viewer 索引

Viewer 启动时通过 `build_comments_incremental()` 将 `_s == "web_api"` 的记录索引到 SQLite `comments` 表。嵌套回复通过 `parent_id` 字段关联主评论。

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

## 扁平布局迁移

从旧版扁平 `data/` 迁移到每频道布局：

```bash
# 自动迁移（启动时检测旧版结构并迁移）
python -m src.web_scraper

# 手动迁移
python scripts/migrate_multi_guild.py [--data-dir <path>] [--guild-id <id>]
```

- 迁移将所有扁平文件 + `media/` 目录移入 `data/<guild_id>/`
- `prometheus.lock` 保持在父级
- 幂等操作：已迁移则无副作用
