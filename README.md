# Prometheus - QQ 频道帖子归档

纯 HTTP 抓取 pd.qq.com 公开 API，捕获帖子、评论、回复与媒体（含评论图片），以 实体树落盘（`data/{guild}/`，sha256 分片 + `_p` 私有账本命名空间）。四个组件协作：scraper（采集）、launcher（守护/客户端分离：监督守护 + 瘦客户端 shell + auth 凭证铸造）、viewer（Web 浏览）、deepbackfill（深度回填：全史一次性回填），共用 entity_store 实体树底座；archive 为批处理打包 CLI（`scripts/archive.py`，手动/cron）。


## 先决条件

| 条件 | 说明 |
|------|------|
| Linux | 各服务以 `python -m` 直跑，无需 QQ 客户端 |
| Python ≥ 3.10 | 建议 `.venv` + `pip install -e ".[contracts]"`（archive 的 manifest 生成依赖 zhizong） |
| prompt_toolkit | launcher shell（随 `pip install -e .` 一并安装） |
| Node.js + npm | Viewer 前端构建需要（`setup.sh --target=viewer`） |

## 快速使用

```bash
# 0. 克隆/复制本项目

# 0a. 建虚拟环境并安装
python3 -m venv .venv
.venv/bin/pip install -e ".[contracts]"

# 0b. 配置目标频道（conf/prometheus.conf.json）
#    guild_number：频道唯一标识，打开 https://pd.qq.com 进入目标频道，地址栏 pd.qq.com/g/{这一串}
#    channel_id / channel_name：频道数字 ID 与名称
#    多频道：conf/guilds.conf.json，scraper 为每个频道建独立 data/<guild_id>/ 树

# 1. 构建 Viewer 前端（需要 Node.js）
bash scripts/setup.sh --target=viewer

# 2. 启动（守护/客户端分离——docker 模型）
bash scripts/start_launcher.sh
#    首次运行自动拉起监督守护（--daemon，setsid 自活——关终端不亡）；
#    之后任何新开的 launcher 都探活连上同一棵监督树。
#    守护不自动启动目标——在 shell 里 start；目标在守护下持续运行，
#    与 shell 开关无关。

# 3. launcher shell 子命令（docker 风格，Tab 补全，三目标）
#    start|stop|restart <scraper|deepbackfill|viewer>
#    logs <target>           实时查看日志（Ctrl+C 停止尾随——回提示符，不退 shell）
#    auth                    deepbackfill 网页扫码登录（自动开浏览器；服务需在跑——
#                           未跑时提示先 start deepbackfill）
#    archive [guild] <from> <to> [--apply] [--force] [--output DIR]
#                           时间窗打包（默认 dry-run 打计数；同步直调引擎；
#                           guild 可 Tab 补全自 data/；日期 UTC YYYYMMDD）
#                           裸 archive = 各 guild 数据跨度全景——可备份的时间范围一目了然
#    config show             分组全景（launcher/scraper/viewer/guilds/凭证掩码；盘-live 漂移可见）
#    config set <key> <val>  键注册表路由（launcher→守护单写者；scraper/viewer→各自 conf
#                           原子写+运行中 live 回显同步；未知键列出全部合法键与生效时机）
#    stats                   每 guild 计数 + 逐月可用日（createTime 跨度、月份直方图、days 区间编码——窗参全集）
#    health [target]         监督态 + API 探测逐行（state、restarts、运行中才探端口；
#                           原 status 已并入——裸 health = 三目标，可单探）
#    help / clear            帮助 / 清屏
#    quit                    只关本 shell——监督树照跑（新开 launcher 随时接回）
#    shutdown                全树关停（守护 + 三目标）

# 4. Web Viewer
#    浏览器打开 http://127.0.0.1:9422/
#    首页无限滚动浏览帖子，点击查看详情（含图片灯箱），搜索栏全文检索

# 5. 监控进度（不开 shell 时）
tail -f log/web_scraper/scraper.log
```

## Web Scraper（数据采集）

无需 QQ 客户端，纯 HTTP 抓取 pd.qq.com 公开 API。

```bash
# 守护模式（默认）
python -m src.web_scraper

# 单次扫描后退出
python -m src.web_scraper --once

# 端口覆盖
python -m src.web_scraper --port 9430

# 通过 launcher 启动
bash scripts/start_launcher.sh
# 在 launcher shell 中: start scraper
```

### 配置

编辑 `conf/prometheus.conf.json`:

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `guild_number` | `Takagi3channel` | 频道唯一标识（从 pd.qq.com 获取） |
| `scraper_max_workers` | `10` | 并发线程数 |
| `scraper_daemon_interval_sec` | `120` | 守护模式扫描间隔（秒） |
| `scraper_api_port` | `9420` | HTTP API 端口 |

API 面（`127.0.0.1:9420`）：`GET /health`、`GET /stats`、`GET|PUT /config`、`GET /logs`、`POST /action/trigger-daemon`。

**时间窗口径**：noauth API 仅覆盖最近 ~5.5 个月（150 页固定截断，观测日期 2026-08-25，实测边界 2026-03-08）。评论按 feed_id 直取，无时间窗，任何在库帖子永久可读。

### 数据格式（实体树）

每个频道一棵实体树，三类实体文件 + 内容寻址媒体，全部经 `src/entity_store` 原子写入：

```
data/{guild}/
├── feeds/B_{shard}/{feed_id}.json          # shard = sha256(feed_id)[:2]
├── comments/c_{shard}/{comment_id}.json    # 评论
├── comments/r_{shard}/{reply_id}.json      # 回复（与评论同树，前缀分治）
├── media/{shard}/{sha256}.{jpg|png|mp4}    # 文件名 = 内容 sha256
└── prometheus.lock                          # 全局进程锁
```

每个实体 JSON 末键恒为 `_p`（私有命名空间）：`first_seen`/`last_seen` 双时钟 + 媒体状态机（`pending` → `failed`×3 → `dead`）。腾讯原始载荷逐字保真，我方账本不混入业务字段。

详细规格见 [doc/DATA_FORMAT.md](doc/DATA_FORMAT.md)。

### 归档打包

将频道数据按时间窗打包为自包含 `tar.zst` 归档包。窗口判据 = 实体 `createTime ∈ (from, to]`（左开右闭，UTC 整日），checkpoint 引用式已废除。

CLI（手动/cron 通道）：

```bash
# dry-run：打印窗内三类实体计数，不落包（默认）
python scripts/archive.py --guild 7743321643036658 --from 20220101 --to 20260830

# 写包：{output}/{guild}/packages/<时刻>_from_<from>_to_<to>.tar.zst
python scripts/archive.py --guild 7743321643036658 --from 20220101 --to 20260830 --apply
```

**Flags**：
- `--apply`：写包（默认 dry-run）
- `--force`：覆盖已存在的输出包
- `--output DIR`：归档根目录（默认 `./archives/`）
- `--data-root DIR`：数据根目录（默认 `./data/`）
- `--level N`：zstd 压缩级别（默认 7）

**Exit codes**：

| Exit code | 说明 |
|-----------|------|
| 0 | 成功（含空窗：三类计数皆零，不落包） |
| 2 | 窗参无效（坏日期/倒序/越界未来窗） |
| 3 | 打包前对账失败（树计数不一致/引用媒体缺盘/名实不符） |
| 1 | 其他错误（未知 guild/输出已存在未 `--force`/IO 错误） |

**自包含语义**：包内目录镜像 guild 实体树切片 + 媒体并集 + `manifest.json`（窗口参数、三类实体计数、媒体清单[路径+sha256]、生成时刻、zhizong 版本戳），可独立解压查看。相同哈希的媒体文件会重复出现在不同包里（设计如此）。


### Launcher API

launcher（`127.0.0.1:9421`）是纯监督者：三目标进程管理 + 配置面。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/targets` | GET | 三目标富对象快照 |
| `/targets/{target}` | GET | 单目标详情（未知目标 → 404） |
| `/targets/{target}/start` | POST | 启动（幂等：已运行 → 200 当前态） |
| `/targets/{target}/stop` | POST | 停止（幂等） |
| `/targets/{target}/restart` | POST | 重启 |
| `/targets/{target}/logs` | GET | 日志尾部（`{"lines": [...]}`） |
| `/config` | GET | 当前配置 |
| `/config` | PUT | 部分更新，回显更新后全量 |
| `/shutdown` | POST | 优雅关停全部目标 |

错误统一为裸状态码 + `{"error": {"code", "message"}}` 信封。

## 深度回填

> **纯网页扫码登录（零部署）** =**纯网页扫码登录（零部署）** + auth 通道纯 HTTP 全量分页，
> 详见 [doc/DEEP_BACKFILL.md](doc/DEEP_BACKFILL.md)（§1 路线 W + §2）。
> deepbackfill 服务（:9424）：一次 trigger = 全史回填（auth 翻页 + 实体树写入 +
> 池化媒体 + 评论/回复补齐，自含全管线）。

### 首跑流程（全史回源·零部署）

```bash
# 0. 无需任何部署（纯网路线）

# 1. launcher shell 里一条命令：起服务 + 自动探测凭证
bash scripts/start_launcher.sh
> start deepbackfill
#    凭证缺失/失效 → 自动弹出浏览器扫码页（http://127.0.0.1:9424/auth/page，
#    二维码原图直出——不做终端渲染）→ 手机 QQ 扫码确认 → 登录成功（含 uin）回提示符
#    （等待中 Ctrl+C 安全取消——服务端二维码会话保留，可随时打开页面续扫）

# 2. 回填全自动：登录成功（或凭证本就有效）→ 服务自动点火全史回填
#    进度：GET /stats（pages/feeds/comments/replies/media/running）；604 页 ≈ 20 分钟（≥1s/页礼貌间隔）
#    手工触发面保留：POST /action/trigger-daemon（运行中再触发 = 409 busy）

# 3. p_skey 失效（小时~天量级）→ 重跑 start deepbackfill / auth 即重扫码刷新
```

API 面（`127.0.0.1:9424`）：`GET /health`（真计数）、`GET /stats`（回填进度）、
`GET|PUT /config`、`GET /logs`、`POST /action/trigger-daemon`（运行中再触发 = 409 busy；
凭证未就绪 = 409 not_configured 指向扫码页）。

纯网登录三端点（行为层）：`GET /auth/qr.png`（二维码 PNG 原图直出）、
`GET /auth/status`（`{"state": "ok"|"qr_pending"|"scanned"|"failed", "detail", "uin?", "qr_epoch"}`）、
`GET /auth/page`（内嵌 HTML 扫码页：自动轮询 + 二维码过期自动换图 cache-bust + 三态文案）。

凭证文件 `conf/deepbackfill.conf.json`（0600，gitignore，模板 `conf/deepbackfill.conf.example.json`）：
uin/p_uin/p_skey/minted_at 四键（服务端扫码成功后写入）。日志与终端
展示对 p_skey 恒掩码（首 4 末 4）。深捕凭证永远自供（自有账号、自担风险、禁止商用/
再分发归档数据——§5 合规护栏）。

## 停止

```bash
# 方式 1：launcher shell 中输入 shutdown（守护 + 全部目标优雅关停）
# 方式 2：quit / Ctrl+D 只关 shell——监督树照跑（默认语义）
# 方式 3：kill 守护进程（pgrep -f "src.launcher --daemon"）→
#         PR_SET_PDEATHSIG 自动杀全部子进程
```

## 架构

```
launcher (Python, :9421) — 守护/客户端分离：监督守护（常驻，setsid 自活）+ 瘦客户端 shell
├── scraper (python -m src.web_scraper, :9420)
│   └── daemon 抓取 → entity_store 直写（feed 重拉观测 / 评论 create-only）
├── viewer (Python HTTP + React, :9422)
│   ├── 目录扫描索引（SQLite FTS5 全文检索）
│   └── SPA + API + 媒体 Range 服务
├── deepbackfill (python -m src.deepbackfill, :9424)
│   └── 全史回填：auth 翻页（p_skey 凭据来自 ptlogin2 纯网页扫码登录——
│       start deepbackfill 自动探测，缺/失效弹出浏览器扫码，二维码原图直出）→ 实体树/媒体/评论全管线
└── entity_store (src/entity_store) — 实体树读写唯一居所（路径分片/原子写者/读取投影/启动扫描）

archive（批处理 CLI，非常驻）— scripts/archive.py 时间窗 (from, to] 打包
  └── 引擎 src/archive/engine.py 只读消费 entity_store（对 data/ 严格只读）
```

三个常驻服务由 launcher 统一监督（崩溃自愈，累计重启计数可查）。archive 为批处理 CLI，按需手动/cron 运行，不在监督面。全部磁盘读写经 `src/entity_store`，派生索引（viewer 的 SQLite）可随时从实体树再生。

## 文档

| 文档 | 内容 |
|------|------|
| [doc/CONFIGURATION.md](doc/CONFIGURATION.md) | 配置体系、环境变量、路径配置、核心参数 |
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | 组件协作、捕获原理、守护模式、HTTP API、Launcher、Viewer |
| [doc/DATA_FORMAT.md](doc/DATA_FORMAT.md) | 实体树、`_p` 规格、归档包格式、常用查询 |
| [doc/DEEP_BACKFILL.md](doc/DEEP_BACKFILL.md) | 深度回填现行方案：p_skey 获取、auth 通道全量分页 |
| [doc/DEPLOYMENT.md](doc/DEPLOYMENT.md) | 目录结构、新机器安装、Viewer 部署、systemd、启动方式 |
