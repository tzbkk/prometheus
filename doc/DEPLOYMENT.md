# 部署

## 目录结构

```
prometheus/
├── conf/                         # 配置目录
│   ├── prometheus.conf.json      # QQ/inject.js 配置
│   ├── tui.conf.json             # TUI 配置
│   └── launcher.conf.json        # Launcher 配置
├── pyproject.toml                # src layout
├── requirements.txt
├── README.md
├── doc/                          # 文档
├── scripts/                      # 可执行脚本
│   ├── setup.sh                  # 一键安装（解包 AppImage + 复制 JS 模块）
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
│   └── tui/                      # textual 控制台
│       ├── app.py                # 主应用
│       ├── api_client.py         # HTTP 客户端
│       ├── api.py                # API 封装
│       └── tabs/                 # 标签页组件
├── tests/                        # Python 测试
│   ├── test_launcher.py
│   ├── test_launcher_api.py
│   ├── test_api_client.py
│   └── test_tui_api.py
├── data/                         # 运行时数据（gitignored）
├── log/                          # 日志目录 (gitignored)
│   ├── prometheus/               # inject.js 运行日志
│   ├── launcher/                 # launcher stdout/stderr
│   └── tui/                      # TUI 日志（预留）
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
│   ├── start_new_session=True    独立 session
│   ├── TEXTUAL_DISABLE_KITTY_KEY=1  禁用 Kitty keyboard
│   └── PR_SET_PDEATHSIG          launcher 死 → 内核杀 TUI
└── HTTP API 127.0.0.1:9421
```

launcher 被 `kill -9` 也不会留下孤儿进程：`PR_SET_PDEATHSIG` 让内核在父进程死亡时自动给子进程发 SIGTERM。

## 启动方式

### 一键启动（推荐）

```bash
bash scripts/start_launcher.sh
# launcher 自动启动 QQ + TUI
# TUI 退出后进入 launcher REPL
# REPL 中 q 退出（会杀 QQ）
```

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
