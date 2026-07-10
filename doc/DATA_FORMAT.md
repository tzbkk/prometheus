# 数据格式与查询

## 文件总览

| 文件 | 格式 | 内容 |
|------|------|------|
| `feeds.jsonl` | JSON Lines | 帖子归档（去重，跨轮幂等） |
| `comments.jsonl` | JSON Lines | 评论归档（去重，跨轮幂等） |
| `media_index.jsonl` | JSON Lines | 媒体索引（url → file/source/type/size） |
| `media/` | 二进制文件 | 下载的图片/视频（SHA256 前 16 位命名） |
| `ids.json` | 纯文本 | 帖子 ID 去重表（每行一个 ID） |
| `dead_media.jsonl` | JSON Lines | 临时失效媒体队列（重试中，跨重启持久化） |
| `dead_media_permanent.jsonl` | JSON Lines | 永久失效 URL（3 次重试失败，启动时加载到 mediaSeen 跳过） |
| `state.json` | JSON | 守护状态快照（feeds/comments 哈希、触底标记） |
| `prometheus.log` | 纯文本 | 运行日志 |

## feeds.jsonl 字段（每条 80+）

| 字段 | 说明 |
|------|------|
| `id` | 帖子 ID（B_ 前缀） |
| `poster` | 作者对象（nick/icon/medal/level 等 35 子字段） |
| `createTime` / `createTimeNs` | Unix 时间戳 / 纳秒时间戳 |
| `contents` / `title` | 富文本正文（contents/images/audios/sticker 等子结构） |
| `images[]` | 图片 CDN 链接 + 多分辨率 URL + 尺寸 |
| `videos[]` | 视频信息（playUrl/cover/vecVideoUrl） |
| `channelInfo` | 频道元数据（guild_name / icon / sign） |
| `total_like` / `total_collect` / `commentCount` | 互动数据 |
| `ip_location_province` | IP 属地 |
| `external_comment_list` | 预加载评论（含正文 + 作者 + IP） |
| `patternInfo` | 富文本排版数据 |
| `share` | 分享信息 |

## comments.jsonl 字段

每条记录包含 `_s`（来源标记）、`ts`（时间戳）、`d`（数据体）。其中 `d` 包含：
- `feedId`：关联的帖子 ID（B_ 前缀）
- `totalNum`：评论总数
- `vecComment`：评论数组，每条含 `id`/`content`/`richContents`/`postUser`/`createTime`/`replyCount`/`vecReply` 等 35+ 子字段

## 常用查询

```bash
# 帖子条数
wc -l data/feeds.jsonl

# 评论条数
wc -l data/comments.jsonl
```

```bash
# 已下载媒体
ls data/media/ | wc -l
du -sh data/media/
```

```python
# 按时间排序的帖子摘要
python3 -c "
import json
feeds = [json.loads(l) for l in open('data/feeds.jsonl')]
for f in sorted(feeds, key=lambda x: int(x.get('createTime',0)), reverse=True)[:20]:
    a = (f.get('poster') or {}).get('nick','?')
    cc = f.get('commentCount',0)
    print(f\"[{a}] [cc={cc}] {f.get('id','')[:20]} {str(f.get('createTime','?'))}\")
"
```

```bash
# 查某条帖子的所有媒体
grep 'B_xxx' data/media_index.jsonl | python3 -m json.tool
```
