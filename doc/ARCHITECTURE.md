# 工作原理

## QQ 版本

| 项目 | 值 |
|------|-----|
| AppImage | 任意版本（已验证 3.2.29-49738） |
| 解包目录 | `qq_patched/` |
| package.json | 保留原始版本号，仅修改 `main` 字段指向 `prometheus.js` |
| `~/.config/QQ/versions/config.json` | 初始化为空 `{}`，QQ 自动写入正确版本 |

`setup.sh` 动态读取 AppImage 的原始版本号，不再硬编码或伪装版本。同一个 `patched` 目录可以对应任意版本 AppImage。

## 注入流程

1. `scripts/setup.sh` 解包 AppImage → 修改 `package.json` 入口为 `prometheus.js`
2. `prometheus.js` 在 QQ 主进程加载时：
   - 自动切换到频道视图（改 `location.hash`）
   - 自动点击目标频道
   - 直接操纵 `scrollTop` 触发 `vue-recycle-scroller` 懒加载

## 帖子捕获

- Hook `JSON.parse` → 扫描所有经过 V8 的 feed 对象（`B_` 前缀 + `createTime`/`poster`/`title`）
- **Guild 过滤**：`saveFeed` 检查 `channelInfo.sign.guild_id === CFG.channelId`，拒绝非目标频道的 feed（QQ 启动时会加载推荐/通知内容，不过滤会污染数据）
- 跨轮幂等：`capturedIds` Set + `ids.json` 持久化，同 ID 不重复写
- 数据通过 `console.log('[P]' + JSON)` 传回主进程 → 写入 `data/feeds.jsonl`

## 滚动机制

QQ 频道列表用 `vue-recycle-scroller`（Vue 虚拟列表库）。Electron 的 `sendInputEvent({type:'mouseWheel'})` **对该组件完全无效**——250 次迭代后 `scrollTop` 仍为 0。

**解决方案**：通过 `wc.executeJavaScript()` 直接操纵 DOM 的 `scrollTop` + 派发合成 `WheelEvent`：

```js
target.scrollTop = target.scrollHeight;
target.dispatchEvent(new WheelEvent('wheel', {deltaY: 100000, bubbles: true, cancelable: true}));
target.dispatchEvent(new Event('scroll', {bubbles: true}));
```

每次迭代直接跳到 `scrollHeight` 底部，派发大幅 `WheelEvent` 模拟用户滚动 → QQ 父组件检测到接近底部 → 调用 `wrapper.node` 加载更多 feed → `JSON.parse` hook 捕获。QQ 异步消化分页请求（可能有短暂 stall，但会追上来）。

### RAF Polyfill

GNOME Mutter / Wayland 合成器在 QQ 窗口被遮挡或位于非活跃 workspace 时**停止发送 `wl_surface.frame` 回调**。Chromium 的 `requestAnimationFrame` 依赖此回调触发。没有 RAF，`vue-recycle-scroller` 无法处理滚动事件 → 懒加载停止。

**修复**：在 Vue 初始化前（`dom-ready` 时）用 `setTimeout(fn, 0)` 替代 `requestAnimationFrame`：

```js
requestAnimationFrame = function(cb) { return setTimeout(function() { cb(performance.now()) }, 0) };
cancelAnimationFrame = function(id) { clearTimeout(id) };
```

此后 scroller 完全不依赖合成器状态。测试：8492 feeds 在窗口完全遮挡时 13 分钟到底，速度 ~650/min。

**到底探测**：每 50 次迭代 probe 一次 DOM 状态。若 `bottomRatio = (scrollTop + clientHeight) / scrollHeight > 0.985` **且** `scrollHeight` 连续 30 次探针无增长，或 QQ UI 出现"没有更多"等文本 → 判定到底，停止滚动。

## 评论捕获

### 三步流程

1. **Vue Router 导航首帖** → QQ 自身代码发起 `GetFeedComments` → fetch hook 截获 `x-oidb` 认证头部
   - OIDB 头部捕获后，后续导航通过 `window.__prom_comment_oidb` 检查**自动跳过**（原来固定导航 3 次，现在只需 1 次）
2. **剩余帖子直接 fetch** + 捕获的 OIDB 头部 → `credentials: 'include'` 携带 cookies
3. **bkn** 从 `[PC]` 消息的实际 API URL 中提取

### feedId 关联

`GetFeedComments` API 响应不含 `feedId`，评论无法直接关联到帖子。解决方式：
- JSON.parse hook 递归扫描时追踪父级 feed 对象的 `id`（`cfid` 参数逐层传递）
- daemon fetch 调用 `__prom_getComments(postId, guildId)` 前设置 `window.__prom_fetching_pid = postId`，JSON.parse hook 读此变量作为 fallback

### daemon 遍历

daemon 启动时只将 `commentCount === 0` 的帖子标记为 traversed（无需抓评论）。`commentCount > 0` 的帖子由 daemonLoop 发现后标记 traversed，每 6 轮 re-poll 最近 10 帖。

去重：按排序后的 comment ID 集合为 key，`capturedCommentKeys` Set + 启动时加载 `comments.jsonl` 保证跨轮幂等。

### 评论捕获限制

QQ 评论存在两个传输路径：
1. HTTPS `GetFeedComments`（`commreader.ComReader`）— 返回空 vecComment，已被 QQ 服务端废弃
2. MSF SDK 二进制协议（`wrapper.node` → `NodeIKernelGuildService.fetchComments()`）— QQ 滚动时预加载 ~30% 帖子的评论

已尝试的方案：
- **noauth 路径**：切换 `gotrpc/auth/` → `gotrpc/noauth/`，无效果
- **慢速滚动**：每帖 2.5s 停留触发预加载，覆盖率不提升（QQ 不预加载 ~70% 帖子）
- **hook wrapper.node**：JS 层无法访问（C++ dlopen 加载，不经过 require cache）
- **VR 导航**：每帖打开详情页触发 MSF 加载，但评论不走 JSON.parse（死路）

当前策略：接受 ~30% 评论覆盖率（已覆盖社区核心讨论帖）。

## 守护模式

启动后 ~2.5 分钟自动开始，每 5 分钟一轮：
1. 等初始滚动到底（`timelineBottomReached`）后才启动，避免与初始滚动冲突
2. 滚到顶 → 等 10s 扫新帖
3. 发现 `commentCount > 0` 且未 traversed 的帖子 → 标记 traversed
4. 每 6 轮额外 re-poll 最近 10 帖
5. 滚到底 → probe 是否有新帖

`timelineBottomReached` 标志避免重复全量滚动。bottom=true 后只做增量扫描。评论仅通过滚屏时 QQ 自身的 MSF 预加载捕获，不主动抓取。

## 启动参数

`--disable-background-networking`：阻止 Chromium 后台网络更新。QQ 的 hotUpdate 机制在启动后约 30s 触发 `setLauncherCounts` → Network Service 崩溃 → SIGTRAP → FATAL。此参数消除崩溃。

`--no-sandbox`：WSL2 / Wayland 下 Electron GPU sandbox 不可用，不加则 GPU 进程无法启动。

## 媒体下载

- **递归扫描** feeds/comments 中所有 CDN URL（`qpic.cn` / `myqcloud.com` / `photo.store` / `qlogo` / `qchannelvideo`）
- **`electron.net.request`** 下载 → 自动携带 session cookies → 鉴权视频 URL 无需额外处理
- **去重**：URL SHA256 前 16 位命名 + `mediaSeen` Set + 启动时加载 `media_index.jsonl`
- **失效重试**：下载 0 字节或网络错误 → 写入 `dead_media.jsonl`（持久化），每 daemon 周期重试同一 URL，最多 3 次；超过后写入 `dead_media_permanent.jsonl`，启动时加载到 `mediaSeen` 永久跳过
- 产出：`data/media/<hash>.<ext>` + 索引 `data/media_index.jsonl`（`source` 字段可追溯到帖子/评论 ID）

## 限制

- QQ 的 CDP / V8 inspector 被编译期移除，无法用标准 DevTools
- asar 文件有完整性校验，直接修改会破坏签名；但通过 `require()` 从外部入口加载不受影响
- Feed API 走 `wrapper.node` 私有协议（非 HTTPS），无法用 SSL key log 拦截；但 `JSON.parse` hook 在传输层之上截获，不依赖 SSL
- 评论 API（`GetFeedComments`）走 HTTPS 返回空 vecComment —— 该端点已被 QQ 服务端废弃，所有调用者（包括 QQ 自身 web 客户端）均返回空
- wrapper.node 由 major.node 通过 C++ dlopen 加载，不经过 Node.js require → 无法从 JS 层 hook 直接调用 `NodeIKernelGuildService.fetchComments()`
- VR 导航是唯一可靠获取评论的方式，但每帖 3s 限速（5h / 5893 帖）
- 正常 QQ (3.2.29) 和 patched QQ (3.2.28) 共享 `~/.config/QQ/` 时互相覆写配置文件 → 版本冲突崩溃，必须串行使用
- 正常 QQ 与 patched QQ 共享 `~/.config/QQ/` 会互相覆写配置 → 版本冲突崩溃，必须串行使用
- QQ hotUpdate 在 ~30s 后触发 `setLauncherCounts` → Network Service 崩溃 → SIGTRAP → FATAL；`--disable-background-networking` 修复
- ~~ydotool 补充滚动~~ → Wayland 下 ydotool 滚当前焦点窗口，QQ 失焦时会滚到 GNOME；`autoscroll.py` 已不推荐使用
- ~~GNOME Mutter 遮挡/非活跃 workspace 时停止 `wl_surface.frame` 回调 → `requestAnimationFrame` 不触发 → 滚动停止~~ → 已用 RAF polyfill（`setTimeout` 替代）解决
