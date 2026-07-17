# 工作原理

## QQ 版本

| 项目 | 值 |
|------|-----|
| AppImage | 任意版本（已验证 3.2.29-49738） |
| 解包目录 | `qq_patched/` |
| package.json | 保留原始版本号，仅修改 `main` 字段指向 `prometheus.js` |

`setup.sh` 动态读取 AppImage 的原始版本号，不再硬编码或伪装版本。同一个 `patched` 目录可以对应任意版本 AppImage。

## 注入流程

1. `scripts/setup.sh` 解包 AppImage → 修改 `package.json` 入口为 `prometheus.js`，复制 4 个 JS 模块（inject + logger + lock + api-server）到 `qq_patched/`
2. `prometheus.js` 在 QQ 主进程加载时：
   - 自动切换到频道视图（改 `location.hash`）
   - 自动点击目标频道
   - 直接操纵 `scrollTop` 触发 `vue-recycle-scroller` 懒加载

## 帖子捕获

- Hook `JSON.parse` → 扫描所有经过 V8 的 feed 对象（`B_` 前缀 + `createTime`/`poster`/`title`）
- **Guild 过滤**：`saveFeed` 检查 `channelInfo.sign.guild_id === CFG.channelId`
- 跨轮幂等：`capturedIds` Set + `ids.json` 持久化
- 数据写入 `data/feeds.jsonl`

## 滚动机制

QQ 频道列表用 `vue-recycle-scroller`。Electron 的 `sendInputEvent` 对该组件无效。通过 `wc.executeJavaScript()` 直接操纵 DOM：

```js
target.scrollTop = target.scrollHeight;
target.dispatchEvent(new WheelEvent('wheel', {deltaY: 100000, bubbles: true, cancelable: true}));
```

### RAF Polyfill

GNOME Mutter / Wayland 在窗口遮挡时不发送 `wl_surface.frame` 回调 → Chromium 的 `requestAnimationFrame` 不触发 → 滚动停止。修复：`dom-ready` 时用 `setTimeout(fn, 0)` 替代 RAF。

### 到底探测

每 50 次迭代 probe 一次。若 `scrollHeight` 连续 30 次探针无增长，或 QQ UI 出现"没有更多"文本 → 判定到底。**`bottomReached` 立即通过 `lock.setBottomReached(true)` 原子写入 lock 文件**，任何崩溃都不丢失。

## 评论捕获

1. Vue Router 导航首帖 → QQ 发起 `GetFeedComments` → fetch hook 截获 `x-oidb` 认证头部
2. 剩余帖子直接 fetch + 捕获的 OIDB 头部
3. bkn 从 `[PC]` 消息的实际 API URL 中提取

评论覆盖率 ~30%（受限于 QQ MSF SDK 预加载机制）。

## 守护模式

启动后等待初始滚动到底，然后每 `daemon_interval_ms`（默认 120s）一轮：
1. 滚到顶 → 等 10s 扫新帖
2. 发现 `commentCount > 0` 且未 traversed 的帖子 → 标记 traversed
3. 每 6 轮 re-poll 最近 10 帖
4. 滚到底 → 写 `state.json`

## 锁机制

`data/prometheus.lock` 是 daemon 周期锁 + 崩溃恢复状态存储。

### 原子写入

所有写操作通过 `_atomicWrite()`：写 `.tmp` → `rename`（POSIX 原子）。崩溃时文件要么是旧版完整内容，要么是新版完整内容，不会截断。

### 生命周期

| 阶段 | 操作 |
|------|------|
| daemon 周期开始 | `acquire()` → `dirty=true`，保留上次 `bottomReached` |
| 周期中 | `addPendingMedia()` / `removePendingMedia()` 更新下载队列 |
| 周期结束 | `release()` → `dirty=false`，`pending_media` 清空，**文件保留不删** |
| 底部首次到达 | `setBottomReached(true)` → 立即原子写入 |

### 崩溃恢复

启动时 `checkAndRecover()` 发现 `dirty=true`：
- 从 lock 文件读 `bottomReached`（不依赖 state.json）
- 重下 `pending_media` 中断的媒体
- **任何崩溃都不会导致重滚**

### 并发保护

`acquire()` 检查 lock 中的 PID：只有 `dirty=true` 且 PID 存活时才 throw。`dirty=false` 的残留 lock（上次正常 release）可以安全覆盖。

## State Hash

`state.json` 存储 `feeds.jsonl` 的 SHA-256 hash，用于检测数据文件是否被外部修改。

- `STATE_FILES = ['feeds.jsonl']`（不含 `ids.json`——它异步写入会导致假 mismatch）
- `readState()` 在 hash 不匹配时**不丢弃 state**（feeds.jsonl 是 append-only，增长是正常行为）
- hash 不匹配只记 DEBUG 日志，`bottomReached` 照常可用

## HTTP API

### QQ API (inject.js, Node.js http, 127.0.0.1:9420)

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/logs?since=N&max=M` | GET | 日志条目 |
| `/stats` | GET | feeds/comments/media/daemon 状态 + `last_scan_ts` |
| `/config` | GET | 读取当前配置 |
| `/config` | PUT | 修改配置 |
| `/action/trigger-daemon` | POST | 手动触发守护扫描 |

### Launcher API (Python http.server, 127.0.0.1:9421)

| 端点 | 方法 | 说明 |
|------|------|------|
| `/start` | POST | 仅启动 QQ（不碰 TUI） |
| `/stop` | POST | 停止 QQ |
| `/restart` | POST | 同步重启 QQ（含健康检查） |
| `/status` | GET | 进程状态 + 重启计数 + `qq_pid`/`scraper_pid` |
| `/shutdown` | POST | 关闭所有进程 |

## Launcher

Python 标准库进程管理器（`src/launcher/`），依赖 prompt_toolkit 提供交互 shell：

- **进程隔离**：QQ 使用 `start_new_session=True`（独立 session，`stdin=DEVNULL` + `stdout=logfile` 不碰终端）；TUI 与 launcher 共享 session（接收 SIGWINCH 以支持终端 resize）
- **PR_SET_PDEATHSIG**：子进程 fork 后调 `prctl(PR_SET_PDEATHSIG, SIGTERM)`，launcher 死了（含 `kill -9`）内核自动杀子进程
- **后台 monitor 线程**：每秒调 `pm.monitor()` 检测进程崩溃并自动重启（`max_restarts=5`）；TUI 干净退出（rc=0）不触发重启
- **TUI 独占终端**：shell 主线程 `proc.wait()` 阻塞等待 TUI 退出，不碰 stdin
- **终端恢复**：TUI 退出后 → `reset` → `tcflush(TCIFLUSH)` 清残留输入
- **子命令 shell**：`prompt_toolkit` PromptSession + WordCompleter + bottom_toolbar 实时状态栏；CommandParser 纯函数解析，Dispatcher 加锁调用 ProcessManager

## TUI 控制台

基于 textual 框架（`src/tui/`），`TEXTUAL_DISABLE_KITTY_KEY=1` 禁用 Kitty keyboard protocol（避免退出时转义序列泄漏到 stdin）：

- **两个标签页**：Prometheus（状态/控制/日志/配置/倒计时）+ TUI（设置/帮助）
- **退出机制**：`on_key` 拦截 'q' 调 `self.exit()`（textual 正常退出流程，自动恢复终端）
- **DISCONNECTED 横幅**：QQ API 不可用时显示，恢复后自动消失
- **PID 变化检测**：QQ 或 scraper restart 后 `_last_log_seq` 重置为 0，确保新日志能显示
- **@work(thread=True)**：kill/restart HTTP 操作在后台线程，不阻塞事件循环

## 日志系统

四级日志（ERROR/WARN/INFO/DEBUG）：

- `log_level` 配置控制（默认 INFO）
- 内存环形缓冲（`log_buffer_lines`，默认 500），按 `entry.seq` 过滤（非数组索引）
- 文件双写 + 10MB 自动轮转（保留 3 个旧文件）
- 文件格式：`[LEVEL] ISO_TIMESTAMP message`
- 日志目录：`log/prometheus/prometheus.log`

## 启动参数

`--disable-background-networking`：阻止 Chromium 后台网络更新。QQ hotUpdate 触发 `setLauncherCounts` → Network Service 崩溃。此参数消除崩溃。

`--no-sandbox`：WSL2 / Wayland 下 Electron GPU sandbox 不可用。

## 限制

- QQ 的 CDP / V8 inspector 被编译期移除
- asar 文件有完整性校验，但通过 `require()` 从外部入口加载不受影响
- Feed API 走 `wrapper.node` 私有协议，`JSON.parse` hook 在传输层之上截获
- 评论 API（`GetFeedComments`）已被 QQ 服务端废弃，返回空
- wrapper.node 由 C++ dlopen 加载，不经过 require → JS 层无法 hook
- 正常 QQ 与 patched QQ 共享 `~/.config/QQ/` → 版本冲突

## Viewer — Web 归档浏览器

Viewer 是 launcher 管理的第三个子进程，提供 Web 界面浏览已归档的帖子。

### 架构

```
Viewer (Python HTTP, 127.0.0.1:9422)
├── API (server.py → api.py)
│   ├── GET  /api/feeds?page=&size=      分页列表（含首张图片）
│   ├── GET  /api/feed/:id               帖子详情（原图 URL 匹配、过滤缩略图变体）
│   ├── GET  /api/feed/:id/comments      评论列表（含嵌套回复、作者、IP、点赞）
│   ├── GET  /api/search?q=&page=        全文检索（LIKE 匹配 title_text + raw_json）
│   ├── GET  /api/stats                  feed/media 数量 + DB 大小
│   └── POST /api/rebuild                手动增量索引
├── 媒体服务 (server.py)
│   ├── GET  /media/:filename            原图/视频（Range 支持，206 Partial Content）
│   └── 路径遍历防护、流式传输
├── 静态文件 (server.py)
│   └── React SPA → src/viewer/static/index.html
├── SQLite (db/viewer.db)
│   ├── feeds      帖子全文 + 作者 + 统计
│   ├── feeds_fts  FTS5 全文索引（已弃用，搜索改用 LIKE）
│   ├── media      媒体文件映射（url → local file）
│   ├── comments   评论（含嵌套回复 parent_id、作者、IP 属地、点赞）
│   └── meta       last_offset / indexed_at
├── 索引器 (indexer.py)
│   ├── build_all()              全量重建
│   ├── build_incremental()      增量追加（last_offset 跟踪）
│   ├── build_comments()         评论全量索引（从 comments.jsonl）
│   ├── build_comments_incremental() 评论增量索引（last_comment_offset 跟踪）
│   └── 启动时自动跑一次增量 + 后台轮询（poll_interval=30s）
└── 前端 (React + Vite + Tailwind + React Query)
    ├── /               FeedListPage（无限滚动、30s 自动刷新）
    ├── /feed/:id       FeedDetailPage（图片灯箱、视频播放）
    └── /search?q=      SearchPage（复用 FeedCard）
```

### API 端口

| 端口 | 服务 | 说明 |
|------|------|------|
| 9420 | QQ HTTP API | inject.js 内嵌 |
| 9421 | Launcher HTTP API | 进程管理 |
| 9422 | Viewer HTTP API | Web 归档浏览器 |

### 配置

`conf/viewer.conf.json`：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| port | 9422 | 监听端口 |
| db_path | db/viewer.db | SQLite 路径 |
| data_dir | data | 数据目录（feeds.jsonl, media/） |
| static_dir | src/viewer/static | React 构建产物 |
| poll_interval | 30 | 增量索引间隔（秒，0=禁用） |
| page_size | 20 | 每页帖子数 |

### 控制方式

| 方式 | 操作 | 说明 |
|------|------|------|
| launcher shell | `start viewer` / `stop viewer` | 启动/停止 Viewer |
| TUI Viewer 标签页 | `v` | 开关 Viewer |
| launcher API | `POST /webapp/start` | 启动 |
| launcher API | `POST /webapp/stop` | 停止 |
| launcher API | `GET /webapp/status` | 查询状态 |

### 设计决策

- **LIKE 代替 FTS5**：FTS5 unicode61 不分词 CJK，中文子串搜索（如"高木"）无结果。LIKE 在 8745 条规模上性能足够。
- **缩略图过滤**：QQ 每张图有 `picUrl`（原图）+ `vecImageUrl[]`（3-4 级缩略图）。详情 API 仅返回匹配 `images[].picUrl` 的本地文件。
- **头像过滤**：作者头像 URL（`qlogo.cn`）被媒体下载器捕获，feed 列表缩略图和详情媒体列表均过滤排除。
- **多频道浏览**：Viewer 扫描所有 `data/*/feeds.jsonl` 目录，即使配置中已删除频道仍可浏览历史数据（G16）。
- **媒体 URL 路由**：媒体路由改为双段 `/media/<guild_id>/<filename>`，对应 `data/<guild_id>/media/<filename>`（Breaking change）。

## Web Scraper 架构（prometheus-neo）

```
web_scraper (Python)
├── client.py — QQWebClient: pd.qq.com HTTP API (urllib stdlib)
│   ├── GetGuildFeeds (service_type=12) — 分页拉取帖子
│   ├── GetFeedComments (service_type=5) — 评论
│   └── Cookie session + exponential backoff retry
├── feeds.py — FeedsScraper: 分页循环 + guild_id 过滤
├── comments.py — CommentsScraper: ThreadPoolExecutor 并发
├── media.py — MediaDownloader: SHA256+扩展名命名 + 原子写入
│   ├── urlnorm.py — 剥离 QQ CDN volatile 参数 (dis_k, dis_t) 后 hash
│   ├── 加载 media_index.jsonl → _seen 去重集合 (normalized URL)
│   ├── 加载 dead_media_permanent.jsonl → _dead 跳过集合
│   └── 零字节文件自动重下
├── store.py — Store: 线程安全 + 去重 (ids.json + comment_keys.json)
│   └── comments_fetched_ids.json — feed_id→last_count 追踪评论增长
├── lock.py — Lock: 进程锁 + stale PID 检测 + 崩溃恢复
├── api_server.py — APIServer: HTTP API (ThreadingHTTPServer, health/stats/config/logs)
│   └── trigger-daemon 异步执行（后台线程），daemon_running 时拒绝重复触发
├── daemon.py — Daemon: 定期扫描 + 分页回填 + 评论增长检测
│   ├── 每个 cycle 从最新页往回翻 (最多 50 页), 遇已抓 feed 停
│   ├── 对比 live commentCount vs stored → 涨了就重新抓评论
│   ├── 每 cycle 轮转回查 50 个旧帖 (3 线程, 防止 QQ API 限流)
│   └── 每个 cycle 写 state.json (SHA256 hash of feeds.jsonl)
└── __main__.py — 入口: 装配组件 + 日志缓冲 + 进程锁
    HTTP API: 127.0.0.1:9420 (与 QQ legacy 互斥)
```

### 多频道抓取流程

**Scraper 多频道流程：**
- 单进程、单守护进程，每轮循环顺序遍历所有配置的频道
- 每个频道拥有独立的 `GuildContext`（client、store、scrapers、per-guild recheck cursor）
- 全局 rate Semaphore 限制所有频道的聚合 API 并发（防止超额请求）
- 容错构造：某个频道失败（网络/地理封锁）会跳过，其他频道继续执行
- 数据布局：`data/<guild_id>/` 每个频道独立目录，进程锁保持在父级 `data/prometheus.lock`
- 统计信息：`/stats` API 返回 per-guild breakdown + 顶层总数

**Viewer 频道发现：**
- `discover_guilds(data_dir)` 扫描 `data/*/feeds.jsonl` — 所有存在 feeds 的数字目录（即使配置中已删除仍可浏览）
- 名称增强：索引时从 `conf/guilds.conf.json` 读取频道名称写入 SQLite `guilds` 表（运行时不再读配置）
- 每频道增量 offset 追踪（B2）
- 媒体 URL：`/media/<guild_id>/<filename>`（双段路由）

**自动迁移（B1）：**
- 启动时检测旧版扁平 `data/` → 自动迁移到 `data/<guild_id>/feeds.jsonl`（针对首个/legacy 频道）
- 幂等操作：已迁移则无操作

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | `{"ok":true,"data":{"status":"ok","scraper":"running"}}` |
| `/stats` | GET | feeds/comments/media 计数 + daemon 状态 |
| `/logs` | GET | 日志缓冲（`?since=N&max=M`，按 `entry.seq > N` 过滤） |
| `/config` | GET/PUT | 查看/修改配置 |
| `/action/trigger-daemon` | POST | 手动触发扫描（异步，立即返回） |

### 进程安全

- **进程锁** (`prometheus.lock`): acquire 时写 `dirty:true` + PID, release 时 `dirty:false`
- **stale PID 检测**: 死 PID 的 lock 自动覆盖, 活 PID 的 lock 阻断双开
- **崩溃恢复**: 启动时发现 dirty lock → log warning
- **stdout 重定向**: launcher 启动 scraper 时重定向到 `log/web_scraper/scraper.log`, 不污染 TUI

### 媒体下载

- **命名**: `SHA256(normalized_url)[:16] + ".jpg"` / `".mp4"` (与 legacy 完全一致)
- **URL 正则化**: QQ 视频 CDN 的 `dis_k`/`dis_t` 鉴权参数每次请求都变 → 剥离后 hash 才稳定, 防止同一视频反复下载
- **去重**: 启动时加载 `media_index.jsonl` → `_seen` 集合, stats 立即显示完整数量
- **零字节重下**: `filepath.stat().st_size > 0` 检查, 空文件自动重新下载
- **永久放弃列表**: 3 次重试失败的 URL 写入 `dead_media_permanent.jsonl`, 永不再试
- **捡漏**: daemon 从 API 获取 live feed (含 legacy 未捕获的视频 URL), 发现文件不在磁盘就下

### 评论增长检测

1. **最新页**: `get_feeds(7,"")` 返回 live `commentCount` → 对比 `comments_fetched_ids.json` 记录的上次数 → 涨了就重抓
2. **旧帖**: 每 cycle 轮转回查 50 个 (3 线程), 全量覆盖 ~4 小时 → batch 调 GetFeedComments → Store 去重兜底

### 与 Legacy 的关系

| 特性 | Legacy (inject.js) | Web Scraper |
|------|-------------------|-------------|
| 依赖 | QQ AppImage | 无（纯 HTTP） |
| 数据格式 | feeds.jsonl | 相同 |
| 评论抓取 | 滚动捕获 (仅 2/307 真实) | API 分页 (5730 真实) |
| 媒体下载 | QQ 缓存复制 | CDN 下载 + 磁盘对账 |
| 进程锁 | lock.js (dirty+PID) | lock.py (dirty+PID) |
| 状态文件 | state.json | 相同 |
| HTTP API | 9420 | 9420（互斥） |
| 稳定性 | 依赖 QQ 版本 | web API 可能随时停用 |
