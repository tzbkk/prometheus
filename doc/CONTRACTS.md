# 契约与测试执法指南

Prometheus 的 API 面与数据面由 `contracts/` 语料钉死，测试面由 mandate 登记册钉死。本文讲怎么用这套体系：语料怎么摆、契约怎么验、边和认领怎么读、测试怎么登记。zhizong 文法本身（文档能写哪些键、Schema 怎么编译）不在本文范围，见 zhizong 包（PyPI `zhizong>=0.4.1`）自带文档。

## 语料布局

语料根 = 仓库根（`.zhizong.yaml` 所在处），校验范围 = `contracts/`：

```
.zhizong.yaml                 # 语料命名空间声明（namespace: prometheus）
contracts/
├── structures/               # 数据结构契约（38 文档）：实体、API 载荷、标量
├── components/               # 组件契约（5 文档）：scraper/launcher/viewer/archive/deepbackfill
├── externals.yaml            # 外部源声明：qq-api（pd.qq.com）、qq-cdn（媒体 CDN）
├── samples/                  # 样本对：30 对 <Name>.valid / <Name>.invalid
└── fixtures/                 # 合成基线树：data/<guild>/ 实体树 + archives/ 合成包 + prometheus.lock
```

当前规模：43 语料文档（38 structures + 5 components）。

- **structure**：一种数据形态。三类实体（Feed/Comment/Reply）、媒体（MediaAsset）、锁（ProcessLock）、每个 API 载荷（ScraperStats、TargetList...）、若干标量（FeedId、Shard、PackageName...）。实体结构带 Location 模板（落盘路径）与 Lifecycle（rewrite-in-place / append-only / transient / generated）。
- **component**：一个进程。声明 ComponentType、Binds（地址：端口）、Runs（启动命令逐字）、Upstream（消费什么）、Downstream（提供什么）。entity_store 是实现载体，不是组件，无组件文档。archive 为 `ComponentType: cli`（无 Binds、无指令边——打包走 `scripts/archive.py`，Manifest 结构随 CLI 通道存续）。
- **external**：一个外部依赖源。组件 Upstream 里 `external:qq-api` 这类引用指向这里。

### 样本对纪律

每个负义务面的 structure 必须在 `contracts/samples/` 配一对样本：`<Name>.valid.<ext>` 必须过该结构编译出的 Schema，`<Name>.invalid.<ext>` 必须不过。义务面的判定（何时必须配对）由 zhizong 执法：file Location 的结构、被指令边引用的结构、ErrorEnvelope 必配对；其余（如 urn 身份址的 Manifest）配对自愿，但配了就同样被查。

**新结构必配对**：往语料里加 structure 而不加样本对，`zhizong validate` 直接红（结构孤儿禁令：新 structure 还必须被某组件 I/O 键或 Parameters.Type 引用，否则同样是红）。改结构不改样本，同样红。样本即该结构形态的最小活例，评审一行样本比评审整段 YAML 便宜。

### fixtures

`contracts/fixtures/` 是全仓库共用的合成数据基线：4 个实体（2 feed + 1 comment + 1 reply）+ 3 个媒体 + 1 把锁 + 1 个合成归档包。审计、归档、harness 的文档示例与测试都跑在它上面，不依赖任何真实抓取数据。

## `zhizong validate` 使用指南

安装（zhizong 经 `[contracts]` extra 引入，也是 src.archive 引擎的运行时依赖）：

```bash
pip install -e ".[contracts]"
```

本地校验（仓库根执行）：

```bash
.venv/bin/zhizong validate
```

实测输出（逐字，43 = 38 structures + 5 components）：

```
43 document(s), 0 violation(s) (0 fail, 0 warn)
```

退出码：`0` = 无 fail 级违规（warn 级不算）；`1` = 存在 fail 级违规；`2` = 用法或配置错误。

**何时跑**：任何 `contracts/` 变更后必跑，0 违规是 merge 门槛。样本对新增、结构字段调整、组件边变更，全部被它覆盖。

**CI**：test job 在 `pip install -e ".[contracts]"` 后先跑 `zhizong validate`（契约层门禁），再跑 `scripts/test_gate.py`（测试层门禁），最后 pytest。三层门禁顺序见下文 mandate 一节。

## 指令边 = API 面

组件契约里的 Upstream/Downstream 边写成形如 `GET /health` 的字符串，这条字符串就是该组件认领或提供的 API 面：

- **Downstream 边** = 本组件提供的路由。`scraper.yaml` 的 `ScraperStats: "GET /stats"` 意思是：scraper 在它的端口上提供 `GET /stats`，响应体形态 = ScraperStats 结构。
- **Upstream 边** = 本组件消费的路由（消费者在组件图中登记认领；语料内暂无认领方——声明免归位是合法态）。
- 每条 (服务, 边) 组合在语料内唯一：同一 API 面只能有一个提供者，消费者任意。
- 查询参数不入边（`?since=`、`?page=` 归行为层）；二进制路由（viewer 的 `/media/*`）不入语料，由 MediaAsset 文件契约与行为测试覆盖。

读一张组件契约的顺序：Binds 知道端口，Runs 知道启动命令，Downstream 知道它提供什么，Upstream 知道它依赖谁。组件图与端口表见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 组件认领表

谁提供、谁认领，一页看完：

| 指令边 | 提供方 | 认领方 |
|--------|--------|--------|
| `GET /targets`，`GET|POST /targets/{target}[/start|/stop|/restart]`，`GET /targets/{target}/logs` | launcher | 无（shell/API 消费） |
| `GET /config` `PUT /config`（launcher 面） | launcher | 无（同上） |
| `POST /shutdown` | launcher | 无（shell/信号链消费） |
| `GET /health` `GET /stats` | scraper | 无 |
| `GET /config` `PUT /config`（scraper 面） | scraper | 无 |
| `GET /logs` `POST /action/trigger-daemon` | scraper | 无 |
| 同族五边（Deepbackfill* 孪生文档） | deepbackfill | 无（暂无，触发走 launcher 监督目标） |
| `GET /api/feeds` `GET /api/search` | viewer | 无（SPA 自消费） |
| `GET /api/feed/{feed_id}` `GET /api/feed/{feed_id}/comments` | viewer | 无（SPA 自消费） |
| `GET /api/guilds` `GET /api/stats` `POST /api/rebuild` | viewer | 无（SPA 自消费） |

规则备注：service 之间不能互相认领（launcher 对 scraper 的健康巡检属控制面实现细节，不入图）；硬角色法下 API 声明无人认领是合法态（viewer 例）。archive 为 CLI 形，无指令边。

## 端口注册表一页图

```
127.0.0.1
:9420  scraper       noauth 采集（daemon / --once）        python -m src.web_scraper
:9421  launcher      三目标监督 + conf 单写者               python -m src.launcher [--daemon]
:9422  viewer        SQLite 索引 + 浏览 API + 媒体服务      python -m src.viewer.backend.server
:9424  deepbackfill  auth 全史采集                          python -m src.deepbackfill

（打包 CLI = scripts/archive.py，无端口。）

依赖序：launcher 守护起 → 它监督的三个服务可独立起停；监督面 = launcher shell/API。
```

## mandate 体系（测试执法）

### 登记册

`tests/manifest.yaml` 是全仓测试的唯一登记册：每个测试函数一行 mandate，六字段全必填。

```yaml
  - id: MD-075
    suite: launcher/shutdown
    name: live_shutdown_accepts_and_stops_all_targets
    asserts: 三目标运行中 POST /shutdown → 200 ShutdownAck accepted 且全部目标转 stopped…
    implements: 契约 structures/ShutdownAck.yaml …
    pillar: A
```

- `id`：MD-NNN 编号不复用（当前 111 行）。
- `suite` → 路径：`a/b/c` 映射 `tests/a/b/test_c.py`；`name` → 函数 `test_<name>`。机械映射，零自由度。
- `asserts`：一行中文，作者可审。AIGC 不得绕过登记册私增测试。
- `implements`：溯源必填，契约条款优先。
- `pillar`：三支柱 + smoke。**A** = 行为契约活体（真实服务 + 编译 Schema 断言）；**B** = 边界与 Schema（形态正负例、写者纪律）；**C** = 韧性与增长（畸形输入、重拉、晋升）。smoke = 起得来、答得了。
- 禁 parametrize、禁测试类：一个 mandate 对应恰好一个 pytest node ID，双向对等的前提。

### 双射门禁

`scripts/test_gate.py` 强制 pytest 收集集与登记册严格双向对等：

```bash
.venv/bin/python scripts/test_gate.py
```

实测输出（逐字）：

```
manifest mandates: 111
pytest collected : 111
gate: OK — bidirectional zero orphans
```

孤儿测试（有实现无登记）与空 mandate（有登记无实现）都是 exit 1。CI 中 gate 先于 pytest。

### 活体 harness

`tests/harness/` 提供两个夹具：`http`（urllib 薄客户端）与 `schema_assert`（zhizong 编译契约 + jsonschema 校验一体）。支柱 A 测试用它起真实服务（port=0 临时端口）、打真实路由、拿真实响应体过契约 Schema。服务端对错造数据不做矫正：坏形态必须能流到响应面被 Schema 抓住，这是执法有效的条件。

### 修改纪律

原子性由门禁反推：gate 在任何中间 commit 态都必须绿，所以 mandate 行、测试实现、（若涉及）被测代码必须同一个 commit。改契约则是四件套同 commit：结构 yaml + 样本对 + 受影响组件 yaml + 测试。跳步必红，红就是设计。

### 常用命令

```bash
# 全量测试
.venv/bin/python -m pytest -q          # 111 passed

# 双向门禁
.venv/bin/python scripts/test_gate.py  # gate: OK — bidirectional zero orphans

# 契约层门禁
.venv/bin/zhizong validate             # 43 document(s), 0 violation(s) (0 fail, 0 warn)

# 数据面巡检（合成基线树）
.venv/bin/python scripts/audit_tree.py --data-root contracts/fixtures
```

四条全绿是任何契约或测试变更的最小验收面。
