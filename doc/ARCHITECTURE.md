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
| `/status` | GET | 进程状态 + 重启计数 |
| `/shutdown` | POST | 关闭所有进程 |

## Launcher

Python 标准库进程管理器（`src/launcher/`），零额外依赖：

- **进程隔离**：QQ 使用 `start_new_session=True`（独立 session，`stdin=DEVNULL` + `stdout=logfile` 不碰终端）；TUI 与 launcher 共享 session（接收 SIGWINCH 以支持终端 resize）
- **PR_SET_PDEATHSIG**：子进程 fork 后调 `prctl(PR_SET_PDEATHSIG, SIGTERM)`，launcher 死了（含 `kill -9`）内核自动杀子进程
- **TUI 独占终端**：launcher 主线程 `proc.wait()` 阻塞等待 TUI 退出，不碰 stdin
- **终端恢复**：TUI 退出后 → `reset`（不捕获输出，让转义序列到终端）→ `tcflush(TCIFLUSH)` 清残留输入
- **REPL**：`input("> ")` 标准行缓冲，有回显和 readline 支持

## TUI 控制台

基于 textual 框架（`src/tui/`），`TEXTUAL_DISABLE_KITTY_KEY=1` 禁用 Kitty keyboard protocol（避免退出时转义序列泄漏到 stdin）：

- **两个标签页**：Prometheus（状态/控制/日志/配置/倒计时）+ TUI（设置/帮助）
- **退出机制**：`action_quit()` 调 `driver.stop_application_mode()`（textual 自己的终端恢复）→ `os._exit(0)`（不等 event loop / worker 线程）
- **DISCONNECTED 横幅**：QQ API 不可用时显示，恢复后自动消失
- **PID 变化检测**：QQ restart 后 `_last_log_seq` 重置为 0，确保新日志能显示
- **@work(thread=True)**：kill/restart HTTP 操作在后台线程，不阻塞事件循环

## 日志系统

四级日志（ERROR/WARN/INFO/DEBUG）：

- `log_level` 配置控制（默认 INFO）
- 内存环形缓冲（`log_buffer_lines`，默认 500）
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
