#!/usr/bin/env python3
"""用本地常驻 LLM 给因果主题簇起中文名，产出 topic_names[_<suffix>].json（topic_id -> 名称）。"""
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANALYSIS = os.path.join(ROOT, "analysis")
API = "http://127.0.0.1:1234/v1/chat/completions"
WORKERS = 4
PROMPT = """你是社群内容分析助手。下面是一个QQ粉丝频道中某个主题簇的线索词和样例帖子。
请给这个主题起一个 2-6 字的中文名称，要求：
- 具体描述这批帖子在做什么（例：转载声明帖、求原图、考试祝福、谷子到货、世萌投票）
- 不要"闲聊""其他""综合""杂谈"这类空泛词
- 不要标点、不要解释
只输出名称。

线索词：{hints}
样例：
{samples}"""


def clean(raw):
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', line)
    return line[:8]


def name_one(row):
    samples = "\n".join(f"- {s}" for s in row["samples"].split(" || ")[:6])
    body = {"model": "qwen3.5-9b", "temperature": 0.3, "max_tokens": 24,
            "messages": [{"role": "user",
                          "content": PROMPT.format(hints=row.get("hints", ""), samples=samples)}]}
    for _ in range(3):
        try:
            r = requests.post(API, json=body, timeout=90)
            raw = r.json()["choices"][0]["message"].get("content") or ""
            name = clean(raw)
            if name:
                return row["topic_id"], name
        except Exception:
            pass
    return row["topic_id"], ""


DUP_PROMPT = """下面是同类帖子的线索词和样例。已有名称（必须避开）：{used}
请换一个更具体、能与其他名称区分的中文名称（2-6 字），必须点出这批帖子独有的内容
（具体角色/作品/活动/事件），不要用"讨论""分享""杂谈"这类泛词，不要标点，只输出名称。

线索词：{hints}
样例：
{samples}"""


def rename_one(row, used):
    samples = "\n".join(f"- {s}" for s in row["samples"].split(" || ")[:6])
    body = {"model": "qwen3.5-9b", "temperature": 0.4, "max_tokens": 24,
            "messages": [{"role": "user", "content": DUP_PROMPT.format(
                used="、".join(used), hints=row.get("hints", ""), samples=samples)}]}
    for _ in range(3):
        try:
            r = requests.post(API, json=body, timeout=90)
            name = clean(r.json()["choices"][0]["message"].get("content") or "")
            if name and name not in used:
                return name
        except Exception:
            pass
    hints = row.get("hints", "").split()
    return f"{row['label']}主题" if len(row['label']) <= 5 else row["label"][:5]


def dedupe(names, rows, rounds=2):
    by_id = {r["topic_id"]: r for r in rows}
    for _ in range(rounds):
        groups = {}
        for tid, nm in names.items():
            groups.setdefault(nm, []).append(tid)
        dups = [v for v in groups.values() if len(v) > 1]
        if not dups:
            break
        for group in dups:
            used = [names[t] for t in names if names[t] not in (names[group[0]],)]
            for tid in group[1:]:
                names[tid] = rename_one(by_id[tid], used + [names[t] for t in group if names[t] != names[tid]] + [names[group[0]]])
    return names


def main():
    suffix = sys.argv[1] if len(sys.argv) > 1 else ""
    stem = f"_{suffix}" if suffix else ""
    rows = list(csv.DictReader(open(os.path.join(ANALYSIS, "topic_samples.csv"))))
    with ThreadPoolExecutor(WORKERS) as ex:
        out = dict(ex.map(name_one, rows))
    names = {k: v for k, v in out.items() if v}
    names = dedupe(names, rows)
    json.dump(names, open(os.path.join(ANALYSIS, f"topic_names{stem}.json"), "w"),
              ensure_ascii=False, indent=0)
    dup = {}
    for k, v in names.items():
        dup.setdefault(v, []).append(k)
    dup = {k: v for k, v in dup.items() if len(v) > 1}
    print(f"命名 {len(names)}/{len(rows)}，重复 {len(dup)} 组: {list(dup.items())[:5]}")
    by_id = {r["topic_id"]: r for r in rows}
    for r in sorted(rows, key=lambda r: -int(r["n_posts"]))[:40]:
        print(f"  {r['n_posts']:>4}帖 {r['birth']}~{r['death']}  "
              f"{r['label']:6s} -> {names.get(r['topic_id'], '?')}")


if __name__ == "__main__":
    sys.exit(main())
