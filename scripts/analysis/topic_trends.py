#!/usr/bin/env python3
"""话题趋势：实体树 → 清洗 → Qwen3-Embedding-0.6B → BERTopic → 本地 LLM 命名/合并 → 双 stack 图。

管线要点：
  ① 文本清洗剥离 QQ 分享标记 / 颜文字 / emoji，cTF-IDF 用自定义分词+停用表
  ② embedding 用 Qwen3-Embedding-0.6B（1024d）
  ③ 簇命名与合并候选由本地 OpenAI 兼容端点（LM Studio, qwen/qwen3.5-9b）完成，
     合并结果人工裁定为行为族（拒绝 LLM 传递闭包造成的巨型团块）
  ④ 特殊类别覆盖（按优先级）：AIGC → 请求直投 → 资源分享 → 自绘 → 原高木/漫画/美图
     → 音乐 → 同人文 → 情感 → 其他圈子 → 行为簇

用法：
  python scripts/topic_trends.py --stage all
  python scripts/topic_trends.py --stage cluster --skip-embed
stage ∈ clean | embed | cluster | name | merge | chart | all（默认 all）
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "analysis")
EMBED_MODEL = "/home/hxue/.cache/modelscope/models/Qwen--Qwen3-Embedding-0.6B/snapshots/master"
LLM_URL = "http://127.0.0.1:1234/v1/chat/completions"
LLM_MODEL = "qwen/qwen3.5-9b"

# 人工裁定的行为族（LLM 邻域裁决 45 对接受后的收敛结果）
GROUPS = {
    "求图/资源求助": [3, 11, 24, 36, 37, 43, 50],
    "夸夸/图片互动": [10, 13, 31, 42],
    "同人绘画": [6, 53],
    "同人文连载": [28, 41, 49],
    "每日打卡/仪式": [17, 27, 40],
    "画师推图": [14, 45, 48],
    "节日/生日祝福": [18, 35, 38],
    "谷子购买/交易": [1, 22],
    "追番进度/续作期待": [4, 26, 34],
    "原作讨论/情感共鸣": [0, 9, 32, 46],
    "跨圈玩梗/闲聊": [19, 25, 33],
    "AI二创/拼豆": [39, 47],
}
AIGC_CATEGORY = "AIGC内容(生成+助手)"
AIGC_KEYWORDS = "AI生成; 豆包; AI绘画; 频道助手日报"
YUANMU_CATEGORY = "原/元高木追更"
YUANMU_KEYWORDS = "原高木; 话数追更; 婚后篇; 小千"
YUANMU_PATTERN = re.compile(r"原高|元高|原高木|元高木")
MANGA_CATEGORY = "漫画页/话数搬运"
MANGA_KEYWORDS = "整话搬运; 单页分享; 多页长图; 仅供娱乐"
MANGA_CHAPTER = re.compile(r"原高木|元高木|\d{2,3}\s*话|第\s*\d+\s*话|漫画\s*线|话\s*$|篇\s*$")
MEITU_CATEGORY = "美图分享(插画)"
MEITU_KEYWORDS = "单幅插画; 图源搬运; 画师署名; 分享图片"
MEITU_CANDIDATE = re.compile(r"^(分享图片|分享视频|分享表情|分享动态)$")
FICTION_CATEGORY = "同人文(长篇)"
EMOTION_CATEGORY = "情感表达(非虚构)"
NOTICE_CATEGORY = "同人公告(更新/预告)"
FICTION_KEYWORDS = "连载; 章节; 对话; 剧情"
EMOTION_KEYWORDS = "青春; 回忆; 喜欢; 陪伴"
NOTICE_KEYWORDS = "更新; 预告; 连载; 宣发"
AIFAN_CATEGORY = "AI工具/二创实验"
AIFAN_KEYWORDS = "AI生成; 豆包; 模型; 提示词; 实验"
AIFAN_SIGNAL = re.compile(r"(?i)(?<![a-z])ai(?![a-z])|模型|生成|豆包|chatgpt|gpt|deepseek|midjourney|novelai|即梦|可灵|lora|炼丹|声线|翻唱|训练|提示词|prompt|高斯|开源|算法")
BEAD_SIGNAL = re.compile(r"拼豆|豆豆|熨斗|拼图|手工|珠子|像素")
DRAW_CATEGORY = "同人绘画"
DRAW_SELF = re.compile(r"我画|画了|画的|画完|手绘|板绘|指绘|草稿|原创|临摹|涂鸦|画技|约稿|接稿|稿子|摸鱼|摸完|练习|作画|画渣")
DRAW_OTHER = re.compile(r"^(小千|群友|大佬|别人|朋友)[^，。]{0,4}画的")
NOTICE_CRE = re.compile(r"更新|预告|连载|新篇|新作|第.{1,3}(话|章|篇)|即将|敬请期待|开更|完结|续写|重启|写到第|又写|周末发|明天发|发布|上线|出第.{1,3}(话|章|篇)")
NOTICE_FAN = re.compile(r"同人|小说|作者|投稿|章节|续写|写.{0,4}(文|章|篇)|粮|产粮")
NOTICE_TAKA = re.compile(r"高木|西片|太太|小千|原高")
RESOURCE_CATEGORY = "资源分享(网盘/链接)"
RESOURCE_KEYWORDS = "网盘; 提取码; 资源站; 压缩包; 合集"
RESOURCE_SIGNAL = re.compile(r"pan\.baidu|aliyundrive|阿里云盘|夸克网盘|迅雷|盘\.xunlei|lanzou|蓝奏|提取码|资源站|资源库|压缩包|图包")
RESOURCE_WEAK = re.compile(r"网盘|提取码|资源站|资源库|压缩包|图包")
RESOURCE_REQ = re.compile(r"求图|求原图|求高清|求壁纸|求头像|求表情|求歌|求曲|求资源|求链接|求软件|求本子|求漫画|求周边|求同人|求出处|求翻译|求字幕|求网址|谁有|有没有|有大佬|跪求|帮找|帮忙找|求分享|求推荐|求个|求一份")
RESOURCE_EXC = re.compile(r"投票|世萌|萌战|票数|闲鱼|淘宝|京东|拼多多|价格|多少钱|购买|下单|代购|吧唧|手办|上架|发售|预告|即将")
RENAMES = {"情感共鸣与同人创作": EMOTION_CATEGORY, "同人文连载": NOTICE_CATEGORY,
           "同人创作与资源分享": "同人创作交流", "原作衍生与资源交流": "原作衍生讨论"}
OPS_CATEGORY = "频道运维/社区"
OPS_KEYWORDS = "周年庆; 活动; 社区闲聊; 引流; 搬运纠纷"
OPS_SOURCE_CATS = {"频道运营与闲聊", "社群维系与资源分享", "跨平台引流互助"}
RESOURCE_HINT = re.compile(r"网盘|提取码|资源|合集|图包|下载|求图|谁有|链接")
META_HINT = re.compile(r"周年|庆典|活动|频道|群主|管理|审核|举报|规范|规则|加入|入坑|解散|板块|直播|晚会|投稿|引流|搬运|点赞|粉丝|互赞")
CROSS_CATEGORY = "跨圈玩梗"
WATER_CATEGORY = "水聊/碎片互动"
WATER_KEYWORDS = "问候; 感叹; 碎片; 扩列"
CROSS_KEYWORDS = "火影; 原神; 手游; 抽卡; 联机"
CROSS_SUBJECT = re.compile(r"火影忍者|火影|原神|王者荣耀|咒术回战|鬼灭之刃|鬼灭|名侦探柯南|宝可梦|口袋妖怪|东方project|崩坏|星穹铁道|明日方舟|孤独摇滚|铃芽之旅|你的名字|间谍过家家|芙莉莲|进击的巨人|海贼王|龙珠|新世纪福音战士|赛马娘|蔚蓝档案|鸣潮|绝区零|第五人格|三角洲行动|我的世界|塞尔达|马里奥|奥特曼|假面骑士|光之美少女|轻音少女|电锯人|蓝色监狱|排球少年|灌篮高手|高达|手游|端游|开黑|联机|抽卡|氪金|撤离|副本|角色池|代充")
ROLE_DISC = re.compile(r"锐评|感情线|声优|CP|在一起|关系|结局|遗憾|角色|戏份|剧情|填坑")
ROLE_PRAISE = re.compile(r"好看|美|可爱|妈妈级别|夯|颜值")
XIPIAN_CRE = re.compile(r"我画|画了|画的|画完|写了|摸鱼|草稿|原创|临摹|每日一画")
XIPIAN_VOTE = re.compile(r"世萌|萌战|投票")
XIPIAN_PRAISE = re.compile(r"可爱|帅|好看|喜欢|爱了|萌|棒|绝|美|老婆")
XIPIAN_DISC = re.compile(r"西片.{0,8}(是|像|觉得|怎么|为什么|会|不会)|如果|其实|这段|那一集|漫画")
WATER_DRAMA = re.compile(r"盗图|盗用|搬运|举报|限流|流量|侵权|挂人|营销号")
WATER_MERCH = re.compile(r"周边|谷子|到货|手办|吧唧|立牌|多少钱|代购|单买")
MUSIC_CATEGORY = "音乐分享(含视频)"
MUSIC_KEYWORDS = "歌曲; 主题曲; BGM; 翻唱; 演奏"
MUSIC_SIGNAL = re.compile(r"音乐|歌曲|歌名|听歌|网易云|QQ音乐|酷狗|歌单|主题曲|片头曲|片尾曲|插曲|(?<![A-Za-z])(OP|op|ED|ed)(?![A-Za-z])|BGM|bgm|背景音乐|翻唱|纯音乐|原曲|单曲循环|循环播放|演唱会|演奏|弹奏|钢琴|吉他|这首歌|这歌|曲名|唱")
MUSIC_SONG = re.compile(r"求.{0,4}歌|什么歌|歌名|曲名|(背景音乐|bgm|BGM).{0,6}(是|叫|花|纯音乐|这首)|(主题曲|插曲|片头|片尾|OP|op|ED|ed).{0,8}(是|叫|：|:)|纯音乐|原曲是")
MUSIC_EDIT = re.compile(r"剪辑|剪映|剪了|剪的|混剪|自制|卡点|踩点")
REQUEST_ASK = re.compile(r"求图|求原图|求高清|求壁纸|求头像|求表情|求歌|求曲|求bgm|求BGM|求资源|求链接|求软件|求本子|求漫画|求周边|求同人|求出处|求翻译|求字幕|求网址|谁有|有人有|有没有人|有没有大佬|有大佬|跪求|帮找|帮忙找|求分享|求推荐|求个|求一份|怎么免费看|在哪看|谁知道.{0,6}(看|资源|图|软件)")
REQUEST_MUSIC = re.compile(r"歌|曲|bgm|BGM|歌词|歌单|专辑")
REQUEST_MERCH = re.compile(r"周边|谷子|手办|单行本|多少钱|哪里买|价格|吧唧|立牌|出|售|闲鱼|定金|尾款")
REQUEST_FAN = re.compile(r"同人|小说|文章")
REQUEST_SKIP = re.compile(r"群号|进群|加群|频道|违规|群聊|二维码|扫码")
REQUEST_NARR = re.compile(r"西片|高木")
MUSIC_TITLE = re.compile(r"纯音乐|歌名|曲名|这首歌|bgm是|背景音乐是|(?<![A-Za-z])(OP|ED|op|ed)(?![A-Za-z])|主题曲|插曲|片尾曲|片头曲")
OTHER_IP = re.compile(r"火影忍者|火影|原神|王者荣耀|咒术回战|鬼灭之刃|鬼灭|名侦探柯南|宝可梦|口袋妖怪|东方project|崩坏|星穹铁道|明日方舟|孤独摇滚|铃芽之旅|你的名字|间谍过家家|芙莉莲|进击的巨人|海贼王|龙珠|新世纪福音战士|赛马娘|蔚蓝档案|鸣潮|绝区零|第五人格|三角洲行动|我的世界|塞尔达|马里奥|奥特曼|假面骑士|光之美少女|轻音少女|电锯人|蓝色监狱|排球少年|灌篮高手")
TAKAGI_HINT = re.compile(r"高木|西片|太太|小千|原高|takagi|Takagi|TKG|tkg")
FICTION_SERIAL = re.compile(r"第\s*\d+\s*[话章篇]|上篇|下篇|续篇|本篇|待续|连载|接上|前情提要")
FICTION_SCENE = re.compile(r"(夜|晨|清晨|傍晚|阳光|月光|风|雨|雪|教室|走廊|街道|车站|海边|天空|樱花|家里)[^，。]{0,10}(的|里|中)|走过|望着|说道|轻声|沉默|笑了|坐在|站在")
EMOTION_FIRST = re.compile(r"我(?!们)|我们")
EMOTION_WORDS = re.compile(r"青春|回忆|祝福|陪伴|谢谢|大家|频道|感动|泪目|遗憾|成长|遇见|喜欢|心里|感觉|觉得")
ANNOUNCE = re.compile(r"公告|预告|更新|资源|链接|群号|地址|合集|求更|催更|宣传片|策划中")
AIGC_SELF = re.compile(
    r"(ai|AI|Ai|人工智能).{0,4}(生成|生产|绘画|绘图|画的|画|图|绘|上色|做的|作品|出图|生图|跑图|写的|创作|修图|溶图)|"
    r"(生成|生产|上色|跑|用|自制).{0,4}(ai|AI)|"
    r"(豆包|chatgpt|ChatGPT|deepseek|DeepSeek|midjourney|Midjourney|novelai|NovelAI|即梦|可灵|通义|文心|元宝|星野|sd|SD).{0,6}(生成|画|做|出图|生图|写|还原)|"
    r"和\s*(ai|AI)\s*聊天.{0,10}(写|创作|灵感)")
AIGC_EXCLUDE = re.compile(
    r"画图吗|是ai吗|是不是ai|ai叫啥|什么软件|怎么做的|ai不错|ai厉害|抵制|必须标注|规则|禁止|举报|下架|诋毁|辨别|"
    r"是不是chatgpt|在哪|哪里|问一下|刚学|怎么用|发生了什么|最热门的帖子|担忧|骗|盗|黑社会|不要动|高清一下|味|而已|"
    r"警告|成立AI公司|怎么还有人|神人|人才|擦边|避免不了|博主")
AIGC_BOT = re.compile(r"(频道助手|高木同学助手)")

GLOSS = """社区词义提示：
- 捉弄 = 本IP核心设定（高木捉弄西片），不是恶作剧/迎新
- 原高/原高木 = 原作漫画《(擅长捉弄的)原高木同学》(婚后篇)；小千 = 婚后篇女儿
- 太太 = 高木（粉丝称呼）；西片 = 男主角；扭 = 发糖/甜
- 谷子 = 周边商品；世萌/新世萌 = 萌战投票；稻叶/山本 = 画师"""
STOP = set("""分享 图片 视频 表情 动画 动态 相册 文件 好看 可以 谢谢 厉害 太强 牛逼 真棒 真好 不错
哈哈 哈哈哈 感觉 有点 一下 真的 时候 什么 怎么 这个 那个 一个 没有 不是 还是 就是 因为 所以 但是 已经
现在 知道 觉得 可能 应该 大家 自己 我们 你们 他们 频道 帖子 评论 成员 楼主 大佬 一起 一直 非常
什么样 为什么 是不是 有没有 这么 那么 一点 东西 这种 那些 这些 之后 之前 以后 以前 刚才 然后 而且
虽然 如果 或者 还有 也是 都是 只是 不过 其实 反正 确实 直接 比较 更好 最好 好多 很多 多少 几个
第一 第二 第三 今天 昨天 明天 最近 原来 果然 居然 竟然 突然 马上 一定 肯定 需要 不会 不能 不用
不要 不行 嘿嘿 呜呜 啊啊 哇哇 捏 喵 哇 诶 嘞 啦 咯 嘛 呗 哦 噢 喔 咦 嗯 恩 呀 吧 呢 吗 啊 这 那
我 你 他 她 它 们 的 了 是 在 也 都 很 还 不 有 个 就 和 与 及 或 但 被 没 去 来 想 说 看 做 要 给
让 对 从 到 里 上 下 中 大 小 多 少 好 会 能 可 所 以 为 之 其 此 该 每 各 另 啥 咋 咱 俺 您 谁 哪
何时 为何 如何 是否 能否 有无 真假 笑死 笑 哭 泪 感谢 辛苦 加油 支持 关注 转发 收藏 私信 群 群聊
群里 进群 加群 链接 网址 网站 软件 app APP AI ai 音频 音乐 歌 曲 照片 原图 高清 壁纸 头像 表情包
情头 搬运 转载 原创 自制 密码 资源 下载 上传 在线 地址 二维码 扫码""".split())


def tokenize(text):
    import jieba
    return [w for w in jieba.lcut(text)
            if len(w) >= 2 and any("\u4e00" <= c <= "\u9fff" for c in w) and w not in STOP]


def llm(prompt, max_tokens=300):
    body = json.dumps({"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": max_tokens}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(LLM_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                txt = json.load(resp)["choices"][0]["message"]["content"]
            txt = re.sub(r"<think.*?</think.*?>", "", txt, flags=re.S)
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                return json.loads(m.group())
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    return {}


def load_docs():
    with open(os.path.join(OUT, "docs_clean.jsonl")) as f:
        return [json.loads(line) for line in f]


def stage_clean():
    import glob
    sys.path.insert(0, ROOT)
    from datetime import datetime, timezone

    from src.entity_store import body_of, created_at_of, iter_window, text_of, title_of

    feeds, comments = {}, {}
    for entry in iter_window(os.path.join(ROOT, "data"), "7743321643036658", 0, 1 << 62):
        if entry.kind == "feed":
            fid = entry.path.stem
            feeds[fid] = {
                "create_time": created_at_of(entry.doc),
                "text": (body_of(entry.doc) or "").strip() or (title_of(entry.doc) or "").strip(),
            }
        else:
            fid = (entry.doc.get("_p") or {}).get("feed_id")
            if fid:
                raw_seq = entry.doc.get("sequence") or 0
                seq = int(raw_seq) if str(raw_seq).isdigit() else 0
                comments.setdefault(fid, []).append((created_at_of(entry.doc), seq, text_of(entry.doc)))

    share = re.compile(r"(分享图片|分享视频|分享表情|动画表情|\[动画表情\]|分享动态|分享文件)")
    keep = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9，。！？、；：“”‘’（）《》…—～\s]")
    out, lost = [], 0
    for fid in sorted(feeds):
        feed = feeds[fid]
        seen, unique = set(), []
        for _, _, t in sorted(comments.get(fid, [])):
            key = t.strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(t)
        merged = unique[:30]
        text = (feed["text"] + " " + " ".join(merged)).strip()
        cleaned = keep.sub(" ", share.sub(" ", text))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        lost += len(text) - len(cleaned)
        if len(cleaned) < 6:
            continue
        out.append({
            "feed_id": fid,
            "month": datetime.fromtimestamp(feed["create_time"], tz=timezone.utc).strftime("%Y-%m"),
            "create_time": feed["create_time"],
            "text": cleaned,
            "n_comments": len(merged),
        })
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "docs_clean.jsonl"), "w") as f:
        for d in out:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"clean: {len(out)} docs, stripped {lost} chars")


def stage_embed():
    from sentence_transformers import SentenceTransformer
    docs = load_docs()
    model = SentenceTransformer(EMBED_MODEL, device="cuda")
    model.max_seq_length = 256
    emb = model.encode([d["text"] for d in docs], batch_size=64, normalize_embeddings=True,
                       show_progress_bar=False, convert_to_numpy=True).astype(np.float32)
    np.save(os.path.join(OUT, "embeddings.npy"), emb)
    print(f"embed: {emb.shape}")


def stage_cluster():
    import warnings
    warnings.filterwarnings("ignore")
    import jieba
    jieba.setLogLevel(60)
    from bertopic import BERTopic
    from bertopic.representation import KeyBERTInspired
    from hdbscan import HDBSCAN
    from sklearn.feature_extraction.text import CountVectorizer
    from sentence_transformers import SentenceTransformer
    from umap import UMAP

    docs = load_docs()
    texts = [d["text"] for d in docs]
    emb = np.load(os.path.join(OUT, "embeddings.npy"))
    st = SentenceTransformer(EMBED_MODEL, device="cuda")
    st.max_seq_length = 256
    # language 默认 english 会删除所有非 ASCII；embedding_model 供 KeyBERTInspired 使用
    tm = BERTopic(
        language="chinese",
        embedding_model=st,
        umap_model=UMAP(n_neighbors=30, n_components=5, min_dist=0.0,
                        metric="cosine", random_state=42),
        hdbscan_model=HDBSCAN(min_cluster_size=40, min_samples=5, metric="euclidean",
                              cluster_selection_method="eom", prediction_data=True),
        # min_df=2：只有 ~55 个主题文档，min_df=5 会得到空词表
        vectorizer_model=CountVectorizer(tokenizer=tokenize, token_pattern=None,
                                         min_df=2, max_df=0.5),
        representation_model={"Main": KeyBERTInspired(top_n_words=10)},
        verbose=False,
    )
    topics, _ = tm.fit_transform(texts, embeddings=emb)
    pre = sum(1 for t in topics if t == -1) / len(topics)
    topics = tm.reduce_outliers(texts, topics, strategy="embeddings", embeddings=emb)
    tm.update_topics(texts, topics=topics, vectorizer_model=tm.vectorizer_model,
                     representation_model={"Main": KeyBERTInspired(top_n_words=10)})
    tm.topics_ = topics
    tm.save(os.path.join(OUT, "topic_model"), serialization="safetensors",
            save_ctfidf=True, save_embedding_model=False)
    np.save(os.path.join(OUT, "topics.npy"), np.array(topics))

    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, poster_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    texts, nicks = {}, {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        texts[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
        poster = poster_of(doc) if poster_of else None
        nicks[fid] = (poster.get("nick") or "") if isinstance(poster, dict) else ""
    rows = []
    for d in docs:
        t, n = texts.get(d["feed_id"], ""), nicks.get(d["feed_id"], "")
        if AIGC_BOT.search(n):
            rows.append({"feed_id": d["feed_id"], "month": d["month"], "text": t[:300], "source": "bot账号"})
        elif t and AIGC_SELF.search(t) and not AIGC_EXCLUDE.search(t):
            rows.append({"feed_id": d["feed_id"], "month": d["month"], "text": t[:300], "source": "自述AIGC"})
    with open(os.path.join(OUT, "aigc_docs.json"), "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "aigc_ids.json"), "w") as f:
        json.dump([r["feed_id"] for r in rows], f)
    print(f"aigc: {len(rows)} posts (bot={sum(1 for r in rows if r['source']=='bot账号')}, "
          f"self-declared={sum(1 for r in rows if r['source']=='自述AIGC')})")


def stage_yuanmu():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    texts = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        texts[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    aigc = set(json.load(open(os.path.join(OUT, "aigc_ids.json")))) if os.path.exists(
        os.path.join(OUT, "aigc_ids.json")) else set()
    hits = [d["feed_id"] for d in docs
            if YUANMU_PATTERN.search(texts.get(d["feed_id"], "")) and d["feed_id"] not in aigc]
    with open(os.path.join(OUT, "yuanmu_ids.json"), "w") as f:
        json.dump(hits, f)
    print(f"yuanmu: {len(hits)} posts")


def stage_manga():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    rows = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        images = doc.get("images") or []
        tall = sum(1 for im in images
                   if im.get("width") and im.get("height")
                   and int(im["height"]) > int(im["width"]) * 1.15)
        rows[fid] = {
            "text": (body_of(doc) or "").strip() or (title_of(doc) or "").strip(),
            "n": len(images),
            "tall": tall / max(len(images), 1),
        }
    aigc = set(json.load(open(os.path.join(OUT, "aigc_ids.json")))) if os.path.exists(
        os.path.join(OUT, "aigc_ids.json")) else set()
    yuanmu = set(json.load(open(os.path.join(OUT, "yuanmu_ids.json")))) if os.path.exists(
        os.path.join(OUT, "yuanmu_ids.json")) else set()
    vision_path = os.path.join(OUT, "vision_cache.json")
    vision_pages = set()
    if os.path.exists(vision_path):
        vision_pages = {f for f, c in json.load(open(vision_path)).items() if c == "1"}
    hits = [fid for fid, r in rows.items()
            if (fid in vision_pages
                or (r["n"] >= 3 and r["tall"] >= 0.6 and MANGA_CHAPTER.search(r["text"])))
            and fid not in aigc and fid not in yuanmu]
    with open(os.path.join(OUT, "manga_ids.json"), "w") as f:
        json.dump(hits, f)
    print(f"manga: {len(hits)} posts")


def stage_draw():
    docs = load_docs()
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    feed_text = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        doc = json.load(open(path))
        feed_text[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    topics = [int(x) for x in np.load(os.path.join(OUT, "topics.npy"))]
    names = {int(k): v["label"] for k, v in json.load(open(os.path.join(OUT, "topic_names.json"))).items()}
    merged_ids = {m for g in GROUPS.values() for m in g}
    single = {int(t): names[int(t)] for t in names if int(t) not in merged_ids}
    key_of = {m: g for g, ms in GROUPS.items() for m in ms}
    skip = set()
    for name in ("aigc_ids.json", "yuanmu_ids.json", "manga_ids.json", "meitu_ids.json",
                 "fiction_ids.json", "essay_ids.json", "music_ids.json", "resource_ids.json"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            skip |= set(json.load(open(path)))
    request_path = os.path.join(OUT, "request_routing.json")
    if os.path.exists(request_path):
        skip |= set(json.load(open(request_path)))
    hits = []
    for d, t in zip(docs, topics):
        fid = d["feed_id"]
        if fid in skip:
            continue
        text = feed_text.get(fid, "")
        cat = RENAMES.get(key_of.get(t, single.get(t)), key_of.get(t, single.get(t)))
        if cat == "夸夸/图片互动" and DRAW_SELF.search(text) and not DRAW_OTHER.search(text):
            hits.append(fid)
    with open(os.path.join(OUT, "draw_ids.json"), "w") as f:
        json.dump(sorted(hits), f)
    print(f"draw: {len(hits)} self-created posts moved to 同人绘画")


def stage_resources():
    docs = load_docs()
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    feed_text = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        doc = json.load(open(path))
        feed_text[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    skip = set()
    for name in ("aigc_ids.json", "yuanmu_ids.json", "manga_ids.json", "meitu_ids.json",
                 "fiction_ids.json", "essay_ids.json", "music_ids.json"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            skip |= set(json.load(open(path)))
    topics = [int(x) for x in np.load(os.path.join(OUT, "topics.npy"))]
    names = {int(k): v["label"] for k, v in json.load(open(os.path.join(OUT, "topic_names.json"))).items()}
    merged_ids = {m for g in GROUPS.values() for m in g}
    single = {int(t): names[int(t)] for t in names if int(t) not in merged_ids}
    key_of = {m: g for g, ms in GROUPS.items() for m in ms}
    hits = []
    for d, t in zip(docs, topics):
        fid = d["feed_id"]
        if fid in skip:
            continue
        text = feed_text.get(fid, "")
        if RESOURCE_REQ.search(text) or RESOURCE_EXC.search(text):
            continue
        cat = key_of.get(t, single.get(t))
        cat = RENAMES.get(cat, cat)
        if RESOURCE_SIGNAL.search(text) or (
                cat == NOTICE_CATEGORY and RESOURCE_WEAK.search(text)):
            hits.append(fid)
    with open(os.path.join(OUT, "resource_ids.json"), "w") as f:
        json.dump(sorted(hits), f)
    print(f"resources: {len(hits)} posts")


def stage_requests():
    docs = load_docs()
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    feed_text = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        doc = json.load(open(path))
        feed_text[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    skip = set()
    for name in ("aigc_ids.json", "fiction_ids.json", "essay_ids.json"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            skip |= set(json.load(open(path)))
    routing = {}
    for d in docs:
        fid = d["feed_id"]
        t = feed_text.get(fid, "")
        if fid in skip or len(t) > 150 or not REQUEST_ASK.search(t):
            continue
        if REQUEST_SKIP.search(t):
            continue
        quotes = t.count("“") + t.count("”") + t.count("「") + t.count("」")
        if quotes >= 2 and REQUEST_NARR.search(t):
            continue
        if REQUEST_MUSIC.search(t):
            routing[fid] = MUSIC_CATEGORY
        elif REQUEST_MERCH.search(t):
            routing[fid] = "谷子购买/交易"
        elif REQUEST_FAN.search(t):
            routing[fid] = NOTICE_CATEGORY
        else:
            routing[fid] = "求图/资源求助"
    with open(os.path.join(OUT, "request_routing.json"), "w") as f:
        json.dump(routing, f, ensure_ascii=False, indent=1)
    print(f"requests: {len(routing)} routed")


def stage_music():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, media_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"]: d for d in docs}
    rows = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        ms = media_of(doc) or []
        rows[fid] = {
            "feed": (body_of(doc) or "").strip() or (title_of(doc) or "").strip(),
            "nv": sum(1 for m in ms if m.get("type") == "video"),
            "pool": wanted[fid]["text"],
        }
    cands = {}
    for fid, r in rows.items():
        feed, pool = r["feed"], r["pool"]
        comment = pool[len(feed):] if pool.startswith(feed) else ""
        if MUSIC_SIGNAL.search(feed) and not (
                MUSIC_EDIT.search(feed) and not MUSIC_SIGNAL.search(feed.replace("剪辑", ""))):
            cands[fid] = "feed"
        elif r["nv"] and MUSIC_SONG.search(comment):
            cands[fid] = "video+comments"
    os.makedirs(os.path.join(OUT, "llm_cache"), exist_ok=True)
    approved = []
    for fid in cands:
        cache = os.path.join(OUT, "llm_cache", f"music_{fid}.json")
        if os.path.exists(cache):
            verdict = json.load(open(cache))
        else:
            verdict = llm("这条QQ频道帖子是否以'音乐分享'为主要目的？（分享歌曲/OP/ED/主题曲/演奏/翻唱/给歌名；"
                          "而不是剪辑作品、手绘、周边、闲聊）只回复 A(是) 或 B(否)。\n\n帖文："
                          + rows[fid]["feed"][:180] + "\n评论区："
                          + rows[fid]["pool"][len(rows[fid]["feed"]):][:220], max_tokens=8)
            if isinstance(verdict, dict):
                verdict = str(list(verdict.values())[0])[:1]
            json.dump(verdict, open(cache, "w"))
        ok = str(verdict).strip().upper().startswith("A")
        if not ok and rows[fid]["nv"]:
            feed = rows[fid]["feed"]
            comment = rows[fid]["pool"][len(feed):] if rows[fid]["pool"].startswith(feed) else rows[fid]["pool"]
            ok = bool(MUSIC_TITLE.search(comment))
        if ok:
            approved.append(fid)
    with open(os.path.join(OUT, "music_ids.json"), "w") as f:
        json.dump(sorted(approved), f)
    print(f"music: {len(approved)} llm-approved (candidates {len(cands)})")


def stage_fiction():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    texts = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        texts[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    skip = set()
    for name in ("aigc_ids.json", "yuanmu_ids.json", "manga_ids.json", "meitu_ids.json"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            skip |= set(json.load(open(path)))

    def fiction_score(t):
        dialog = t.count("“") + t.count("”") + t.count("「") + t.count("」")
        return dialog + len(FICTION_SERIAL.findall(t)) * 3 + len(FICTION_SCENE.findall(t)) * 1.2

    def essay_score(t):
        return len(EMOTION_FIRST.findall(t)) + len(EMOTION_WORDS.findall(t)) * 0.8

    fiction, essays = [], []
    for d in docs:
        fid = d["feed_id"]
        t = texts.get(fid, "")
        if not t or fid in skip or len(t) < 150:
            continue
        f, e = fiction_score(t), essay_score(t)
        if f >= 4 and f > e:
            fiction.append(fid)
        elif e >= 3 and not ANNOUNCE.search(t):
            essays.append(fid)
    with open(os.path.join(OUT, "fiction_ids.json"), "w") as f:
        json.dump(fiction, f)
    with open(os.path.join(OUT, "essay_ids.json"), "w") as f:
        json.dump(essays, f)
    print(f"fiction: {len(fiction)} | essays(长文): {len(essays)}")


def stage_fiction_check():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, title_of

    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    texts = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid in wanted:
            doc = json.load(open(path))
            texts[fid] = (body_of(doc) or "").strip() or (title_of(doc) or "").strip()
    essay = set(json.load(open(os.path.join(OUT, "essay_ids.json")))) if os.path.exists(
        os.path.join(OUT, "essay_ids.json")) else set()
    fiction = set(json.load(open(os.path.join(OUT, "fiction_ids.json")))) if os.path.exists(
        os.path.join(OUT, "fiction_ids.json")) else set()
    keep = set()
    for fid in sorted(essay):
        verdict = llm("判断这段帖子属于：A=同人文/小说(有虚构情节或人物对白) "
                      "B=真实情感随笔/感想(作者本人抒情回忆) C=其他(锐评/公告/设定/讨论)。只回复字母。\n\n"
                      + texts.get(fid, "")[:800], max_tokens=10)
        if isinstance(verdict, dict):
            verdict = str(list(verdict.values())[0])[:1]
        else:
            verdict = "C"
        if verdict == "A":
            fiction.add(fid)
        elif verdict == "B":
            keep.add(fid)
    with open(os.path.join(OUT, "fiction_ids.json"), "w") as f:
        json.dump(sorted(fiction), f)
    with open(os.path.join(OUT, "essay_ids.json"), "w") as f:
        json.dump(sorted(keep), f)
    print(f"fiction-check: fiction={len(fiction)}, essays={len(keep)}")


def stage_meitu():
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, media_of, title_of

    cache_path = os.path.join(OUT, "vision_cache.json")
    if not os.path.exists(cache_path):
        print("meitu: no vision_cache.json, skip")
        return
    vision = json.load(open(cache_path))
    docs = load_docs()
    wanted = {d["feed_id"] for d in docs}
    media_file = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        if fid not in wanted:
            continue
        doc = json.load(open(path))
        ms = [m for m in (media_of(doc) or []) if m.get("file")]
        if ms:
            media_file[fid] = ms[0]["file"]
    classes = {}
    for fid in media_file:
        if fid in vision:
            classes[fid] = vision[fid]
    skip = set()
    for name in ("aigc_ids.json", "yuanmu_ids.json", "manga_ids.json"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            skip |= set(json.load(open(path)))
    hits = [f for f, c in classes.items() if c == "2" and f not in skip]
    with open(os.path.join(OUT, "meitu_ids.json"), "w") as f:
        json.dump(hits, f)
    print(f"meitu: {len(hits)} posts (vision class 2)")


def stage_name():
    docs = load_docs()
    topics = [int(x) for x in np.load(os.path.join(OUT, "topics.npy"))]
    size = {int(r["topic_id"]): int(r["size"])
            for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    kws = {int(r["topic_id"]): r["keywords"]
           for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    by_topic = defaultdict(list)
    for d, t in zip(docs, topics):
        if t >= 0:
            by_topic[t].append(d)
    os.makedirs(os.path.join(OUT, "llm_cache"), exist_ok=True)
    names = {}
    for t in sorted(by_topic):
        cache = os.path.join(OUT, "llm_cache", f"naming_{t}.json")
        if os.path.exists(cache):
            names[t] = json.load(open(cache))
            continue
        random.seed(42 + t)
        picked = sorted(random.sample(by_topic[t], min(8, len(by_topic[t]))),
                        key=lambda d: d["month"])
        samples = "\n".join(f"{i+1}. [{d['month']}] {d['text'][:120]}"
                            for i, d in enumerate(picked))
        prompt = (f"你在分析一个QQ频道（动漫《擅长捉弄的高木同学》粉丝社区）的话题聚类结果。\n"
                  f"以下8条是该簇的真实帖子（跨时间抽样）：\n{samples}\n\n"
                  f"该簇 c-TF-IDF 关键词：{kws[t][:120]}\n{GLOSS}\n\n"
                  '请概括这簇的"社区行为类型"（如：求图、夸夸、打卡、资源求助、剧情讨论、'
                  '投票应援、AI生成、活动宣传、同人创作…）。\n'
                  '只输出JSON：{"label":"不超过10字的中文标签",'
                  '"keywords":["3-5个区分性关键词"],"why":"一句话依据"}')
        names[t] = llm(prompt)
        json.dump(names[t], open(cache, "w"), ensure_ascii=False)
        print(f"name #{t}({size[t]}) → {names[t].get('label')}")
    with open(os.path.join(OUT, "topic_names.json"), "w") as f:
        json.dump(names, f, ensure_ascii=False, indent=1)


def stage_merge():
    docs = load_docs()
    topics = np.load(os.path.join(OUT, "topics.npy"))
    names = {int(k): v for k, v in json.load(open(os.path.join(OUT, "topic_names.json"))).items()}
    size = {int(r["topic_id"]): int(r["size"])
            for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    kws = {int(r["topic_id"]): r["keywords"]
           for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    by_topic = defaultdict(list)
    for i, t in enumerate(topics):
        if t >= 0:
            by_topic[int(t)].append(i)
    emb = np.load(os.path.join(OUT, "embeddings.npy"))
    cent = {}
    for t, idx in by_topic.items():
        c = emb[idx].mean(axis=0)
        cent[t] = c / np.linalg.norm(c)

    # 每簇最近 3 邻作为候选（余弦绝对值无区分力：Qwen3 下整体 0.89-0.95）
    import itertools
    pairs = sorted(((float(cent[a] @ cent[b]), a, b)
                    for a, b in itertools.combinations(sorted(cent), 2)), reverse=True)
    cand, seen = [], set()
    for t in sorted(cent):
        k = 0
        for s, a, b in pairs:
            if t in (a, b) and (a, b) not in seen:
                seen.add((a, b))
                cand.append((s, a, b))
                k += 1
                if k >= 3:
                    break

    def block(t):
        random.seed(t + 7)
        idx = random.sample(by_topic[t], min(5, len(by_topic[t])))
        return (f"【{names[t]['label']}】{size[t]}篇 | 依据：{names[t].get('why','')}\n"
                f"关键词：{kws[t][:100]}\n"
                + "\n".join("- " + docs[i]["text"][:70] for i in idx))

    decisions = []
    for s, a, b in cand:
        cache = os.path.join(OUT, "llm_cache", f"merge2_{a}_{b}.json")
        if os.path.exists(cache):
            res = json.load(open(cache))
        else:
            prompt = (f'判断以下两个QQ频道话题簇是否属于同一"社区行为类别"。只看行为类型是否同类，'
                      f'不看具体角色/用词。\n\n簇A：\n{block(a)}\n\n簇B：\n{block(b)}\n\n'
                      f'参考：簇心余弦 {s:.2f}。宁可保守，不确定 → false。\n'
                      '只输出JSON：{"same":true/false,"merged_label":"不超过12字"}')
            res = llm(prompt, max_tokens=200)
            json.dump(res, open(cache, "w"), ensure_ascii=False)
        res.update({"cos": round(s, 4), "a": a, "b": b})
        decisions.append(res)
    json.dump(decisions, open(os.path.join(OUT, "merge_decisions2.json"), "w"),
              ensure_ascii=False, indent=1)
    same = [d for d in decisions if d.get("same")]
    print(f"merge: LLM accepted {len(same)}/{len(decisions)} candidate pairs "
          f"(人工裁定为 {len(GROUPS)} 个行为族，见 GROUPS 常量)")
    print("注意：LLM 的接受对不可直接做传递闭包，否则会坍缩成巨型团块。")


def stage_merge_outputs():
    docs = load_docs()
    topics = [int(x) for x in np.load(os.path.join(OUT, "topics.npy"))]
    names = {int(k): v for k, v in json.load(open(os.path.join(OUT, "topic_names.json"))).items()}
    kws = {int(r["topic_id"]): r["keywords"]
           for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    size = {int(r["topic_id"]): int(r["size"])
            for r in csv.DictReader(open(os.path.join(OUT, "topic_summary.csv")))}
    merged_ids = {m for g in GROUPS.values() for m in g}
    single = {int(t): names[int(t)]["label"] for t in names if int(t) not in merged_ids}
    key_of = {m: g for g, ms in GROUPS.items() for m in ms}
    aigc_path = os.path.join(OUT, "aigc_ids.json")
    aigc_ids = set(json.load(open(aigc_path))) if os.path.exists(aigc_path) else set()
    yuanmu_path = os.path.join(OUT, "yuanmu_ids.json")
    yuanmu_ids = set(json.load(open(yuanmu_path))) if os.path.exists(yuanmu_path) else set()
    yuanmu_ids -= aigc_ids
    manga_path = os.path.join(OUT, "manga_ids.json")
    manga_ids = set(json.load(open(manga_path))) if os.path.exists(manga_path) else set()
    manga_ids -= aigc_ids | yuanmu_ids
    meitu_path = os.path.join(OUT, "meitu_ids.json")
    meitu_ids = set(json.load(open(meitu_path))) if os.path.exists(meitu_path) else set()
    meitu_ids -= aigc_ids | yuanmu_ids | manga_ids
    fiction_path = os.path.join(OUT, "fiction_ids.json")
    fiction_ids = set(json.load(open(fiction_path))) if os.path.exists(fiction_path) else set()
    fiction_ids -= aigc_ids | yuanmu_ids | manga_ids | meitu_ids
    essay_path = os.path.join(OUT, "essay_ids.json")
    essay_ids = set(json.load(open(essay_path))) if os.path.exists(essay_path) else set()
    essay_ids -= aigc_ids | yuanmu_ids | manga_ids | meitu_ids | fiction_ids
    draw_path = os.path.join(OUT, "draw_ids.json")
    draw_ids = set(json.load(open(draw_path))) if os.path.exists(draw_path) else set()
    resource_path = os.path.join(OUT, "resource_ids.json")
    resource_ids = set(json.load(open(resource_path))) if os.path.exists(resource_path) else set()
    request_path = os.path.join(OUT, "request_routing.json")
    request_routing = json.load(open(request_path)) if os.path.exists(request_path) else {}
    music_path = os.path.join(OUT, "music_ids.json")
    music_ids = set(json.load(open(music_path))) if os.path.exists(music_path) else set()
    music_ids -= aigc_ids | yuanmu_ids | manga_ids | meitu_ids
    import glob
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of as _body_of, title_of as _title_of

    feed_text = {}
    for path in glob.glob(os.path.join(ROOT, "data", "*", "feeds", "B_*", "*.json")):
        fid = os.path.basename(path)[:-5]
        doc = json.load(open(path))
        feed_text[fid] = (_body_of(doc) or "").strip() or (_title_of(doc) or "").strip()

    def classify(d, t):
        fid = d["feed_id"]
        if fid in aigc_ids:
            return AIGC_CATEGORY
        if fid in request_routing:
            return request_routing[fid]
        if fid in resource_ids:
            return RESOURCE_CATEGORY
        if fid in draw_ids:
            return DRAW_CATEGORY
        if fid in yuanmu_ids:
            return YUANMU_CATEGORY
        if fid in manga_ids:
            return MANGA_CATEGORY
        if fid in meitu_ids:
            return MEITU_CATEGORY
        if fid in music_ids:
            return MUSIC_CATEGORY
        if fid in fiction_ids:
            return FICTION_CATEGORY
        if fid in essay_ids:
            return EMOTION_CATEGORY
        ft = feed_text.get(fid, d["text"])
        if OTHER_IP.search(ft) and not TAKAGI_HINT.search(ft):
            if re.search(r"禁止|违规|规则|广告|色情|封禁|话题|超管|推广链接", ft):
                return OPS_CATEGORY
            return CROSS_CATEGORY
        key = key_of.get(t, single.get(t))
        key = RENAMES.get(key, key)
        if key == "音乐分享与讨论":
            return MUSIC_CATEGORY
        if key == "高木同人与二创":
            if re.search(r"生日|快乐", ft):
                return "节日/生日祝福"
            if re.search(r"世萌|萌战|投票", ft):
                return "世萌投票应援"
            if re.search(r"发售|情报|初恋展|画集|公式书|BD|特典|周边|商品", ft):
                return "谷子购买/交易"
            if re.search(r"翻唱|唱|歌|曲|声线", ft):
                return MUSIC_CATEGORY
            if re.search(r"剪辑|剪", ft):
                return "视频剪辑分享"
            if re.search(r"画|绘|临摹", ft):
                return DRAW_CATEGORY
            return "夸夸/图片互动"
        if key == "角色关系讨论":
            if REQUEST_ASK.search(ft):
                return "求图/资源求助"
            if ROLE_DISC.search(ft):
                return "原作讨论/情感共鸣"
            if ROLE_PRAISE.search(ft):
                return "夸夸/图片互动"
            return WATER_CATEGORY
        if key == "西片同人创作与求图":
            if XIPIAN_CRE.search(ft):
                return DRAW_CATEGORY
            if REQUEST_ASK.search(ft):
                return "求图/资源求助"
            if XIPIAN_VOTE.search(ft):
                return "世萌投票应援"
            if XIPIAN_DISC.search(ft) and len(ft) > 18:
                return "原作讨论/情感共鸣"
            if XIPIAN_PRAISE.search(ft):
                return "夸夸/图片互动"
            return WATER_CATEGORY
        if key in ("跨圈玩梗/闲聊", "捉弄互动与玩梗"):
            if CROSS_SUBJECT.search(ft):
                return CROSS_CATEGORY
            if WATER_DRAMA.search(ft):
                return OPS_CATEGORY
            if WATER_MERCH.search(ft):
                return "谷子购买/交易"
            if TAKAGI_HINT.search(ft) and len(ft) > 20:
                return "原作讨论/情感共鸣"
            return WATER_CATEGORY
        if key == NOTICE_CATEGORY:
            if NOTICE_CRE.search(ft) and not re.search(r"翻译|搬运|转载", ft):
                return NOTICE_CATEGORY
            if re.search(r"翻译|搬运|转载", ft):
                return "同人创作交流"
            if NOTICE_FAN.search(ft):
                return "同人创作交流"
            if NOTICE_TAKA.search(ft):
                return "原作讨论/情感共鸣"
            return WATER_CATEGORY
        if key == "AI二创/拼豆":
            if BEAD_SIGNAL.search(ft):
                return "同人创作交流"
            if AIFAN_SIGNAL.search(ft):
                return AIFAN_CATEGORY
            return WATER_CATEGORY
        if key in OPS_SOURCE_CATS:
            if RESOURCE_HINT.search(ft):
                return "求图/资源求助"
            if META_HINT.search(ft):
                return OPS_CATEGORY
            return WATER_CATEGORY
        return key

    months = sorted({d["month"] for d in docs})
    cnt, tot = defaultdict(lambda: defaultdict(int)), defaultdict(int)
    doc_category = {}
    for d, t in zip(docs, topics):
        key = classify(d, t)
        if key:
            doc_category[d["feed_id"]] = key
            cnt[key][d["month"]] += 1
            tot[d["month"]] += 1
    with open(os.path.join(OUT, "doc_categories.json"), "w") as f:
        json.dump(doc_category, f, ensure_ascii=False)
    with open(os.path.join(OUT, "topics_over_time_merged.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["topic", "month", "count", "share"])
        for key in cnt:
            for m in months:
                c = cnt[key].get(m, 0)
                w.writerow([key, m, c, round(c / tot[m], 6)])

    cat_docs = defaultdict(list)
    for d, t in zip(docs, topics):
        key = classify(d, t)
        if key:
            cat_docs[key].append(d["text"])

    global_df = defaultdict(int)
    for t in (d["text"] for d in docs):
        for w in set(tokenize(t)):
            global_df[w] += 1

    def top_terms(texts, k=5):
        n_cat = max(len(texts), 1)
        df = defaultdict(int)
        for t in texts:
            for w in set(tokenize(t)):
                df[w] += 1
        scored = [(w, (c / n_cat) / ((global_df[w] + 1) / (len(docs) + 1)))
                  for w, c in df.items() if c >= 3]
        scored.sort(key=lambda x: -x[1])
        return "; ".join(w for w, _ in scored[:k])

    reverse_single = {v: k for k, v in single.items()}
    with open(os.path.join(OUT, "topic_summary_merged.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["topic", "size", "share_total", "keywords", "peak_month"])
        aigc_sources = {}
        if os.path.exists(os.path.join(OUT, "aigc_docs.json")):
            aigc_sources = {r["feed_id"]: r["source"] for r in json.load(open(os.path.join(OUT, "aigc_docs.json")))}

        for key, n in sorted(((k, sum(v.values())) for k, v in cnt.items()),
                             key=lambda x: -x[1]):
            if key == AIGC_CATEGORY:
                kw = AIGC_KEYWORDS
            elif key == YUANMU_CATEGORY:
                kw = YUANMU_KEYWORDS
            elif key == MANGA_CATEGORY:
                kw = MANGA_KEYWORDS
            elif key == MEITU_CATEGORY:
                kw = MEITU_KEYWORDS
            elif key == FICTION_CATEGORY:
                kw = FICTION_KEYWORDS
            elif key == EMOTION_CATEGORY:
                kw = EMOTION_KEYWORDS
            elif key == NOTICE_CATEGORY:
                kw = NOTICE_KEYWORDS
            elif key == OPS_CATEGORY:
                kw = OPS_KEYWORDS
            elif key == MUSIC_CATEGORY:
                kw = MUSIC_KEYWORDS
            elif key == RESOURCE_CATEGORY:
                kw = RESOURCE_KEYWORDS
            elif key == AIFAN_CATEGORY:
                kw = AIFAN_KEYWORDS
            elif key == WATER_CATEGORY:
                kw = WATER_KEYWORDS
            elif key == CROSS_CATEGORY:
                kw = CROSS_KEYWORDS
            else:
                kw = top_terms(cat_docs[key])
            peak = max(months, key=lambda m: cnt[key].get(m, 0) / max(tot[m], 1))
            w.writerow([key, n, round(n / len(docs), 6), kw, peak])
    with open(os.path.join(OUT, "merges.json"), "w") as f:
        json.dump({"groups": GROUPS, "singles": single,
                   "note": "LLM 提议经人工裁定：仅保留同类行为族，拒绝传递闭包"}, f,
                  ensure_ascii=False, indent=1)
    print(f"merged: {len(cnt)} categories, top = {max(cnt, key=lambda k: sum(cnt[k].values()))}")


CHARACTERS = {
    "高木/太太": re.compile(r"高木|太太|Takagi|takagi"),
    "西片": re.compile(r"西片"),
    "小千": re.compile(r"小千"),
    "其他角色": re.compile(r"北条|滨口|木村|真野|中井|小堇|樱子|早苗|美奈|大代|鬼瓦|鹰|堇"),
    "创作者": re.compile(r"稻叶|山本|高桥李依|大原|崇一朗"),
}


def stage_characters():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["SimHei", "FangSong_GB2312"]
    plt.rcParams["axes.unicode_minus"] = False

    docs = load_docs()
    months = sorted({d["month"] for d in docs})
    month_total = Counter(d["month"] for d in docs)
    counts = {name: Counter() for name in CHARACTERS}
    for d in docs:
        for name, pattern in CHARACTERS.items():
            if pattern.search(d["text"]):
                counts[name][d["month"]] += 1

    with open(os.path.join(OUT, "character_trends.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "total"] + list(CHARACTERS))
        for m in months:
            w.writerow([m, month_total[m]] + [counts[n].get(m, 0) for n in CHARACTERS])

    fig, ax = plt.subplots(figsize=(22, 9))
    colors = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e"]
    for (name, _), color in zip(CHARACTERS.items(), colors):
        series = [counts[name].get(m, 0) / max(month_total[m], 1) * 100 for m in months]
        ax.plot(range(len(months)), series, label=name, color=color, lw=2)
    years = [("2022", "2022-01", "2022-12"), ("2023", "2023-01", "2023-12"),
             ("2024", "2024-01", "2024-12"), ("2025", "2025-01", "2025-12"),
             ("2026", "2026-01", "2026-10")]
    midx = {m: i for i, m in enumerate(months)}
    for _, m0, _ in years[1:]:
        ax.axvline(midx[m0] - 0.5, color="k", lw=1.2, ls="--", alpha=0.5)
    ax.set_xticks(range(0, len(months), 2))
    ax.set_xticklabels([months[i] for i in range(0, len(months), 2)], rotation=90, fontsize=8)
    ax.set_ylabel("提及占比 %（当月文档）", fontsize=12)
    ax.set_title("角色提及趋势 · 2022-01 → 2026-10 · 逐月原始值", fontsize=15)
    ax.legend(ncol=5, fontsize=11, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.92, bottom=0.18)
    fig.savefig(os.path.join(OUT, "character_trends.png"), dpi=150)
    print("characters: analysis/character_trends.png")


def stage_chart(n_top=20):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["SimHei", "FangSong_GB2312"]
    plt.rcParams["axes.unicode_minus"] = False

    def sane(s):
        return "".join(c for c in s if ord(c) < 0x1F000 and c not in "˄´`˚¸")

    rows = list(csv.DictReader(open(os.path.join(OUT, "topics_over_time_merged.csv"))))
    summ = {r["topic"]: r for r in csv.DictReader(open(os.path.join(OUT, "topic_summary_merged.csv")))}
    months = sorted({r["month"] for r in rows})
    midx = {m: i for i, m in enumerate(months)}
    n = len(months)
    share = defaultdict(lambda: np.zeros(n))
    for r in rows:
        share[r["topic"]][midx[r["month"]]] = float(r["share"])
    size = {k: int(v["size"]) for k, v in summ.items()}
    kws = {k: sane(v["keywords"].replace("; ", "/"))[:18] for k, v in summ.items()}

    top = sorted(size, key=lambda k: -size[k])[:n_top]
    rest = sorted([k for k in size if k not in top], key=lambda k: -size[k])
    other = np.sum([share[k] for k in rest], axis=0)
    series = [share[k] for k in top] + [other]
    total = np.sum(series, axis=0)
    mat1 = np.vstack([s / total * 100 for s in series])
    lab1 = [f"{k}({size[k]}) · {kws[k]}" for k in top] + ["其他"]
    mat2 = np.vstack([share[k] / np.where(other > 0, other, np.nan) * 100 for k in rest])
    mat2 = np.nan_to_num(mat2)
    mat2 = mat2 / mat2.sum(axis=0) * 100
    lab2 = [f"{k}({size[k]}) · {kws[k]}" for k in rest]

    years = [("2022", "2022-01", "2022-12"), ("2023", "2023-01", "2023-12"),
             ("2024", "2024-01", "2024-12"), ("2025", "2025-01", "2025-12"),
             ("2026", "2026-01", "2026-10")]
    c1 = list(matplotlib.colormaps["tab20"].colors) + ["#555555"]
    c2 = [matplotlib.colormaps["tab20c"](i) for i in range(len(rest))]

    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(28, 17),
                                   gridspec_kw={"height_ratios": [1, 1], "hspace": 0.12})
    fig.subplots_adjust(left=0.05, right=0.775, top=0.94, bottom=0.06)
    ax1.stackplot(range(n), mat1, labels=lab1, colors=c1, alpha=0.92,
                  edgecolor="white", linewidth=0.3)
    ax1.set_ylim(0, 100)
    ax1.set_xlim(0, n - 1)
    ax1.set_ylabel("话题构成 %（全部 9,678 篇）", fontsize=13)
    ax1.legend(ncol=1, fontsize=9.5, loc="upper left", bbox_to_anchor=(1.005, 1.0),
               frameon=False, handlelength=1.2, labelspacing=0.55)
    ax2.stackplot(range(n), mat2, labels=lab2, colors=c2, alpha=0.92,
                  edgecolor="white", linewidth=0.3)
    ax2.set_ylim(0, 100)
    ax2.set_xlim(0, n - 1)
    ax2.set_ylabel("长尾内部构成 %", fontsize=13)
    ax2.set_xticks(range(n))
    ax2.set_xticklabels(months, rotation=90, ha="center", fontsize=8)
    ax2.legend(ncol=1, fontsize=9.5, loc="upper left", bbox_to_anchor=(1.005, 1.0),
               frameon=False, handlelength=1.2, labelspacing=0.6)
    for ax in (ax1, ax2):
        for _, m0, _ in years[1:]:
            ax.axvline(midx[m0] - 0.5, color="k", lw=1.2, ls="--", alpha=0.55)
    trans = ax1.get_xaxis_transform()
    for y, m0, m1 in years:
        ax1.text((midx[m0] + midx[m1]) / 2, 1.02, y, ha="center", va="bottom",
                 fontsize=15, fontweight="bold", transform=trans)
    for ax in (ax1, ax2):
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.suptitle("话题构成 · 逐月原始值 · 2022-01 → 2026-10 · "
                 f"上=Top{n_top}+其他 / 下=长尾",
                 fontsize=16.5, y=0.985)
    path = os.path.join(OUT, "trend_stack_dual.png")
    plt.savefig(path, dpi=150)
    print(f"chart: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["clean", "embed", "cluster", "name", "aigc", "yuanmu", "manga", "meitu", "fiction", "fiction-check", "music", "requests", "resources", "draw", "characters", "merge", "chart", "all"])
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    stages = ["clean", "embed", "cluster", "name", "aigc", "yuanmu", "manga", "meitu", "fiction", "fiction-check", "music", "requests", "resources", "draw", "characters", "merge", "chart"] if args.stage == "all" else [args.stage]
    for s in stages:
        if s == "clean":
            stage_clean()
        elif s == "embed":
            if args.skip_embed and os.path.exists(os.path.join(OUT, "embeddings.npy")):
                print("embed: cached")
            else:
                stage_embed()
        elif s == "cluster":
            stage_cluster()
        elif s == "name":
            stage_name()
        elif s == "aigc":
            stage_aigc()
        elif s == "yuanmu":
            stage_yuanmu()
        elif s == "manga":
            stage_manga()
        elif s == "meitu":
            stage_meitu()
        elif s == "fiction":
            stage_fiction()
        elif s == "fiction-check":
            stage_fiction_check()
        elif s == "music":
            stage_music()
        elif s == "requests":
            stage_requests()
        elif s == "resources":
            stage_resources()
        elif s == "draw":
            stage_draw()
        elif s == "characters":
            stage_characters()
        elif s == "merge":
            stage_merge()
            stage_merge_outputs()
        elif s == "chart":
            stage_chart(args.top)


if __name__ == "__main__":
    sys.exit(main())
