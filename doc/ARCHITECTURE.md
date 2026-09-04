# 架构

Prometheus = 四个组件共用一个实体树底座（entity_store），另有批处理打包 CLI（archive）。组件之间的全部交互面由契约语料（`contracts/`）钉死：每个 HTTP 路由、每个落盘文件形态都有对应契约文档。使用与执法指南见 [CONTRACTS.md](CONTRACTS.md)，数据面规格见 [DATA_FORMAT.md](DATA_FORMAT.md)。


## 总览：四组件 over entity_store（+ archive CLI）

```
外部源（契约 externals.yaml）
  external:qq-api   https://pd.qq.com 公开 API 家族（noauth commreader / auth guild_feed_reader）
  external:qq-cdn   媒体 CDN（*.photo.store.qq.com / channel.qpic.cn）
        │ 采集                        │ 媒体下载
        ▼                            ▼
┌────────────────┐          ┌────────────────┐
│ scraper  :9420 │          │ deepbackfill   │
│ noauth 持续抓取 │          │ :9424          │
└───────┬────────┘          └───────┬────────┘
        │      write_entity()       │
        ▼                           ▼
┌─────────────────────────────────────────────┐
│              entity_store（底座）             │
│  writer · paths · lock · reader · scan       │
│  data/<guild>/{feeds,comments,media}/...     │
└───────┬─────────────────────┬───────────────┘
        │ 只读（投影 + 扫描）  │ 只读（时间窗切片）
        ▼                     ▼
┌────────────────┐    ┌──────────────────────────┐
│ viewer   :9422 │    │ archive（批处理 CLI）      │
│ 索引 + 浏览服务  │    │ scripts/archive.py 打包   │
└────────────────┘    └──────────────────────────┘
                              ▲
                ┌─────────────┴─────────────┐
                │ launcher :9421             │
                │ 监督守护 + 瘦客户端 shell   │
                └───────────────────────────┘
```

要点：

- **entity_store 是实现载体，不是组件图节点**（⑤）：组件图只画四个组件（+ archive CLI），实体树是它们的共享底座。写入只走 `write_entity()` 单入口，读取只走 reader 投影与 scan 扫描。
- **launcher 是纯监督者**：不承担打包、不碰数据面。监督三目标（守护/客户端分离）：scraper、deepbackfill、viewer。archive 不在监督面。
- **archive 是批处理 CLI**：打包是批处理操作，不是常驻服务——引擎 `src/archive/engine.py` + 入口 `scripts/archive.py`（手动/cron），对 data/ 严格只读。
- **监督面 = launcher 守护 + 瘦客户端 shell**：shell 经 :9421 守护 API 消费监督动词（start/stop/restart/status/logs/auth/archive/config/health/tail/quit/shutdown）；viewer 无认领（SPA 自消费），deepbackfill 暂无认领。

### 端口注册表

| 端口 | 组件 | Runs（契约逐字） | 状态 |
|------|------|------------------|------|
| 9420 | scraper | `python -m src.web_scraper` | 在产 |
| 9421 | launcher | `python -m src.launcher [--daemon]` | 在产 |
| 9422 | viewer | `python -m src.viewer.backend.server` | 在产 |
| 9424 | deepbackfill | `python -m src.deepbackfill` | 在产 |

（archive 是批处理 CLI，无端口。）

监听地址一律 127.0.0.1。端口覆盖惯例统一为 `--port` CLI 旗标，config 键为次优先级。全部路由、认领与契约文档名的对照表见 [CONTRACTS.md](CONTRACTS.md)。

## entity_store：共享底座

| 模块 | 职责 | 关键纪律 |
|------|------|----------|
| `paths.py` | Location 模板构造 + 逆解析 | 纯函数零 IO；畸形 id/路径 fail loud |
| `writer.py` | `write_entity()` 唯一写入口 | 新见/重拉/回填三路共用；媒体合并只填不删；写前晋升 failed→dead |
| `lock.py` | ProcessLock 5 字段读写 | 存在即活动；stale = pid 不存活；缺字段 fail loud |
| `reader.py` | 八投影函数 + 严格加载 | 投影是原体唯一合法读取视图；不 mutate 文档 |
| `scan.py` | 启动扫描 + 时间窗迭代 | 宽容遍历（skip+log），纯内存零落盘 |
| `scripts/audit_tree.py` | 逐文件点名审计 | 只报告不修复；违例非零退出 |

设计原则：严格解析禁多路回退（fail loud）、派生态即算即用不落盘、时钟由调用方注入。完整规格见 [DATA_FORMAT.md](DATA_FORMAT.md)。

## 服务 API 面（契约指令边一览）

指令边 = 契约中写成形如 `GET /health` 的 Upstream/Downstream 边。下表是每组件的完整边集（与 `contracts/components/*.yaml` 逐字对齐，命名空间结构文档省略）。

### launcher :9421（进程监督）

| 边 | 语义 |
|----|------|
| `GET /targets` | 三目标快照（富对象数组：state/pid/uptime/restarts） |
| `GET /targets/{target}` | 单目标状态 |
| `POST /targets/{target}/start` `…/stop` `…/restart` | 幂等启停重启：重复 start 已运行目标返回 200 当前态 |
| `GET /targets/{target}/logs` | 日志尾（tail 查询参数归行为层，不入边） |
| `GET /config` `PUT /config` | launcher 单写者、原子落盘；PUT 合并后回显完整对象 |
| `POST /shutdown` | 优雅关停全部目标 |

`{target}` 之家 = TargetName 三枚举：scraper/deepbackfill/viewer。未知目标与未知路由返回裸状态码 + ErrorEnvelope。

### scraper :9420 与 deepbackfill :9424（采集，同族平行）

| 边 | 语义 |
|----|------|
| `GET /health` | 健康检查（真计数） |
| `GET /stats` | 计数（feed/comment/reply/media + gateway_rejects 僵尸绿灯防线），verbatim 直通不做类型矫正 |
| `GET /config` `PUT /config` | scraper 侧 PUT 为 echo-only 合并，下次启动生效；guilds 键只读（400） |
| `GET /logs` | 日志缓冲 |
| `POST /action/trigger-daemon` | 手动触发扫描（异步） |

两服务除凭据面（noauth commreader vs auth guild_feed_reader）与 feed 清单获取器外完全同构。

### viewer :9422（归档浏览器）

| 边 | 语义 |
|----|------|
| `GET /api/feeds` | 分页列表（page/size 为查询参数，不入边） |
| `GET /api/search` | 全文检索（q 为查询参数） |
| `GET /api/feed/{feed_id}` | 帖子详情 |
| `GET /api/feed/{feed_id}/comments` | 评论列表（CommentList 只出 `c_` 条目，回复计入 stats） |
| `GET /api/guilds` | guild 列表 |
| `GET /api/stats` | 摄取摘要 + 库元数据 |
| `POST /api/rebuild` | 全量重建索引，异步受理（RebuildAck） |

实现要点：SQLite(FTS5) 是派生索引，可随时从实体树全量再生（周期性全量重建）。媒体路由 canonical 三段 `/media/<guild>/<shard>/<file>`，shard = 内容寻址名前 2 hex；另有两段兼容路由经索引反查。二进制 `/media/*` 不入语料契约，由 MediaAsset 文件契约与行为测试覆盖。

## 契约、实现、测试：三层执法

1. **契约层**：`contracts/` 语料是唯一规格源：43 语料文档（38 structures + 5 components），另有 externals 声明与 fixtures/samples 支撑面。`.venv/bin/zhizong validate` 每次变更必跑，0 违规是 merge 门槛。契约钉什么（Location/字段/枚举/指令边）由 zhizong 文法定义，见 [CONTRACTS.md](CONTRACTS.md)。
2. **实现层**：组件的 Binds/Runs/边集被契约钉死后，实现只剩行为自由度。契约不钉的实现细节（查询参数、tail 语义、重建时机）在组件 docstring 声明。
3. **测试层**：三道门禁。
   - `scripts/test_gate.py`：pytest 收集集与 `tests/manifest.yaml` mandate 登记册双向对等，孤儿测试与空 mandate 都过不了 CI。
   - mandate 行按三支柱分类：A 行为契约活体（真实服务 + Schema 断言）、B 边界与 Schema、C 韧性与增长，外加 smoke。
   - 活体 harness（`tests/harness/`）起真实服务（port=0 临时端口）用编译自契约的 JSON Schema 断言响应体。服务对错造数据不做矫正，坏形态必须能流到响应面被断言抓住，否则执法失效。

三层一起保证：改契约必炸测试或 validate，绕过测试必炸 gate。修改纪律（mandate 行、测试、实现同 commit）见 [CONTRACTS.md](CONTRACTS.md)。

## 归档打包

### 时间窗

窗口 = 纯日期 `(from, to]`，左开右闭，UTC。快照语义：三类实体各自按自身 `createTime` 判窗（deepbackfill 老帖按真实发布日落窗），不是按抓取时刻。窗口边界就是两个日期。

校验全在引擎侧：畸形日历、倒序、未来窗一律拒绝（exit 2），不做吸附夹取。`from == to` 合法（单日窗）。空窗（三类计数皆零）不落包，exit 0。

### 包格式

自包含 `tar.zst`，内部镜像 guild 实体树布局，首成员恒为 `manifest.json`：

```
$ tar --zstd -tf <package>.tar.zst
manifest.json
comments/c_20/c_1a2b….json
comments/r_7d/r_0f9e….json
feeds/B_df/B_9d8c….json
feeds/B_fa/B_5f2e….json
```

包自包含意味着：同内容媒体跨包重复出现（设计如此）；解压后可独立浏览（镜像树布局，viewer/audit 可直接指向解压目录）。

### manifest 六要素

薄钉六要素（DECISION-2），其余键宽容面随实现演进：

| 要素 | 形态 |
|------|------|
| `window.from` | UTC YYYYMMDD 八位数字串（不含） |
| `window.to` | UTC YYYYMMDD（含） |
| `counts` | `{feeds, comments, replies}` 窗口内三类实体计数（打包前对账基准） |
| `media` | `[{path, sha256}]` 媒体并集清单，path 为包内相对路径，sha256 与文件名摘要段一致 |
| `created_at` | 包生成时刻（UTC ISO 8601 秒精度） |
| `zhizong_version` | 打包时 zhizong 运行时版本戳 |

### 包命名

```
package ::= YYYYMMDDTHHMMSSZ(_full | _from_<YYYYMMDD>_to_<YYYYMMDD>)
```

全量包 = 创建时刻 + `_full`；增量包 = 创建时刻 + 窗口段。窗口已编码进文件名，免读 manifest 即可定窗。

### CLI 单入口

打包唯一入口 = CLI（`scripts/archive.py`，手动/cron 通道；原 :9423 服务面已拆除）。引擎流程：`iter_window` 产出窗口快照，随后对树内每个实体文件做对账（在窗或严格证窗外，损坏/引用媒体缺盘/名实不符一律失败），再写包。对 data/ 严格只读。

实测（跑在合成 fixtures 上，dry-run 与落包）：

```bash
.venv/bin/python scripts/archive.py --guild 1000000000000001 \
    --from 20260630 --to 20260701 \
    --data-root contracts/fixtures/data --output /tmp/prometheus-doc-demo
```

```
window (20260630, 20260701] guild 1000000000000001: feeds=2 comments=1 replies=1 media=0
DRY RUN — no package written. Use --apply to create it.
```

加 `--apply` 落包（时刻段随运行时刻变）：

```
wrote /tmp/prometheus-doc-demo/1000000000000001/packages/20260831T131432Z_from_20260630_to_20260701.tar.zst (693 bytes)
```

窗参无效的两种拒绝（exit 2）：

```
ERROR: inverted window: from '20260701' is later than to '20260630' (window is half-open (from, to])
```

```
ERROR: future window: to '20301231' lies beyond the current UTC date 20260831
```

### Exit codes

| 码 | 语义 |
|----|------|
| 0 | 成功，或空窗（不落包） |
| 2 | 窗参无效（畸形日历 / 倒序 / 未来窗） |
| 3 | 打包前对账失败（树计数不一致 / 引用媒体缺盘 / 内容 sha256 与文件名摘要段不符） |
| 1 | 其他（未知 guild / 输出已存在未 `--force` / IO 错误） |

## launcher：纯监督者

- **监督面**：三目标统一 `start/stop/restart`，进程表含 state（stopped/running/failed 三态）、pid、uptime、restarts。failed 态可观测：在管进程退出且非用户 stop，monitor 一秒内记 failed。
- **进程隔离**：子进程 `PR_SET_PDEATHSIG`，launcher 被 `kill -9` 内核自动回收全部子进程；各目标 stdout 重定向到 `log/` 各自子目录。
- **配置**：launcher 是自身 conf 的单写者，PUT /config 原子落盘；guilds 恒读 `conf/guilds.conf.json`（API 面只读）。
- **shell**：launcher 客户端自带交互 shell（docker 风格子命令），经 :9421 守护 API 消费——守护常驻（setsid 自活），shell 只是可随时弃开的瘦客户端。
