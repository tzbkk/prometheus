# 部署

## 目录结构

```
prometheus/
├── conf/                         # 配置目录
│   ├── prometheus.conf.json      # scraper 配置
│   ├── guilds.conf.json          # 多频道目标表
│   ├── launcher.conf.json        # launcher 配置
│   └── viewer.conf.json          # viewer 配置
├── contracts/                    # 契约语料（structures/components/samples/fixtures）
├── .zhizong.yaml                 # 语料命名空间声明
├── pyproject.toml
├── README.md
├── doc/                          # 文档
├── scripts/
│   ├── archive.py                # 归档打包 CLI（打包唯一入口，引擎 src/archive/）
│   ├── audit_tree.py               # 实体树审计（只读点名）
│   ├── setup.sh                  # Viewer 前端构建（唯一目标 --target=viewer）
│   ├── start_launcher.sh         # 一键启动 launcher（推荐）
│   └── test_gate.py              # mandate 双射门禁
├── src/
│   ├── entity_store/             # 实体树底座（writer/paths/lock/reader/scan）
│   ├── web_scraper/              # scraper（:9420）
│   ├── launcher/                 # 进程监督者（:9421）
│   ├── viewer/                   # 归档浏览器（:9422，backend + React 前端）
│   ├── archive/                  # 归档打包引擎（批处理，无端口——CLI scripts/archive.py 消费）
│   └── prometheus_version.py     # 版本读取
├── tests/                        # mandate 登记册 + 按套件目录的测试
│   └── manifest.yaml
├── db/                           # viewer SQLite 索引（gitignored，可随时重建）
├── data/                         # 实体树（gitignored）
│   └── <guild_id>/               # 每频道：feeds/ comments/ media/
├ archives/                       # 归档包输出（{guild}/packages/*.tar.zst）
└── log/                          # 各服务日志（gitignored）
```

## 新机器安装

```bash
# 1. 复制项目
scp -r prometheus/ newhost:~/Projects/

# 2. 安装 Python 依赖（含 zhizong，契约校验与 archive 打包引擎需要）
cd ~/Projects/prometheus
python -m venv .venv && . .venv/bin/activate
pip install -e ".[contracts]" pytest

# 3. 配置目标频道（conf/guilds.conf.json，或单频道走 prometheus.conf.json）

# 4. （可选）构建 Viewer 前端
bash scripts/setup.sh --target=viewer

# 5. 启动
bash scripts/start_launcher.sh
```

## 进程架构

```
launcher (Python, :9421) · 守护/客户端分离：监督守护常驻（setsid 自活），瘦客户端 shell 经 :9421 接入
├── scraper        python -m src.web_scraper            :9420
├── deepbackfill   python -m src.deepbackfill           :9424
└── viewer         python -m src.viewer.backend.server  :9422

archive = 批处理 CLI（scripts/archive.py，手动/cron）——不在监督面，无端口
```

- 每个子进程带 `PR_SET_PDEATHSIG`：launcher 被 `kill -9` 内核自动回收全部子进程。
- 各目标 stdout 重定向到 `log/` 各自子目录。
- 端口覆盖惯例统一为 `--port` CLI 旗标。

launcher 被强杀也不会留下孤儿进程。

## 启动方式

### 一键启动（推荐）

```bash
bash scripts/start_launcher.sh
# 首次运行自动拉起监督守护（--daemon，setsid 自活）；之后任何新开的
# launcher 都探活连上同一棵监督树。守护不自动启动目标。
# shell 子命令：start/stop/restart <target>, status, logs, auth, archive,
#               config, health, tail, help, quit, shutdown
# 底部状态栏实时显示三目标状态
# quit 只关本 shell——监督树照跑；shutdown 才全树关停
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
| launcher shell | `start viewer` / `stop viewer` | 启动/停止 Viewer |
| launcher API | `POST /targets/viewer/start` | 幂等启动（已运行返回 200 当前态） |
| launcher API | `POST /targets/viewer/stop` | 停止 |

## systemd 部署

各服务可直接用 systemd 跑（下例）；launcher 守护及其三目标监督面是常驻场景的推荐入口（`python -m src.launcher --daemon`）。

## Web Scraper 部署

### 独立部署

```bash
# 1. 配置 conf/guilds.conf.json（或 prometheus.conf.json 单频道字段）
# 2. 运行（daemon 是默认模式；--once 单次扫描）
python -m src.web_scraper
```

### 通过 Launcher 部署

```bash
bash scripts/start_launcher.sh
# launcher shell: start scraper
```

### systemd 服务

```ini
[Unit]
Description=Prometheus Web Scraper
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/prometheus
ExecStart=/usr/bin/python3 -m src.web_scraper
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
