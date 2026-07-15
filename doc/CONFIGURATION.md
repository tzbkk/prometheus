# 配置

三个独立配置文件在 `conf/` 目录，各组件读自己的文件，互不干扰。

## 文件结构

| 文件 | 消费者 | 关键字段 |
|------|--------|----------|
| `conf/prometheus.conf.json` | inject.js（QQ 进程内） | 频道、滚动、守护、API、日志 |
| `conf/launcher.conf.json` | launcher（Python 进程管理器） | 端口、重启策略 |
| `conf/tui.conf.json` | TUI（textual 终端界面） | 轮询间隔 |

## conf/prometheus.conf.json

```json
{
  "channel_id": "7743321643036658",
  "channel_name": "擅长捉弄的高木同学",
  "daemon_mode": true,
  "daemon_interval_ms": 120000,
  "api_port": 9420,
  "log_level": "INFO",
  "log_buffer_lines": 500,
  "api_version": "1"
}
```

### 消费方式

- **bash 脚本** 通过 `eval "$(python3 src/prometheus/_envconfig.py)"` 把 JSON 转成 `PROMETHEUS_*` 环境变量
- **inject.js** 被复制进 AppImage，无法读 JSON，只读 `process.env.PROMETHEUS_*`
- 环境变量优先级最高：`PROMETHEUS_CHANNEL_ID=xxx bash scripts/start_qq.sh`

### 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `channel_id` | — | 目标频道 guild_id（必填）。获取：打开 `pd.qq.com` → 进入目标频道 → 地址栏 `pd.qq.com/g/{id}` 中的数字 |
| `channel_name` | — | 频道显示名（用于自动点击匹配） |
| `scroll_max_iterations` | 50000 | 最大滚动次数上限 |
| `daemon_mode` | true | 守护模式 |
| `daemon_interval_ms` | 120000 | 守护间隔（2 分钟） |
| `api_port` | 9420 | QQ HTTP API 端口 |
| `log_level` | INFO | ERROR/WARN/INFO/DEBUG |
| `log_buffer_lines` | 500 | 内存环形缓冲行数 |
| `startup_sequence` | [...] | QQ 启动后自动操作序列（快速启动设为 `[]`） |

### 路径配置

`data_dir` / `patched_dir` / `appimage` 支持 `null`、相对路径、`~`。`null` = fallback 到项目内默认值。

| 配置键 | 默认值（null 时） | gitignored |
|---|---|---|
| `data_dir` | `<project>/data` | ✓ |
| `patched_dir` | `<project>/qq_patched` | ✓ |

## conf/launcher.conf.json

```json
{
  "launcher_port": 9421,
  "max_restarts": 5,
  "restart_delay": 5,
  "qq_start_script": "scripts/start_qq.sh",
  "start_tui": true,
  "api_version": "1"
}
```

| 参数 | 说明 |
|------|------|
| `launcher_port` | Launcher HTTP API 端口 |
| `max_restarts` | TUI 崩溃后最大自动重启次数 |
| `start_tui` | launcher 启动时是否同时启动 TUI |

## conf/tui.conf.json

```json
{
  "poll_interval": 1,
  "api_version": "1"
}
```

 | 参数 | 说明 |
|------|------|
| `poll_interval` | TUI 向 QQ/launcher API 轮询数据的间隔（秒） |
| `api_version` | 预期 QQ API 版本，不匹配时 TUI 显示警告横幅 |

## Web Scraper 配置

以下配置项在 `conf/prometheus.conf.json` 中设置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `guild_number` | string | `Takagi3channel` | 频道唯一标识，用于 GetFeedComments API |
| `scraper_max_workers` | int | `10` | ThreadPoolExecutor 并发线程数 |
| `scraper_daemon_interval_sec` | int | `120` | 守护模式扫描间隔（秒） |
| `scraper_api_port` | int | `9420` | Scraper HTTP API 端口（与 QQ legacy 互斥） |

### guild_number 获取方式

1. 打开 https://pd.qq.com
2. 进入目标频道
3. 打开浏览器开发者工具 → Network
4. 查找 GetFeedComments 请求
5. 在请求体中找到 `channelSign.guild_number` 的值
