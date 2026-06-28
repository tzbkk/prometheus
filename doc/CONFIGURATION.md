# 配置

所有可调参数集中在 `prometheus.conf.json`（项目根）。改一个文件即可换目标频道、调滚动速度、换路径——无需碰代码。

## 配置文件

```json
{
  "channel_id": "7743321643036658",
  "channel_name": "擅长捉弄的高木同学",
  "scroll_delta_y": -500,
  "scroll_interval_ms": 800,
  "scroll_max_iterations": 50000,
  "daemon_mode": true,
  "daemon_interval_ms": 300000,
  "startup_sequence": [
    {"action": "switch_guild", "delay_ms": 6000},
    {"action": "switch_guild", "delay_ms": 15000},
    {"action": "click_channel", "delay_ms": 18000},
    {"action": "click_channel", "delay_ms": 28000},
    {"action": "click_channel", "delay_ms": 38000},
    {"action": "start_scroll", "delay_ms": 45000},
    {"action": "click_channel", "delay_ms": 55000}
  ]
}
```

## 三种消费方式

- **Python 模块** 直接 `from prometheus import config; config.get("channel_id")`
- **bash 脚本** 通过 `eval "$(python3 src/prometheus/_envconfig.py)"` 把 JSON 转成 `PROMETHEUS_*` 环境变量
- **inject.js**（被复制进 AppImage，无法读 JSON）只读 `process.env.PROMETHEUS_*`，默认值与 JSON 对齐

环境变量优先级最高，适合一次性覆盖不修改文件：`PROMETHEUS_CHANNEL_ID=xxx bash scripts/start_qq.sh`。`PROMETHEUS_CONFIG=/path/to/other.json` 可换整个配置文件。

## 路径配置

路径类配置（`data_dir` / `patched_dir` / `appimage`）支持 `null`、相对路径、`~` 三种写法。**`null` = fallback 到项目内默认值**，保证所有产出物都落在 `<project>/` 下，不污染 `$HOME`。相对路径会 anchor 到项目根。

| 配置键 | 默认值（null 时） | 用途 | gitignored |
|---|---|---|---|
| `data_dir` | `<project>/data` | feed 归档、去重表、日志 | ✓ |
| `patched_dir` | `<project>/qq_patched` | AppImage 解包结果（~500MB） | ✓ |
| `output_dir`* | `<project>/output` | CLI 产出的 archive.db、markdown/、media/ | ✓ |

\* `output_dir` 不可配置，由 `config.output_dir()` 固定为 `<project>/output`；CLI `--output` 可在运行时覆盖。

## 核心参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `channel_id` | — | 目标频道 guild_id（必填） |
| `channel_name` | — | 频道显示名（用于自动点击匹配） |
| `scroll_delta_y` | -500 | 滚动幅度（负数=向下） |
| `scroll_interval_ms` | 800 | 滚动间隔（毫秒） |
| `scroll_max_iterations` | 50000 | 最大滚动次数上限 |
| `daemon_mode` | true | 守护模式（每 N 毫秒自动刷新） |
| `daemon_interval_ms` | 300000 | 守护间隔（默认 5 分钟） |
