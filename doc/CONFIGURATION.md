# 配置

配置文件在 `conf/` 目录，各组件读自己的文件，互不干扰。

## 文件结构

| 文件 | 消费者 | 关键字段 |
|------|--------|----------|
| `conf/prometheus.conf.json` | scraper（唯一运行时读者） | 频道、并发、守护间隔、API 端口、data_dir |
| `conf/guilds.conf.json` | scraper（多频道目标表）、viewer（guild 名） | guild_id/guild_number/name |
| `conf/launcher.conf.json` | launcher（进程监督） | 端口、重启策略、目标端口 |
| `conf/viewer.conf.json` | viewer | 端口、db 路径、分页 |
| `conf/deepbackfill.conf.json` | deepbackfill（auth 通道凭据，0600，gitignore） | uin/p_uin/p_skey/minted_at 四键 |

## conf/prometheus.conf.json

scraper 实际读取的键（`src/web_scraper/config.py`）：

```json
{
  "channel_id": "7743321643036658",
  "channel_name": "擅长捉弄的高木同学",
  "guild_number": "Takagi3channel",
  "scraper_max_workers": 10,
  "scraper_daemon_interval_sec": 120,
  "scraper_api_port": 9420,
  "data_dir": null
}
```

### 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `channel_id` | — | 目标频道 guild_id（必填）。获取：打开 `pd.qq.com` → 进入目标频道 → 地址栏 `pd.qq.com/g/{id}` 中的数字 |
| `guild_number` | — | 频道唯一标识 slug，用于 GetFeedComments API |
| `scraper_max_workers` | 10 | 评论/媒体并发线程数 |
| `scraper_daemon_interval_sec` | 120 | 守护模式扫描间隔（秒）；daemon 是默认模式，`--once` 单次 |
| `scraper_api_port` | 9420 | Scraper HTTP API 端口；`--port` CLI 旗标可覆盖 |
| `data_dir` | `<project>/data` | 实体树根目录；相对路径按项目根解析，支持 `~` |

### guild_number 获取方式

1. 打开 https://pd.qq.com
2. 进入目标频道
3. 打开浏览器开发者工具 → Network
4. 查找 GetFeedComments 请求
5. 在请求体中找到 `channelSign.guild_number` 的值

## conf/launcher.conf.json

```json
{
  "launcher_port": 9421,
  "max_restarts": 5,
  "restart_delay": 5
}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `launcher_port` | 9421 | Launcher HTTP API 端口；`--port` CLI 旗标可覆盖 |
| `max_restarts` | 5 | 目标崩溃后最大自动重启次数 |
| `restart_delay` | 5 | 重启间隔（秒） |
| `deepbackfill_port` | 9424 | deepbackfill 服务端口（监督命令注入 `--port`） |
| `scraper_api_port` | 9420 | shell `health` 命令的健康检查目标端口 |


## conf/deepbackfill.conf.json（深度回填凭证，0600）

deepbackfill 的 auth 通道凭据（纯网页扫码登录，零部署）。**推荐经 launcher shell 的 `auth` 动词生成**（服务端自动
弹出浏览器扫码页 `http://127.0.0.1:9424/auth/page`，二维码 PNG 原图直出 →
手机 QQ 扫码 → 四键凭证 + 0600 落盘一步完成）；手工填模板
（`conf/deepbackfill.conf.example.json`）为兜底。真文件 gitignore，永不入库。

| 字段 | 类型 | 说明 |
|------|------|------|
| `uin` | string | QQ 号（扫码产物） |
| `p_uin` | string | `o<uin>` 形（扫码产物） |
| `p_skey` | string | pd.qq.com 域 44 字符 web 凭证（扫码产物；有效期小时~天量级，失效重跑 `auth` 重扫） |
| `minted_at` | integer | 凭证写入时刻 epoch ms |

环境变量 `PROMETHEUS_DEEPBACKFILL_CONF` 可覆写路径（测试/多实例用）。
凭证卫生：文件权限 0600；日志与终端展示对 `p_skey` 恒掩码（首 4 末 4）。
现行登录路线见 [doc/DEEP_BACKFILL.md](DEEP_BACKFILL.md)。

## conf/guilds.conf.json（多频道支持）

用于配置多个频道抓取目标，scraper 会为每个频道创建独立实体树 `data/<guild_id>/`。

### 文件格式

```json
{
  "_comment": "Multi-guild scrape targets. Scraper writes to data/<guild_id>/.",
  "guilds": [
    {"guild_id": "7743321643036658", "guild_number": "Takagi3channel", "name": "擅长捉弄的高木同学"}
  ]
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `guild_id` | string | 频道数字 ID（必填），从 `pd.qq.com/g/{guild_id}` 地址栏获取 |
| `guild_number` | string | 频道唯一标识 slug，用于 GetFeedComments API |
| `name` | string | 频道显示名（用于 Viewer 显示） |

### 配置文件解析顺序

1. scraper 会在 `prometheus.conf.json` 所在目录查找 `guilds.conf.json`（遵循 `PROMETHEUS_CONFIG` 环境变量）
2. 若未找到，fallback 到项目默认位置 `conf/guilds.conf.json`
3. 若 `guilds.conf.json` 不存在，scraper 会从 `prometheus.conf.json` 的 `channel_id`/`guild_number`/`channel_name` 字段构建单个频道（兼容旧版配置）

### 验证规则

- `guild_id` 必须为非空数字字符串
- 缺少 `guild_id` 或 `guild_number` 的条目会被跳过（记录警告日志）

### 注意事项

- **不支持热重载**：添加/删除频道需编辑配置文件并重启 scraper
