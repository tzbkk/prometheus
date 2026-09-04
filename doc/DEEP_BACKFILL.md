# 深度回填 — p_skey HTTP 深捕路径

> 原理: 生产客户端频道页 = 内嵌 SPA + `pd.qq.com` HTTP auth 端点 + p_skey cookie。
> 拿到 p_skey 后深捕退化为**纯 HTTP 分页**，无需任何客户端进程。
> 实测: 597 页全量 2026-08 → 2022-02-01，isFinish=true 干净终止。
> 复核: 2026-08-29 auth 通道 604 页再测到 2022-02-01 **同帖同秒**（§2.5）；p_skey 获取已由
> ptlogin2 纯网页扫码登录工业化（§1 路线 W）。

**验证状态徽章**: ✅ = 本轮实测通过 · ⚠️ = 设计合理但未实测

②纯网页扫码登录（零部署）；③**二维码在浏览器展示**（原话"手动绘制qr依然是非常低效，
> 能把二维码图片在浏览器里展示吗"——不做终端渲染，服务直出 PNG，shell 自动开浏览器）。
> 实现：`src/deepbackfill/weblogin.py`（四步客户端）+ 服务端 `/auth/*` 三端点 + 
> `start deepbackfill` 自动探测开浏览器流。协议参数见 §1 路线 W。

## 0. 管线总览

```
┌─ 登录 ────────────┐   ┌─ HTTP auth 深分页 ─┐   ┌─ 落库（复用      ┐
│ 纯网页扫码（路线W）│ → │ (纯 HTTP,无客户端) │ → │ web_scraper 格式)│
└───────────────────┘   └───────────────────┘   └─────────────────┘
      ✅ 工业化          ✅ 597页实测+复核        ✅ 实体树直写
```

- 媒体下载、评论抓取**不变**：媒体走 CDN noauth（media.py），评论走 noauth GetFeedComments
  （无时间窗，任何在库 feed_id 永久可读）。~~深捕只需要补 feeds 列表本身~~
  **（与 scraper 完全同化——深捕一次运行自含评论/回复/媒体全管线，
  不依赖事后慢补；cli 已 service 化 :9424，契约见 contracts/components/deepbackfill.yaml）**。
- p_skey 获取的成品路径：ptlogin2 纯网页扫码登录（§1 路线 W）——服务端自动
  探测凭证、浏览器出码、扫码即得 p_skey。

## 1. 取 p_skey — 路线 W：ptlogin2 纯网页扫码登录 ✅（现行，零部署）

直接走腾讯网页登录协议（参考实现 fish2018/pansou plugin/qqpd/qqpd.go 生产代码；
参数 librarian 活测）——**无需部署任何组件**。四步：

1. `GET https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=1600001587&daid=823&style=8&hide_close_icon=1&s_url=https%3A%2F%2Fpd.qq.com%2Fexplore` → 种子 cookie（pt_login_sig）。
   （appid=1600001587/daid=823 是 pd.qq.com 专属对。）
2. `GET https://ssl.ptlogin2.qq.com/ptqrshow?appid=1600001587&e=2&l=M&s=3&d=72&v=4&t=<random>&daid=823&pt_3rd_aid=0`（Referer: xui.ptlogin2.qq.com）→ **二维码 PNG 原图** + Set-Cookie: qrsig（码寿命 ~2 分钟）。
3. 轮询 `GET https://ssl.ptlogin2.qq.com/ptqrlogin?u1=…&ptqrtoken=<hash33(qrsig,init=0)>&ptredirect=1&h=1&t=1&g=1&from_ui=1&ptlang=2052&action=0-0-<epoch_ms>&js_ver=25100115&js_type=1&login_sig=&pt_uistyle=40&aid=1600001587&daid=823`（Cookie: qrsig）→ body `ptuiCB('<code>','0','<check_url>','0','<msg>','<nickname>')`（参数 6~8 个不定，宽解析）。码表：66 未扫 / 67 已扫待确认 / 65 过期（重跑 ptqrshow 换新码）/ 0 成功（第 3 参 = check_sig URL）。**节奏 ≥1.5s/次**。
4. `GET <check_url>`（host=ptlogin2.pd.qq.com，禁跟随重定向）→ Set-Cookie: p_skey / p_uin / uin（o 前缀）落 .pd.qq.com 域。bkn = hash33(p_skey, init=5381)——与 §2.2 同函数。

`hash33：h=init; for c in s: h += (h<<5)+ord(c); return h & 0x7FFFFFFF`

工程面：`src/deepbackfill/weblogin.py`（WebLoginClient 四步全封装）；
deepbackfill 服务探测凭证缺/失效自动起 QR 会话，二维码经 `/auth/qr.png` 原图直出 +
`/auth/page` 内嵌页在浏览器展示；launcher `start deepbackfill` 自动开浏览器并等 ok。

p_skey = 为 `pd.qq.com` 域铸造的 44 字符 web 凭证（cookie 形态 `p_skey=...`，配 `p_uin=o<uin>; uin=<uin>`）。

### p_skey 有效期

未测。失效表现 = auth 端点返回错误/空数据。预估小时~天量级（web pskey 惯例）。
失效处置: `start deepbackfill` / `auth` 重扫码即重铸（成本 ≈ 一次扫码 + 2 分钟）。

## 2. HTTP auth 深度分页 ✅（597 页实测）

### 2.1 请求规格（生产客户端原样）

```
POST https://pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.guild_feed_reader.ComReader/GetGuildFeeds
     ?bkn=<bkn>&_t=<epoch_ms>&_v=1.0.1&client_platform=pcqqwebview

cookie: p_uin=o<uin>; uin=<uin>; p_skey=<pskey>
x-oidb: {"uint32_command":"0x93df","uint32_service_type":13}
x-qq-client-appid: 537379447          # webview 专用 appid（≠ Linux 客户端 537376650）
content-type / accept: application/json
origin: https://pd.qq.com
referer: https://pd.qq.com/explore
user-agent: Mozilla/5.0 (X11; Linux x86_64) ... QQAppId/537379447 QQWebview/1.0.0.0

body: {"count":20,"from":7,"guild_number":"<guild_number>","get_type":1,
       "feedAttchInfo":"<cursor>","sortOption":0,
       "need_channel_list":false,"need_top_info":false}
```

注意与 noauth 的差异（易踩）:

| 项 | noauth（scraper 日常用） | auth（深捕用） |
|---|---|---|
| 服务名 | `trpc.qchannel.commreader.ComReader` | `trpc.qchannel.guild_feed_reader.ComReader` |
| guild 标识 | body 键 `guild_id`（值同样是 guild_number） | body 键 `guild_number` ✅实测 |
| x-oidb | service_type=12 | `0x93df` + service_type=13 |
| 深度 | ~5.5 个月窗口 | 默认频道全量（isFinish 终止）+ 逐频道 timeline（§2.6） |

### 2.2 bkn 计算（经典 QZONE 哈希，逐字节验证 ✅）

```python
def bkn(p_skey: str) -> int:
    h = 5381
    for c in p_skey:
        h = (h + (h << 5) + ord(c)) & 0x7FFFFFFF
    return h
```

### 2.3 分页循环语义

- 游标: 响应 `data.feedAttchInfo` 回填下一页 body（opaque 串，含 pageNum 等）
- 终止: `data.isFinish == true`（干净）或 游标不再变化（异常）
- 响应取数: `data.vecFeed[]`（feed 对象含 `id` / `createTime` / `channelInfo` 等，与 noauth 同名同义）
- **每页固定 10 条**（count 参数无效，实测 400 页 × 10）
- 实测性能: ~1.13s/页（含 1s 礼貌间隔），597 页 ≈ 11 分钟跑完 4 年全量

### 2.4 实测结论

```
400 页 → 2023-12-26（首跑验证击穿 noauth 窗口）
597 页 → 2022-02-01 22:57 最老帖 (B_584af961c60500001441152187194632840X60), isFinish=true
```

### 2.5 复核（2026-08-29）

```
604 页 → 2022-02-01 22:57:28 同帖同秒, isFinish=true
1.93s/页（0.8s 间隔）· 19.5 分钟全量 · 6016 unique feeds
年份分布: 2022:121 · 2023:306 · 2024:248 · 2025:1709 · 2026:3633
```

### 2.6 频道走查（2026-09-03 完整性事故 · "没有抓全"）

**GetGuildFeeds 默认流只覆盖默认频道（帖子广场）**——605 页 isFinish=true
仅 6037 帖，其余 16 个分区频道的帖子（全频道实际 ~10k）只出现在各自
timeline。修复 = listing 两段式：默认流走完 → `need_channel_list=true`
取频道清单（同路由，响应 `data.channels[]`，含 `channel_id`/`name`）→
逐频道 timeline 全量走（流已覆盖的频道跳过）→ growth 评论重拉最后。

频道 timeline 线格式（浏览器抓包实证，与默认流**不同服务域**）：

```
POST https://pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.commreader.ComReader/GetChannelTimelineFeeds?bkn=<bkn>
x-oidb: {"uint32_service_type":10}        # 无 uint32_command
x-qq-client-appid: 537246381              # 非默认流 appid 族
Referer: https://pd.qq.com/g/<guild_id>
{"count":10,"from":7,
 "channelSign":{"guild_id":"<数字id>","channel_id":"<cid>"},
 "feedAttchInfo":"<游标>","sortOption":0,"need_top_info":false}
```

硬约束（违反即 `[backend] 参数错误`）：query **只能带 bkn**（_t/_v/
client_platform 一律不带）；channelSign 用**数字 guild_id**（slug 报错）；
`sortOption=1` 是热度序（翻页重叠），全量走必须 `sortOption=0` 时序。
每页 12 条（count 无效），页界约 1 条重叠——save 按 feed id 幂等去重。
频道清单失败降级：只走默认流继续 growth（不阻断）。

## 3. 落库 ✅

> 落库 = `entity_store` 写者直写实体树（契约 `contracts/components/deepbackfill.yaml` Downstream `Feed: []`）；auth 与 noauth 的 vecFeed 同 schema。

- 格式兼容性核心事实: auth 与 noauth 的 vecFeed 元素**同一 schema**（`id`/`createTime`/`vecFeed`/`feedAttchInfo`/`isFinish` 全部同名，深捕循环直接按 web_scraper 同款键消费成功）
- 落库路径: `entity_store` 写者直写实体树；feed 解析复用 `src/web_scraper/` 同款键（`id`/`createTime`/`channelInfo.sign.guild_id`）
- 媒体: feed 内媒体 URL 交给现有 media.py 管线（CDN noauth 不限）
- 评论: 现有 comments.py noauth 抓取（老帖无窗口，永久可读）——深捕完成后照常补评论

## 4. 运维参数

| 项 | 值/状态 |
|---|---|
| 触发条件 | **全量获取主通道（2022 以降全史唯一回源）** |
| 单次成本 | 一次扫码 + ~15 分钟（登录2 + 分页11 + 校验2） |
| p_skey 有效期 | 未测（小时~天量级推测），失效即重扫码重铸 |
| 频率建议 | ≥1s/页间隔（本轮 1.13s/页 无任何风控迹象）；避免短时间内反复全量 |
| 归档数据 | **永不随代码/仓库分发**（含他人个人信息） |

## 5. 合规护栏（开源发布形态，见对话结论 2026-08-25）

1. **深捕凭证永远用户自供**: p_skey 只经用户本人手机扫码获得（§1 路线 W），代码不做任何替代性凭证获取
2. **不做凭证收割自动化** — 凭证获取只走官方网页扫码面，这是刑事定性风险的核心区分项（对比: 丁某案 vs RSSHub）
3. README 声明: 自有账号、自担封号风险、禁止商用/再分发数据
4. 对照生态水位: RSSHub（pd.qq.com noauth 路由，运行多年）
