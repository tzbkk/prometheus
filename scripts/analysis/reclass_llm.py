#!/usr/bin/env python3
"""全量帖子重鉴定 v2：多模态（帖文+评论区+图片）、置信度、低置信升级队列。

用法:
  python scripts/reclass_llm.py --build-inputs
  python scripts/reclass_llm.py --limit 40 --workers 4
  python scripts/reclass_llm.py --workers 6
  python scripts/reclass_llm.py --report
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANALYSIS = os.path.join(ROOT, "analysis")
SHARDS = os.path.join(ANALYSIS, "reclass_shards")
CACHE_ROOT = os.path.join(ANALYSIS, "llm_cache")
URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"
ESCALATE_BELOW = 0.6

CATEGORIES = {
    "水聊/碎片互动": "无主题短句、问候、感叹、进度播报、扩列",
    "求图/资源求助": "求图、求原图、求壁纸头像、求软件、求网址、求漫画资源等请求",
    "原作讨论/情感共鸣": "讨论剧情、角色、设定，或观后感",
    "夸夸/图片互动": "看图后的夸赞、评价、吐槽（有评价文案）",
    "情感表达(非虚构)": "第一人称抒情、回忆、真实经历",
    "谷子购买/交易": "周边、谷子、漫画、手办购买、价格、到货、出物",
    "追番进度/续作期待": "动画/剧场版进度、第几季、完结感怀、期待续作",
    "同人绘画": "自己画的画、绘画过程/点评",
    "AIGC内容(生成+助手)": "AI生成的成品图/文、频道助手AI日报/拟人号",
    "节日/生日祝福": "节日、生日祝福与贺图",
    "每日打卡/仪式": "喜欢高木第N天、幻想第N天、三刷打卡等系列",
    "画师推图": "山本/稻叶等画师推特、贺图搬运（带署名）",
    "美图分享(插画)": "无文案纯图分享，或只有画师署名",
    "考试祈福与加油": "中考/高考/期末祈福、加油、成绩",
    "漫画页/话数搬运": "整话或多页漫画、单页漫画分享",
    "同人创作交流": "同人创作讨论、求更、投稿、翻译搬运",
    "同人文(长篇)": "同人小说/连载正文（叙事体）",
    "视频剪辑分享": "自制剪辑、AMV、混剪",
    "频道运维/社区": "频道活动、规则、管理、审核、纠纷、引流、周年庆",
    "音乐分享(含视频)": "歌曲、OP/ED、翻唱、演奏、求歌名、歌单",
    "原/元高木追更": "《(元)高木同学》婚后篇的追更与讨论",
    "圣地巡礼与打卡": "小豆岛等圣地巡礼、旅行记录",
    "世萌投票应援": "世萌/萌战投票、拉票、应援",
    "原作衍生讨论": "小千、北条等衍生角色/衍生作品讨论",
    "校园生活闲聊": "开学、假期、军训、校园日常",
    "同人公告(更新/预告)": "更新预告、新作宣发、连载公告",
    "AI工具/二创实验": "AI工具、模型、提示词的实验与讨论（非成品）",
    "跨圈玩梗": "以其他IP/游戏/其他圈子为主体的玩梗闲聊",
    "资源分享(网盘/链接)": "分享网盘链接、提取码、资源站、压缩包",
}

GLOSSARY = "捉弄=IP核心设定（高木捉弄西片）；原高木=婚后篇外传；小千=婚后篇女儿；太太=高木；谷子=周边；世萌=萌战；扭=发糖。"


def build_prompt(post):
    lines = "\n".join(f"- {name}：{desc}" for name, desc in CATEGORIES.items())
    return (
        "你是QQ频道（动漫《擅长捉弄的高木同学》粉丝社区）的帖子分类器。\n"
        f"{GLOSSARY}\n\n类别定义（只能选一个）：\n{lines}\n\n"
        "判定优先级：请求图/资源/软件/歌名→求图/资源求助；分享网盘→资源分享；"
        "自己的创作→对应创作类；有评价文案的看图帖→夸夸/图片互动；"
        "无主题且短→水聊/碎片互动；主体是别的IP/游戏→跨圈玩梗；其余按主题。\n\n"
        f"帖子正文：{post['feed'][:400]}\n"
        f"评论区（最多5条）：{post['comments'][:400]}\n"
        f"附图为该帖前{len(post['images'])}张图片。\n\n"
        "如果信息不足、图文矛盾或跨多类无法确定，category 输出“不确定”，confidence 低于 0.5。\n"
        '只输出JSON：{"category":"类别名","confidence":0.0-1.0,"reason":"不超过20字"}'
    )


def image_payload(path):
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((384, 384))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def classify(post, retry=True):
    content = [{"type": "text", "text": build_prompt(post)}]
    for path in post["images"][:2]:
        try:
            content.append(image_payload(path))
        except Exception:
            pass
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": content}],
                       "temperature": 0, "max_tokens": 80}).encode()
    try:
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as resp:
            raw = json.load(resp)["choices"][0]["message"]["content"]
    except Exception:
        return {"category": "不确定", "confidence": 0.0, "reason": "请求失败"}
    raw = re.sub(r"<think.*?</think.*?>", "", raw, flags=re.S)
    match = re.search(r"\{.*\}", raw, re.S)
    parsed = {}
    if match:
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            parsed = {}
    label = str(parsed.get("category", "")).strip()
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if label not in CATEGORIES and label != "不确定":
        for name in CATEGORIES:
            if name in raw:
                label = name
                break
        else:
            if retry:
                return classify(post, retry=False)
            return {"category": "不确定", "confidence": 0.0, "reason": "输出无法解析"}
    return {"category": label, "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(parsed.get("reason", ""))[:40]}


def build_inputs():
    sys.path.insert(0, ROOT)
    from src.entity_store import body_of, iter_window, media_of, text_of, title_of

    docs = [json.loads(line) for line in open(os.path.join(ANALYSIS, "docs_clean.jsonl"))]
    wanted = {d["feed_id"] for d in docs}
    feeds, comments = {}, {}
    for entry in iter_window(os.path.join(ROOT, "data"), "7743321643036658", 0, 1 << 62):
        fid = entry.path.stem if entry.kind == "feed" else (entry.doc.get("_p") or {}).get("feed_id")
        if fid not in wanted:
            continue
        if entry.kind == "feed":
            paths = []
            for media in media_of(entry.doc) or []:
                name = media.get("file")
                if name:
                    paths.append(os.path.join(ROOT, "data", "7743321643036658", "media", name[:2], name))
            feeds[fid] = {
                "feed": (body_of(entry.doc) or "").strip() or (title_of(entry.doc) or "").strip(),
                "images": paths[:2],
            }
        elif fid:
            comments.setdefault(fid, []).append(text_of(entry.doc))
    months = {d["feed_id"]: d["month"] for d in docs}
    with open(os.path.join(SHARDS, "inputs.jsonl"), "w") as f:
        for fid in sorted(feeds):
            f.write(json.dumps({
                "feed_id": fid,
                "month": months.get(fid, ""),
                "feed": feeds[fid]["feed"],
                "comments": " / ".join(comments.get(fid, [])[:5]),
                "images": feeds[fid]["images"],
            }, ensure_ascii=False) + "\n")
    print(f"inputs: {len(feeds)} posts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-inputs", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--tag", default="main")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.build_inputs:
        build_inputs()
        return

    posts = [json.loads(line) for line in open(os.path.join(SHARDS, "inputs.jsonl"))]
    rules = json.load(open(os.path.join(ANALYSIS, "doc_categories.json")))
    cache_dir = os.path.join(CACHE_ROOT, f"reclass_{args.tag}")
    os.makedirs(cache_dir, exist_ok=True)

    if args.report:
        results = {}
        for root, _, files in os.walk(CACHE_ROOT):
            if "reclass" not in os.path.basename(root):
                continue
            for name in files:
                if not name.endswith(".json"):
                    continue
                data = json.load(open(os.path.join(root, name)))
                if isinstance(data, dict):
                    results[name[:-5]] = data
        confs = [r.get("confidence", 0) for r in results.values()]
        agree = sum(1 for fid, r in results.items() if r.get("category") == rules.get(fid))
        print(f"已判 {len(results)} | 与规则一致 {agree} ({agree / max(len(results), 1) * 100:.1f}%)")
        print(f"置信度: 均值 {sum(confs)/max(len(confs),1):.2f} | <0.6: {sum(1 for c in confs if c < 0.6)} | 不确定: {sum(1 for r in results.values() if r.get('category')=='不确定')}")
        print("标签分布:", Counter(r.get("category") for r in results.values()).most_common(10))
        return

    posts = posts[args.shard::args.shards] if args.shards > 1 else posts
    pending = [p for p in posts if not os.path.exists(os.path.join(cache_dir, f"{p['feed_id']}.json"))]
    if args.limit:
        pending = pending[: args.limit]
    print(f"待判 {len(pending)} / {len(posts)} | workers={args.workers}", flush=True)

    escalate = []
    started = time.time()
    done = 0

    def work(post):
        result = classify(post)
        with open(os.path.join(cache_dir, f"{post['feed_id']}.json"), "w") as f:
            json.dump(result, f, ensure_ascii=False)
        return result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, p): p for p in pending}
        for future in as_completed(futures):
            done += 1
            post = futures[future]
            try:
                result = future.result()
                if result["category"] == "不确定" or result["confidence"] < ESCALATE_BELOW:
                    escalate.append({"feed_id": post["feed_id"], "month": post["month"],
                                     "rule": rules.get(post["feed_id"], ""), **result})
            except Exception as exc:
                print(f"  ERR {post['feed_id']}: {str(exc)[:60]}", flush=True)
            if done % 25 == 0:
                rate = done / (time.time() - started)
                print(f"  {done}/{len(pending)} | {rate:.2f} 帖/s | 剩余 {(len(pending)-done)/max(rate,0.01)/60:.1f} 分钟", flush=True)
    if escalate:
        with open(os.path.join(SHARDS, f"escalation_{args.tag}.jsonl"), "w") as f:
            for item in escalate:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"完成 {done} | 升级队列 {len(escalate)} | 用时 {(time.time()-started)/60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    sys.exit(main())
