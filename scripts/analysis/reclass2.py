#!/usr/bin/env python3
"""重鉴定 v2：8 粗类 + 5 正交标签（学术锚定体系，见 analysis/reclass_shards/SPEC2.md）。

用法:
  python scripts/reclass2.py --gold --workers 4      # 本地 LLM 判金标 58 条
  python scripts/reclass2.py --workers 6             # 全量
  python scripts/reclass2.py --compare               # 金标对比报告
  python scripts/reclass2.py --coarse-stats          # 旧细类→粗类分布（图表用）
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
from collections import Counter, defaultdict
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
    "同人创作": "自己产出的新内容：同人文/绘画(含临摹WIP)/剪辑AMV/翻唱演奏/手作定制(痛枪拼豆黏土)/AI生成成品图文。判据:有创造性投入(OECD),非纯搬运",
    "搬运策展": "转发整理搬运既有内容,且帖文只有来源/标题/署名/一句转发说明(如原高木249/分享图片/原作者:xxx):画师推图/漫画页整话/官方资讯海报/原曲安利歌单/翻译转载。若附有自己的实质评论感想→言说讨论",
    "求助与供享": "求图求原图壁纸头像/求软件网址/求漫画资源/求歌名;主动供图供资源('要哪张吱一声'/图包发放/经验攻略/网盘链接资源包发放)。资源包整包发放归此类,单曲单图安利归搬运策展",
    "谷子交易": "周边谷子漫画手办的购买/到货/价格/出物/换谷/代购拼车/晒谷",
    "言说讨论": "针对作品/剧情/角色/画面的实质评价或讨论(有具体内容,非单纯感叹):剧情讨论/观后感/夸夸评价/追番感想/原高木追更/跨圈讨论。单纯邀请组队/问候/祝福/打卡/格言/一句感叹→仪式寒暄;配自己作品问咋样/求评价→同人创作",
    "仪式寒暄": "无信息增量的在场宣告与联结:问候水聊碎片/每日打卡系列(喜欢第N天)/节日生日祝福/表情包寒暄。判据:删掉不损失信息,无具体评价对象",
    "社区运维": "频道活动/规则/管理/审核/纠纷/引流/周年庆/世萌投票应援动员/频道AI助手日报",
    "自我展演": "第一人称实质性生活与情感叙事(有具体经历或感受,不能只是一句状态):抒情回忆/真实经历/校园生活/考试祈福/圣地巡礼游记。一句话状态归仪式寒暄",
}

TAGS = {
    "定向互动": "@/回复/点名特定人",
    "AI生成": "内容为AI生成或AI工具实验",
    "节日节点": "日历驱动的节庆时刻",
    "跨圈主体": "主体是其他IP/游戏/圈子",
    "官方信源": "搬运内容来自官方/画师/制作方",
}

RULE_TAGS = {
    "AI生成": r"AI生成|AI绘画|AI画|ai生成|AI图|豆包|sora|即梦|stable.?diffusion|midjourney",
    "节日节点": r"新年|圣诞|春节|元旦|中秋|端午|万圣|情人节|七夕|生日|周年|快乐|祝福",
    "跨圈主体": r"火影|原神|鸣潮|崩铁|明日方舟|奥特曼|宝可梦|咒术|鬼灭|孤独摇滚|轻音|蜡笔小新|哆啦|海贼|柯南",
    "官方信源": r"官号|公式|官方|山本|稻叶|推图|推特|转载|搬运|原画|版权",
    "定向互动": r"@|回复\s*[:：]|楼主|层主|楼上",
}
PRE_RULES = [
    ("社区运维", r"^\d{1,2}月\d{1,2}日频道|发生了什么"),
    ("社区运维", r"(世萌|萌战|b萌|B萌).*?(投票|拉票|投一投|赛程|预选赛|表演赛)"),
    ("社区运维", r"频道改版|频道公告|活动规则|本频道|周年庆|版规|审核|^新?世萌"),
    ("谷子交易", r"出物|收物|代购|拼车|尾款|闲鱼|吧唧|立牌|手办|到货|发货|多少钱|价格"),
    ("求助与供享", r"求图|求资源|求歌名|求个|求原图|求壁纸|求头像|谁有|谁知道|谁给|有没有人|有人有|有人能|有人会|有人吗|哪个好|哪个当|来点|帮我|帮忙|发我|私发|自取|有需要|分享一点|要哪张|给点"),
    ("搬运策展", r"^原[高木元高木]*\d+|^分享图片|原作者\s*[:：]|推图|^\s*转载"),
    ("仪式寒暄", r"有没有.{0,6}(一起|玩|打)|有人一起|一起玩|一起打|开黑|第[一二三四五六七八九十0-9]+天"),
    ("同人创作", r"同人.{0,8}(更新|连载|预告|章节)|(更新|连载|章节).{0,8}同人|同人公告"),
]
MODE = "cat"

GLOSSARY = "捉弄=IP核心设定(高木捉弄西片);原高木=婚后篇外传;小千=婚后篇女儿;太太=高木;谷子=周边;世萌=萌战;扭=发糖。"

# 旧 29 细类 → 8 粗类（映射表定案，v1 体系迁移用）
COARSE_OF_FINE = {
    "同人绘画": "同人创作", "同人文(长篇)": "同人创作", "视频剪辑分享": "同人创作",
    "同人创作交流": "同人创作", "音乐分享(含视频)": "同人创作",
    "AI工具/二创实验": "同人创作", "AIGC内容(生成+助手)": "同人创作",
    "画师推图": "搬运策展", "漫画页/话数搬运": "搬运策展",
    "美图分享(插画)": "搬运策展", "同人公告(更新/预告)": "同人创作",
    "求图/资源求助": "求助与供享", "资源分享(网盘/链接)": "求助与供享",
    "谷子购买/交易": "谷子交易",
    "原作讨论/情感共鸣": "言说讨论", "夸夸/图片互动": "言说讨论",
    "追番进度/续作期待": "言说讨论", "原/元高木追更": "言说讨论",
    "原作衍生讨论": "言说讨论", "跨圈玩梗": "言说讨论",
    "水聊/碎片互动": "仪式寒暄", "节日/生日祝福": "仪式寒暄", "每日打卡/仪式": "仪式寒暄",
    "频道运维/社区": "社区运维", "世萌投票应援": "社区运维",
    "情感表达(非虚构)": "自我展演", "考试祈福与加油": "自我展演",
    "圣地巡礼与打卡": "自我展演", "校园生活闲聊": "自我展演",
}


def build_prompt(post):
    lines = "\n".join(f"- {name}：{desc}" for name, desc in CATEGORIES.items())
    tag_lines = "\n".join(f"- {name}：{desc}" for name, desc in TAGS.items())
    return (
        "你是QQ频道（动漫《擅长捉弄的高木同学》粉丝社区）的帖子分类器。"
        "把帖子判到 8 个互斥粗类之一（按主导功能），并可叠加正交标签。\n"
        f"{GLOSSARY}\n\n粗类定义：\n{lines}\n\n正交标签（可多选可空）：\n{tag_lines}\n\n"
        "判定优先级（依次下判）：\n"
        "1 求资源或供资源(请求/发放)→求助与供享；2 谷子买卖交换晒→谷子交易；"
        "3 自己产出的新内容(含AI成品/手作)→同人创作(加AI生成标签)；"
        "4 搬运转发既有内容且帖文仅有来源/标题/署名→搬运策展(官方来源加官方信源标签;附实质评论则归言说讨论)；"
        "5 频道管理/活动动员/AI日报→社区运维；"
        "6 无信息量的寒暄/打卡/节日/表情包(删掉不损失信息)→仪式寒暄(节日时刻加节日节点标签)；"
        "7 第一人称实质性生活情感叙事(有具体经历或感受)→自我展演；"
        "8 有共享所指的评价讨论→言说讨论(他IP主体加跨圈主体标签)。\n"
        "作者判据:判断自己产出看发布者是否为作者。转发他人画作/演奏/视频/截图→搬运策展,"
        "即使文案带感叹或描述;发布者明确自制才归同人创作。\n"
        "边界：自制翻唱演奏→同人创作,纯原曲安利→搬运策展;频道AI日报→社区运维,成员AI成品→同人创作;"
        "考试祈福→自我展演,节日祝福→仪式寒暄;巡礼游记→自我展演,巡礼打卡一句话→仪式寒暄。\n\n"
        f"帖子正文：{post['feed'][:400]}\n"
        f"评论区（最多5条）：{post['comments'][:400]}\n"
        f"附图为该帖前{len(post['images'])}张图片。\n\n"
        "信息不足/图文矛盾时 confidence 低于 0.5。\n"
        '只输出JSON：{"coarse":"粗类名","tags":["标签名"],"confidence":0.0-1.0,"reason":"不超过20字"}'
    )


def pre_rule(post):
    text = re.sub(r"\s+", "", post["feed"])
    for cat, pattern in PRE_RULES:
        if re.search(pattern, text):
            return cat
    return None


def rule_tags(post):
    text = post["feed"] + " / " + post.get("comments", "")
    return [name for name, pattern in RULE_TAGS.items() if re.search(pattern, text, re.I)]


def build_prompt_cat(post):
    return build_prompt(post).rsplit("信息不足", 1)[0] + "只回复一个类别名，不要JSON、不要解释。"


def image_payload(path):
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((320, 320))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def classify_cat(post):
    hit = pre_rule(post)
    if hit:
        return {"coarse": hit, "tags": rule_tags(post), "confidence": None, "reason": "pre_rule"}
    content = [{"type": "text", "text": build_prompt_cat(post)}]
    for path in post["images"][:2]:
        try:
            content.append(image_payload(path))
        except Exception:
            pass
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": content}],
                       "temperature": 0, "max_tokens": 16}).encode()
    try:
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as resp:
            raw = json.load(resp)["choices"][0]["message"]["content"].strip()
    except Exception:
        return {"coarse": "不确定", "tags": [], "confidence": None, "reason": "请求失败"}
    hits = [name for name in CATEGORIES if name in raw]
    if len(hits) == 1 and len(raw) <= 12:
        return {"coarse": hits[0], "tags": rule_tags(post), "confidence": None, "reason": ""}
    reason = "多个类别" if len(hits) > 1 else "输出无法解析"
    return {"coarse": "不确定", "tags": [], "confidence": None, "reason": reason}


def classify(post, retry=True):
    if MODE == "cat":
        return classify_cat(post)
    content = [{"type": "text", "text": build_prompt(post)}]
    for path in post["images"][:2]:
        try:
            content.append(image_payload(path))
        except Exception:
            pass
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": content}],
                       "temperature": 0, "max_tokens": 100}).encode()
    try:
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as resp:
            raw = json.load(resp)["choices"][0]["message"]["content"]
    except Exception:
        return {"coarse": "不确定", "tags": [], "confidence": 0.0, "reason": "请求失败"}
    raw = re.sub(r"<think.*?</think.*?>", "", raw, flags=re.S)
    match = re.search(r"\{.*\}", raw, re.S)
    parsed = {}
    if match:
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            parsed = {}
    label = str(parsed.get("coarse", "")).strip()
    tags = [t for t in (parsed.get("tags") or []) if t in TAGS]
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if label not in CATEGORIES:
        for name in CATEGORIES:
            if name in raw:
                label = name
                break
        else:
            if retry:
                return classify(post, retry=False)
            return {"coarse": "不确定", "tags": [], "confidence": 0.0, "reason": "输出无法解析"}
    return {"coarse": label, "tags": tags, "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(parsed.get("reason", ""))[:40]}


def run(posts, tag, workers):
    cache_dir = os.path.join(CACHE_ROOT, f"reclass_{tag}")
    os.makedirs(cache_dir, exist_ok=True)
    pending = [p for p in posts if not os.path.exists(os.path.join(cache_dir, f"{p['feed_id']}.json"))]
    print(f"待判 {len(pending)} / {len(posts)} | workers={workers}", flush=True)
    escalate, done, started = [], 0, time.time()

    def work(post):
        result = classify(post)
        with open(os.path.join(cache_dir, f"{post['feed_id']}.json"), "w") as f:
            json.dump(result, f, ensure_ascii=False)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, p): p for p in pending}
        for future in as_completed(futures):
            done += 1
            post = futures[future]
            try:
                result = future.result()
                conf = result.get("confidence")
                if result["coarse"] == "不确定" or (conf is not None and conf < ESCALATE_BELOW):
                    escalate.append({"feed_id": post["feed_id"], **result})
            except Exception as exc:
                print(f"  ERR {post['feed_id']}: {str(exc)[:60]}", flush=True)
            if done % 25 == 0:
                rate = done / (time.time() - started)
                print(f"  {done}/{len(pending)} | {rate:.2f} 帖/s", flush=True)
    if escalate:
        with open(os.path.join(SHARDS, f"escalation_{tag}.jsonl"), "w") as f:
            for item in escalate:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"完成 {done} | 升级队列 {len(escalate)} | 用时 {(time.time()-started)/60:.1f} 分钟", flush=True)


def compare(tag="gold2"):
    gold2 = {}
    path = os.path.join(SHARDS, tag + "_labels.jsonl")
    if os.path.exists(path):
        for line in open(path):
            d = json.loads(line)
            gold2[d["feed_id"]] = d
    local = {}
    cache_dir = os.path.join(CACHE_ROOT, "reclass_" + tag)
    for name in os.listdir(cache_dir):
        if name.endswith(".json"):
            local[name[:-5]] = json.load(open(os.path.join(cache_dir, name)))
    fine_gold = {}
    path = os.path.join(SHARDS, "gold_labels.jsonl")
    if os.path.exists(path):
        for line in open(path):
            d = json.loads(line)
            fine_gold[d["feed_id"]] = d["category"]
    fids = sorted(set(local) & (set(gold2) if gold2 else set(local)))
    print(f"本地LLM {len(local)} | agent金标v2 {len(gold2)} | 旧细类金标 {len(fine_gold)} | 评测集 {len(fids)}")

    if gold2 and fids:
        ok = sum(1 for f in fids if local[f]["coarse"] == gold2[f]["coarse"])
        print(f"\n[主指标] 本地LLM vs agent金标v2: {ok}/{len(fids)} = {ok/len(fids)*100:.1f}%")
        pairs = Counter((gold2[f]["coarse"], local[f]["coarse"]) for f in fids if local[f]["coarse"] != gold2[f]["coarse"])
        for (g, l), n in pairs.most_common(8):
            print(f"  错判: 金标[{g}] ← LLM[{l}] ×{n}")
        buckets = defaultdict(lambda: [0, 0])
        for f in fids:
            b = round(local[f].get("confidence", 0) * 5) / 5
            buckets[b][1] += 1
            if local[f]["coarse"] == gold2[f]["coarse"]:
                buckets[b][0] += 1
        print("置信度校准:", {f"{b:.1f}": f"{ok_}/{n}" for b, (ok_, n) in sorted(buckets.items())})
        overlap = sum(1 for f in fids if set(local[f].get("tags", [])) & set(gold2[f].get("tags", [])))
        exact = sum(1 for f in fids if set(local[f].get("tags", [])) == set(gold2[f].get("tags", [])))
        print(f"标签: 至少1个交集 {overlap}/{len(fids)} | 完全一致 {exact}/{len(fids)}")

    mapped = {f: COARSE_OF_FINE.get(c, "?") for f, c in fine_gold.items()} if fine_gold else {}
    common = [f for f in fids if f in mapped]
    if common:
        ok_m = sum(1 for f in common if local[f]["coarse"] == mapped[f])
        print(f"\n[参考·类别级映射口径，未帖级重判] 本地LLM vs 旧金标映射: {ok_m}/{len(common)} = {ok_m/len(common)*100:.1f}%")
        if gold2:
            ok_g = sum(1 for f in common if gold2[f]["coarse"] == mapped[f])
            print(f"[参考·类别级映射口径，未帖级重判] agent金标v2 vs 旧金标映射: {ok_g}/{len(common)} = {ok_g/len(common)*100:.1f}%")


def coarse_stats():
    rules = json.load(open(os.path.join(ANALYSIS, "doc_categories.json")))
    counts = Counter(COARSE_OF_FINE.get(c, "未映射") for c in rules.values())
    total = sum(counts.values())
    for cat, n in counts.most_common():
        print(f"  {cat}: {n} ({n/total*100:.1f}%)")
    with open(os.path.join(ANALYSIS, "coarse_map.json"), "w") as f:
        json.dump({"coarse_of_fine": COARSE_OF_FINE,
                   "counts": dict(counts), "total": total}, f, ensure_ascii=False, indent=1)
    print(f"合计 {total} | 已导出 analysis/coarse_map.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--gold", action="store_true", help="只判金标样本58条")
    parser.add_argument("--tag", default="")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--coarse-stats", action="store_true")
    parser.add_argument("--mode", choices=["cat", "json"], default="cat")
    parser.add_argument("--input", default="")
    args = parser.parse_args()
    global MODE
    MODE = args.mode

    if args.compare:
        compare(args.tag or "gold2")
        return
    if args.coarse_stats:
        coarse_stats()
        return
    if not args.tag:
        args.tag = "gold2" if args.gold else "main2"
    source = args.input or ("gold_sample.jsonl" if args.gold else "inputs.jsonl")
    posts = [json.loads(line) for line in open(os.path.join(SHARDS, source))]
    run(posts, args.tag, args.workers)


if __name__ == "__main__":
    sys.exit(main())
