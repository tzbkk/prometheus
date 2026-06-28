# QQ 频道评论抓取研究

日期: 2026-06-30 — 2026-07-01

## 目标

全量捕获 QQ 频道（guild/channel）中所有帖子的评论数据。

## 现状

| 指标 | 数值 |
|------|------|
| 总帖子 | ~8,500 |
| commentCount > 0 的帖子 | ~5,900 |
| 可获取评论的帖子 | ~1,700 (29%) |
| 实际评论行数 | ~7,200 |
| 评论期望总量（commentCount 累计） | ~34,000 |
| 估算实际评论数（扣除已删除/机器人） | ~12,000 |
| 实际覆盖率（按估算修正） | ~60% |

## 传输架构

QQ 使用两套网络传输层，对应两个后端服务：

```
                                ┌─────────────────┐
                                │   QQ 服务端      │
                                └────────┬────────┘
                           ┌─────────────┴─────────────┐
                           │                           │
                    ┌──────▼──────┐           ┌────────▼────────┐
                    │  MSF SDK    │           │   HTTPS (fetch)  │
                    │ wrapper.node│           │  commreader API  │
                    │ 二进制协议   │           │  JSON over HTTP  │
                    └──────┬──────┘           └────────┬────────┘
                           │                           │
              ┌────────────┴────────────┐              │
              │                         │              │
        ┌─────▼─────┐           ┌──────▼──────┐       │
        │ GetGuild  │           │ fetchComments│       │
        │ Feeds     │           │ (6 args)     │       │
        │ (贴列表)  │           │ (评论获取)   │       │
        └───────────┘           └──────┬───────┘       │
                                       │                │
                                  ┌────▼────┐    ┌─────▼──────┐
                                  │ 返回真实 │    │ 返回空      │
                                  │ 评论数据 │    │ vecComment  │
                                  └─────────┘    └────────────┘
```

| 传输层 | 协议 | API 路径 | 评论返回 | 我们可拦截 |
|--------|------|----------|----------|-----------|
| MSF SDK | 自定义二进制 (TCP) | `guild_feed_reader.ComReader/GetGuildFeeds`<br>`NodeIKernelGuildService.fetchComments()` | ✅ 有数据 | ❌ JS 不可达 |
| HTTPS | JSON over HTTP | `commreader.ComReader/GetFeedComments` | ❌ 空 vecComment | ✅ fetch hook |

## 已尝试方案

### 1. 直接 HTTPS 调用 (commreader.GetFeedComments)

**做法**：用捕获的 OIDB header + bkn + cookies 直接 POST 到 `pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.commreader.ComReader/GetFeedComments`

**结果**：❌ 全部返回 `retcode: 0` 但 `vecComment: []`。QQ 自身 web 客户端的请求同样返回空——该端点已被服务端废弃。

### 2. noauth 路径

**做法**：切换 URL 从 `gotrpc/auth/` → `gotrpc/noauth/`（参考 RSSHub 项目）

**结果**：❌ 无效果，仍返回空。

### 3. 慢速滚动触发预加载

**做法**：逐帖慢速滚动（2.5s/帖），让 `vue-recycle-scroller` 有时间渲染每个帖子，触发 QQ 的 MSF SDK 评论预加载。

**结果**：❌ 遍历全部 8,500 帖后，评论覆盖率仅 29%（与快速滚动相同）。QQ 的 MSF SDK 只预加载约 30% 帖子（活跃/近期帖子）的评论，其余 70% 无论滚多慢都不会触发。

### 4. Hook wrapper.node 原生接口

**做法**：
- `Module._load` hook 拦截 wrapper.node 的 require 调用
- Proxy 递归包装导出对象以记录方法调用
- 全局 scope 扫描和 require cache 探测

**结果**：❌ wrapper.node 不在 require cache 中。它由 `major.node` 通过 C++ `dlopen` 加载，完全绕过 Node.js 模块系统。JS 层无法访问 `NodeIKernelGuildService.fetchComments()`。

证实数据：
- require cache 中只有 1 个 `.node` 文件：`major.node`
- 全局 scope 无 wrapper/session/kernel 相关对象
- `Module._load` hook 从未被 wrapper.node 触发

### 5. VR 导航逐帖加载

**做法**：通过 Vue Router 导航到每个帖子详情页（`/g/<guildId>/post/<postId>`），2s 后返回。期望 QQ 在打开详情页时通过 MSF SDK 加载评论。

**结果**：❌ 导航成功执行（5,916 帖），但评论数据不走 `JSON.parse`。帖子详情页和频道列表页使用不同的渲染路径；详情页的评论响应不经过 JSON.parse hook。

## wrapper.node 分析

通过 `strings` 分析 wrapper.node 二进制文件，发现关键 N-API 导出：

```
NodeIQQNTWrapperSession:
  getAlbumService() → NodeIKernelAlbumService
  getGuildMsgService() → NodeIKernelGuildService

NodeIKernelGuildService:
  fetchComments(6 args)        ← 我们需要的方法
  fetchLatestComments(4 args)
  decodeMVPFeedsRspPb

NodeIKernelFeedService:
  getChannelTimelineFeeds(1 arg)
  getNextPageReplies(1 arg)
  getExternalComments
  preloadGuildFeeds
```

这些接口通过 `wrapper.node` 的 N-API 绑定暴露给 C++ 代码，但不经过 Node.js require 系统。从 JS 层直接调用的路径不存在。

## OIDB 认证

OIDB header 是 session-based（非 per-request），确认来自 NapCatQQ 源码分析。不同 API 使用不同 command 码：

| API | command | service_type |
|-----|---------|-------------|
| GetGuildFeeds | 0x93df | 13 |
| GetFeedComments | 0x110d | 16 |
| GetFeedDetail | 0x10f4 | 12 |

OIDB header 不是评论抓取的瓶颈——`GetFeedComments` 的 HTTPS endpoint 本身返回空。

## 结论

目前 QQ 频道评论**无法全量捕获**。根本限制在于：

1. QQ 服务端已废弃 `GetFeedComments` HTTPS 端点（commreader），所有调用者均返回空
2. MSF SDK 原生接口（guild_feed_reader / fetchComments）仅在 QQ 自己决定预加载时调用，覆盖约 30% 帖子
3. wrapper.node 原生接口从 JS 层无法访问（C++ dlopen 加载）
4. 帖子详情页的评论渲染路径不同于频道列表页，JSON.parse hook 不适用

**当前策略**：接受约 30% 帖子 + 60% 实有评论的覆盖率。已覆盖社区核心讨论帖（热门帖评论数中位数 4，缺失帖中位数 1）。
