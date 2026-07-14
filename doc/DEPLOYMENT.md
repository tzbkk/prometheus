# 部署

## 目录结构

```
prometheus/
├── conf/                         # 配置目录
│   ├── prometheus.conf.json      # QQ/inject.js 配置
│   ├── tui.conf.json             # TUI 配置
│   ├── launcher.conf.json        # Launcher 配置
│   └── viewer.conf.json          # Viewer 配置（port/interval/page_size）
├── pyproject.toml                # src layout
├── requirements.txt
├── README.md
├── doc/                          # 文档
├── scripts/                      # 可执行脚本
│   ├── setup.sh                  # 一键安装（--target=qq|viewer|all）
│   ├── start_qq.sh               # 启动 patched QQ
│   ├── start_launcher.sh         # 一键启动 launcher（推荐）
│   └── start_tui.sh              # 单独启动 TUI
├── deploy/                       # 部署配置
│   └── qq-prometheus.service     # systemd user service 模板
├── src/
│   ├── prometheus/               # 归档核心
│   │   ├── inject.js             # 注入脚本（核心）
│   │   ├── logger.js             # 日志模块
│   │   ├── lock.js               # 崩溃安全锁模块
│   │   ├── api-server.js         # QQ HTTP API 模块
│   │   ├── _envconfig.py         # JSON → 环境变量转换
│   │   └── ...                   # 其他工具模块
│   │   └── tests/                # JS 测试
│   ├── launcher/                 # 进程管理器
│   │   ├── process_manager.py    # 进程生命周期 + PR_SET_PDEATHSIG
│   │   ├── api.py                # Launcher HTTP API (127.0.0.1:9421)
│   │   └── __main__.py           # 入口：REPL + TUI 监控
│   ├── tui/                      # textual 控制台
│   │   ├── app.py                # 主应用
│   │   ├── api_client.py         # HTTP 客户端
│   │   ├── api.py                # API 封装
│   │   └── tabs/                 # 标签页组件
│   │       ├── prometheus_tab.py # QQ 控制 + 日志
│   │       ├── viewer_tab.py     # Viewer 控制 + 配置
│   │       └── tui_tab.py        # TUI 配置 + 关于
│   └── viewer/                   # Web 归档浏览器
│       ├── backend/              # Python HTTP 后端
│       │   ├── server.py         # HTTP 服务器 (127.0.0.1:9422)
│       │   ├── api.py            # API handlers (feeds/detail/search/stats/rebuild)
│       │   ├── schema.py         # SQLite schema (FTS5)
│       │   └── indexer.py        # feeds.jsonl → SQLite 增量索引
│       ├── frontend/             # React + Vite + Tailwind
│       │   ├── src/pages/        # FeedListPage / FeedDetailPage / SearchPage
│       │   ├── src/components/   # FeedCard / MediaGrid / Layout
│       │   ├── src/hooks/        # useFeeds / useFeedDetail / useSearch
│       │   └── src/lib/          # api.ts (typed client)
│       └── static/               # npm run build 输出
├── db/                           # SQLite 数据库 (gitignored)
│   └── viewer.db                 # feeds + media 索引
├── tests/                        # Python 测试
│   ├── test_launcher.py
│   ├── test_launcher_api.py
│   ├── test_viewer_schema.py
│   ├── test_viewer_indexer.py
│   └── test_viewer_api.py
├── data/                         # 运行时数据（gitignored）
├── log/                          # 日志目录 (gitignored)
│   ├── prometheus/               # inject.js 运行日志
│   ├── launcher/                 # launcher stdout/stderr
│   ├── tui/                      # TUI 日志
│   └── viewer/                   # viewer stdout/stderr
└── qq_patched/                   # AppImage 解包结果（gitignored）
```

## 新机器安装

```bash
# 1. 复制项目
scp -r prometheus/ newhost:~/Projects/

# 2. 安装 Python 依赖
pip install textual

# 3. 下载 QQ AppImage
#    https://im.qq.com/linuxqq/index.shtml

# 4. 运行安装脚本
cd ~/Projects/prometheus
bash scripts/setup.sh ~/Downloads/QQ_3.2.29_260528_x86_64_01.AppImage

#    更新 inject.js/logger.js/lock.js/api-server.js 或升级 QQ 版本时：
#    rm -rf qq_patched && bash scripts/setup.sh ~/Downloads/QQ_*.AppImage

# 5. 启动
bash scripts/start_launcher.sh
```

## 进程架构

```
launcher (Python)
├── QQ (Electron)
│   ├── start_new_session=True    独立 session
│   ├── stdin=DEVNULL             不碰终端输入
│   ├── stdout→logfile            输出到 log/launcher/qq.log
│   └── PR_SET_PDEATHSIG          launcher 死 → 内核杀 QQ
├── TUI (textual)
│   ├── 同 session               共享终端 session（收 SIGWINCH）
│   ├── TEXTUAL_DISABLE_KITTY_KEY=1  禁用 Kitty keyboard
│   └── PR_SET_PDEATHSIG          launcher 死 → 内核杀 TUI
├── Viewer (Python HTTP)
│   ├── 127.0.0.1:9422            React SPA + API + 媒体服务
│   ├── stdout→logfile            输出到 log/viewer/viewer.log
│   ├── PR_SET_PDEATHSIG          launcher 死 → 内核杀 Viewer
│   └── 后台轮询线程              定期增量索引 feeds.jsonl 新行
└── HTTP API 127.0.0.1:9421
```

launcher 被 `kill -9` 也不会留下孤儿进程：`PR_SET_PDEATHSIG` 让内核在父进程死亡时自动给子进程发 SIGTERM。

## 启动方式

### 一键启动（推荐）

```bash
bash scripts/start_launcher.sh
# launcher 启动 API + REPL（不自动启动 QQ/TUI/Viewer）
# REPL 中 r = 启动全部，s = 启动 TUI，v = 启动 Viewer
# q 退出（会杀 QQ + TUI + Viewer）
```

### 构建 Viewer 前端

```bash
# 安装 Node 依赖 + 构建 React 前端
bash scripts/setup.sh --target=viewer
# 构建产物：src/viewer/static/index.html
# 更新前端代码后重新运行此命令
```

### Viewer 控制

| 方式 | 操作 | 说明 |
|------|------|------|
| launcher REPL | `v` | 启动/停止 Viewer |
| TUI Viewer 标签页 | `v` | 切换 Viewer 启动/停止 |
| HTTP API | `POST /webapp/start` | 通过 launcher API 启动 |
| HTTP API | `POST /webapp/stop` | 通过 launcher API 停止 |

### 单独启动 QQ

```bash
bash scripts/start_qq.sh
# 直接启动 QQ，无 TUI
```

### 单独启动 TUI

```bash
bash scripts/start_tui.sh --port 9421
# 仅 TUI，需要 launcher 已在运行
```

## systemd 部署

`deploy/qq-prometheus.service` 提供了 systemd user service 模板。可根据需要启用以实现开机自动启动。
