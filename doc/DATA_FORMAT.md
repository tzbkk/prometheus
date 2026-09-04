# 数据格式（实体树）

数据面 = 每频道一棵实体树（一个实体一个 JSON 文件）+ 内容寻址媒体 + 一把全局进程锁。没有 append-only 日志，没有派生索引文件：计数、去重、死集、索引全部是扫描时即算即用的内存投影（原则 3：派生态不落盘）。

本文规格与两处权威源逐字对齐：`contracts/structures/*.yaml`（契约）与 `src/entity_store/`（模块 docstring）。两者冲突时以契约为准，请报 issue。

## 目录布局

```
data/
├── prometheus.lock                          # 全局进程锁（全系统唯一的非实体数据文件）
├── <guild_id>/                              # 每频道一树，目录名 = 频道 ID
│   ├── feeds/
│   │   └── B_<shard>/<feed_id>.json         # 帖子实体
│   ├── comments/
│   │   ├── c_<shard>/<comment_id>.json      # 主评论实体
│   │   └── r_<shard>/<reply_id>.json        # 回复实体
│   └── media/
│       └── <shard>/<sha256>.<ext>           # 内容寻址媒体（无字母前缀）
└── <other_guild_id>/                        # 相同布局
```

注意媒体分片目录没有 `B_`/`c_`/`r_` 字面前缀，桶是裸两位 hex。前缀只属于实体目录。

合成基线树在仓库内可审计：`contracts/fixtures/data/<guild>/`（4 实体 + 3 媒体 + 1 锁），下文命令块都跑在它上面。

## 三类实体的路径模板与分片规则

权威规格 = 各结构契约的 Location 模板（`contracts/structures/{Feed,Comment,Reply,MediaAsset}.yaml`），实现居所 = `src/entity_store/paths.py`。

| 实体 | Location 模板 | 分片（shard 段） |
|------|--------------|------------------|
| Feed | `data/{guild}/feeds/B_{feed_shard}/{feed_id}.json` | `sha256(feed_id)` 十六进制摘要前 2 位 |
| Comment | `data/{guild}/comments/c_{comment_shard}/{comment_id}.json` | `sha256(comment_id)` 前 2 位 |
| Reply | `data/{guild}/comments/r_{reply_shard}/{reply_id}.json` | `sha256(reply_id)` 前 2 位 |
| MediaAsset | `data/{guild}/media/{media_shard}/{media_file}` | 文件名摘要前 2 位（文件名即哈希，不二次哈希） |

规则要点：

- 帖子/评论/回复各 256 桶。评论与回复是两个结构，`c_`/`r_` 字面前缀分治，桶互不混合。
- ID 前缀是身份的一部分：Feed 恒 `B_`，Comment 恒 `c_`，Reply 恒 `r_`，与文件名一致。文件名与 ID 不一致是契约违例，写者与审计都会 fail loud。
- 路径可逆：`paths.resolve()` 从任意实体文件路径逆解出 kind/guild/shard/id，目录字面、桶形态、桶与 ID 一致性三重强校验。
- 纯函数纪律：paths 零磁盘 IO、不创建目录（目录创建归写者）。分桶永远用 `paths.feed_path()` 等助手派生，不要手算 sha256。

## 范式 B：腾讯原体 + `_p` 命名空间

每个实体文件 = 腾讯原体顶层（逐字，一字不动）+ 一个 `_p` 对象（prometheus 捕获元数据）。契约只钉 D1 薄钉集与 `_p` 全钉；顶层其余字段 `additionalProperties` 恒为 true，腾讯加字段不判非法。`_p` 恒为 JSON 顶层末键。

Feed 顶层薄钉三字段：`id`（FeedId）、`createTime`（十进制字符串秒，腾讯原体形态）、`channelInfo.sign.guild_id`（与所在 guild 目录一致，多频道树的定根字段）。Comment/Reply 薄钉 `id` 与 `createTime`（实证 channelInfo 零存在，不钉）。

### `_p` 规格

| 实体 | `_p` 键序 |
|------|-----------|
| Feed | `captured_via, first_seen, last_seen, media`（4 键） |
| Comment / Reply | `feed_id, captured_via, first_seen, last_seen, media`（5 键，`feed_id` 居首） |

| 字段 | 类型 | 语义 |
|------|------|------|
| `feed_id` | string | 所属帖子 ID（仅评论/回复有）。捕获时从列表页上下文注入，是评论到帖子的唯一归属链。feed 实体禁有此项。 |
| `captured_via` | string | 捕获通道溯源（如 `scraper`）。值域未钉，写者全控字段。 |
| `first_seen` | integer | 首次落盘时刻（unix 毫秒）。重拉永不改写。 |
| `last_seen` | integer | 最近一次重拉确认时刻（unix 毫秒）。每次重写必更新。 |
| `media` | array\<object\> | 媒体引用清单，条目 9 字段见下表。空数组 = 无媒体。 |

键序纪律：`_p` 内键序 = 契约表序，由写者硬编码构造。JSON Schema 表达不了键序，这项由 `scripts/audit_tree.py` 的 `_P_KEY_ORDER` 检查兜底。

### media 块九字段

`_p.media[]` 每个条目 9 字段全钉（含 download_url）：

| 字段 | 类型 | 语义 |
|------|------|------|
| `url` | string | 归一 URL（剥 dis_k/dis_t 易变签名参）。媒体唯一身份键、去重键、死集键。 |
| `download_url` | string \| null \| 缺省 | 最近观测的原签地址（含 dis_k/dis_t）。下载器实际取的 URL——签名是下载必需凭证，剥离即 404（签名剥离即 404）。复观刷新；无签名参数媒体 = url 同值；遗留块缺省（读取回退 url）。 |
| `file` | string \| null | 内容寻址文件名（`<sha256>.<ext>`）。null = 未下载（pending 态）。 |
| `type` | string | 媒体类型（腾讯语义，惯例值 image/video/sticker/audio）。腾讯值域不钉枚举。 |
| `width` | integer \| null | 像素宽；null = 未测得（下载后 sniff 回填）。 |
| `height` | integer \| null | 像素高；null = 未测得。 |
| `status` | `pending` \| `ok` \| `failed` \| `dead` | 下载状态机，见下节。 |
| `retries` | integer | 失败重试计数（跨周期累积），晋升 dead 的判据。 |
| `last_attempt_ts` | integer \| null | 最近下载尝试时刻（unix 毫秒）；null = 从未尝试。 |

同 url 条目跨实体可指向同一文件：内容寻址使同内容媒体坍缩为单文件，多实体引用不重复落盘。

### 状态机与晋升规则

```
pending ──下载成功──→ ok
   │
   └──下载失败──→ failed ──(retries ≥ 3)──→ dead
```

- 晋升是写前判，不是事后扫：写者合并媒体清单时，条目 `status=failed` 且 `retries>=3` 直接判 dead，一次性落盘。
- 重试计数跨周期累积：一次 attempt = 一次 fetch，无调用内重试环；3 次 failed 自然发生在 3 个 daemon 周期里。
- dead 条目不再尝试。死集在启动时从树派生（`scan()` 汇总所有 `status=dead` 的 url），不持久化。
- 媒体合并序（重拉时）：新观测清单在前，仅旧有的条目按原序追加在后；同 url 的新 9 字段块整体替换，旧条目只填不删（旧 url 永不从清单消失）。

### 两本时钟

观测时钟（`first_seen`/`last_seen`）与媒体尝试时钟（`last_attempt_ts`）是两本独立的时钟：

- 常规写入（`touch_clocks=True`）：首见取 `now_ms`，重拉保留 `first_seen`、更新 `last_seen`。
- 回填写入（`touch_clocks=False`）：要求文件已存在，两本观测时钟冻结于存量值，媒体块照常回填。观测确认与媒体下载进度互不牵连。
- 归档时间窗的判据是腾讯业务时钟 `createTime`，与这两本时钟都无关（见 [ARCHITECTURE.md](ARCHITECTURE.md) 归档一节）。

### 序列化纪律

所有实体与锁文件共用同一套写盘纪律（`src/entity_store/writer.py`）：

- UTF-8 无 BOM，`indent=2`，`ensure_ascii=False`（中文直存）。
- 腾讯键序保留，`_p` 恒为末键。
- 原子替换：写 `.tmp` 后 `os.replace`。崩溃时磁盘上要么是旧完整内容，要么是新完整内容，不会截断；`.tmp` 残留是可识别的崩溃遗物（审计豁免）。

可变性语义（Lifecycle: rewrite-in-place）：重拉 = 整文件重写。顶层逐字替换，新载荷为准，旧顶层独有的字段随重写消失；只有 `_p.first_seen` 和 `_p.media` 的旧条目被保留。

## MediaAsset：内容寻址媒体文件

文件名语言即契约（Form: grammar），文件内容不钉：

```
media_file ::= /^[0-9a-f]{64}\.(jpg|png|mp4|gif)$/
```

- 命名 = 文件内容 sha256 完整摘要 + 真实扩展名。扩展名来自 magic sniff，不转码、不改扩展名猜测；sniff 不识别的字节（如 gif）按 failed 留档，重试耗尽晋升 dead。
- 同内容跨实体、跨频道去重为单文件；删除引用不删文件（无引用计数）。
- 审计只验名形态与桶位（名前 2 hex = 所在桶），零二进制读。字节级名实复核归写者与打包引擎（打包时对账，名实不符 exit 3）。

## ProcessLock：全局进程锁

`data/prometheus.lock`，全系统唯一全局文件，存在即活动（transient 语义，release 后不删除）。5 字段全钉，键序 = 契约表序：

| 字段 | 类型 | 语义 |
|------|------|------|
| `pid` | integer | 持锁进程 PID。pid 不存活 = stale 锁。 |
| `dirty` | boolean | 脏位：本周期是否有未收尾写入。 |
| `cycle` | integer | 守护周期计数，单调递增。 |
| `ts` | integer | 锁最近更新时刻（unix 秒，非毫秒）。 |
| `bottomReached` | boolean | 历史底部是否已到达——崩溃恢复语义的承载。 |

- 读侧：缺文件 = 无锁（正常瞬态）；存在但缺字段、多字段、类型错、JSON 损坏 = `LockFormatError` fail loud。stale 判定用 `os.kill(pid, 0)`，`EPERM` 视为存活（无权发信号不等于进程死了）。
- 写侧：原子写同实体纪律，`indent=2`。

## 常用巡检

```bash
# 审计整棵 fixtures 树（Schema + 写者纪律 + 媒体名形态）
.venv/bin/python scripts/audit_tree.py --data-root contracts/fixtures
```

实测输出（逐字）：

```
=== audit_tree: entity-tree audit (schema + writer discipline) ===
data root : contracts/fixtures
guild roots (1): data/1000000000000001
scope     : feeds/+comments/ entities (Schema + _p discipline) · media/ names (64-hex content-address form, zero binary reads)
skipped (out of scope): 2 file(s)
  - archives/1000000000000001/packages/20260827T000000Z_full.tar.zst — outside guild entity/media trees (feeds/ comments/ media/)
  - data/prometheus.lock — outside guild entity/media trees (feeds/ comments/ media/)
VIOLATIONS: none
summary: entities 4 · media 3 · skipped 2 · violations 0 (-)
RESULT: CLEAN — exit 0
```

```bash
# 各类实体计数（合成基线树：2 feed + 1 comment + 1 reply + 3 media）
find contracts/fixtures/data -name 'B_*.json' | wc -l   # 2
find contracts/fixtures/data -name 'c_*.json' | wc -l   # 1
find contracts/fixtures/data -name 'r_*.json' | wc -l   # 1
find contracts/fixtures/data -path '*/media/*' -type f | wc -l   # 3
```

读取面工具（均在 `src/entity_store/`）：

| 模块 | 职责 |
|------|------|
| `reader.py` | 八个投影函数（`kind_of`/`text_of`/`author_of`/`created_at_of`/`media_of`/`target_of`/`title_of`/`poster_of`）+ 严格加载 `load_entity`。投影是腾讯原体的唯一合法读取视图。 |
| `scan.py` | `scan()` 启动扫描（per-feed 计数 + 死集）与 `iter_window()` 时间窗迭代。宽容遍历：损坏文件 skip + log，不炸服务启动。 |
| `writer.py` | `write_entity()` 唯一写入口（新见/重拉/回填共用），晋升与合并纪律所在。 |
| `paths.py` / `lock.py` | 路径分片与锁读写，见上文。 |

审计与扫描是同一种树上的两种遍历：scan 宽容（保服务启动），audit_tree 点名（逐文件逐类报告，非零退出）。损坏由审计点名，不由扫描吞掉。

