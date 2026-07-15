# Prometheus - QQ 频道帖子归档

通过 AppImage 解包 + 代码注入，Hook `JSON.parse` 捕获完整 feed 数据（80+ 字段/条），下载媒体文件，捕获滚动时 QQ 预加载的评论。

**已实现**: 帖子 + 评论 + 图片/视频全部覆盖，跨轮幂等去重，守护模式自动刷新，GUI 控制台（launcher + TUI），Web 归档浏览器（Viewer）。

## 先决条件

| 条件 | 说明 |
|------|------|
| Linux + Wayland | QQ Electron 仅在 Wayland 下可靠运行（`--ozone-platform=wayland`） |
| QQ AppImage (Linux) | 从 [im.qq.com/linuxqq](https://im.qq.com/linuxqq/index.shtml) 下载 AppImage（已验证 v3.2.29）|
| 正常 QQ 不能同时运行 | patched QQ 与正常 QQ 共享 `~/.config/QQ/` → 版本冲突崩溃 |
| Python ≥ 3.8 | `start_qq.sh` 通过 `_envconfig.py` 把 JSON 配置转环境变量 |
| textual | `pip install textual`（TUI 框架，唯一额外依赖） |
| Node.js + npm | Viewer 前端构建需要（`setup.sh --target=viewer`） |
| 首次 QQ 登录 | 启动后需手机扫码登录；session 持久化在 `~/.config/QQ/`，后续重启免登录 |

## 快速使用

```bash
# 0. 克隆/复制本项目

# 0a. 配置目标频道（conf/prometheus.conf.json）
#    channel_id：打开 https://pd.qq.com → 进入目标频道 → 地址栏 pd.qq.com/g/{这一串数字}
#    channel_name：频道名（用于自动点击匹配）

# 1. 一键安装（从 AppImage 构建 patched QQ，复制 4 个 JS 模块到 qq_patched/）
bash scripts/setup.sh ~/Downloads/QQ_3.2.29_260528_x86_64_01.AppImage
#    更新 JS 或升级 QQ 时：rm -rf qq_patched && bash scripts/setup.sh ~/Downloads/QQ_*.AppImage

# 1a. 构建 Viewer 前端（需要 Node.js）
bash scripts/setup.sh --target=viewer

# 2. 启动（launcher 启动 API + REPL，不自动启动 QQ/TUI/Viewer）
bash scripts/start_launcher.sh

# 3. 首次启动需手机扫码登录 QQ（仅一次）

# 4. TUI 操作
#    Prometheus 标签页：
#      [S]tart QQ   [K]ill QQ   [R]estart   [T]rigger daemon   [V]iewer   [Q]uit
#      Ctrl+S 保存配置    ↑↓ 暂停/恢复日志滚动
#    Viewer 标签页：
#      [V] 启动/停止 Viewer    Ctrl+S 保存 Viewer 配置
#      查看状态、统计、配置编辑
#    TUI 标签页：配置编辑、关于、帮助
#    q 键随时退出 TUI → 回到 launcher REPL

# 5. launcher REPL（TUI 退出后或 launcher 直接启动后）
#    [Enter]=status  s=TUI  v=viewer  p=stop QQ  r=start all  q=quit

# 6. Web Viewer
#    浏览器打开 http://127.0.0.1:9422/
#    首页无限滚动浏览帖子，点击查看详情（含图片灯箱），搜索栏全文检索

# 7. 监控进度（不开 TUI 时）
tail -f log/prometheus/prometheus.log
```

## 停止

```bash
# 方式 1：在 TUI 中按 q → 回到 launcher REPL → 再按 q
# 方式 2：在 launcher REPL 中按 q
# 方式 3：Ctrl+C 发送 SIGINT → launcher 优雅关闭 QQ + TUI
# 方式 4（强制）：kill launcher 进程 → PR_SET_PDEATHSIG 自动杀 QQ + TUI + Viewer
```

## 架构

```
launcher (Python)
├── QQ (Electron, start_new_session + stdin=DEVNULL)
│   └── inject.js → logger.js + lock.js + api-server.js
│       HTTP API: 127.0.0.1:9420
├── TUI (textual, TEXTUAL_DISABLE_KITTY_KEY=1)
│   └── 轮询 QQ API + launcher API → dashboard
├── Viewer (Python HTTP + React)
│   ├── 127.0.0.1:9422 — SPA + API + 媒体 Range 服务
│   ├── SQLite (FTS5) 索引 feeds + media
│   └── 后台轮询增量索引 feeds.jsonl
└── HTTP API: 127.0.0.1:9421 (launcher)
```

## 文档

| 文档 | 内容 |
|------|------|
| [doc/CONFIGURATION.md](doc/CONFIGURATION.md) | 三文件配置体系、环境变量、路径配置、核心参数 |
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | 注入流程、帖子/评论捕获原理、滚动机制、守护模式、锁机制、HTTP API、Launcher、TUI、Viewer |
| [doc/DATA_FORMAT.md](doc/DATA_FORMAT.md) | 数据文件格式、字段说明、常用查询、锁格式、日志格式 |
| [doc/DEPLOYMENT.md](doc/DEPLOYMENT.md) | 目录结构、新机器安装、Viewer 部署、systemd、启动方式 |
