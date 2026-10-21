#!/usr/bin/env python3
"""因果主题生命周期 treemap：按月自回归（transformer 解码器式）——
每月的簇先与历史主题记忆做因果匹配（质心余弦），命中则延续旧主题并更新记忆，
未命中残差诞生新主题；同主题跨月同色，当月新生主题加深色边框。"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from treemap_utils import (squarify, wrap_label, top_keywords, text_color,  # noqa: E402
                           tokenize, GRAY, TAB, PT)

FIG_W_IN = 48.0
AX_L, AX_R, AX_T, AX_B = 0.03, 0.98, 0.88, 0.06

plt.rcParams["font.sans-serif"] = ["SimHei", "FangSong_GB2312"]
plt.rcParams["axes.unicode_minus"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANALYSIS = os.path.join(ROOT, "analysis")

MATCH_SIM = float(os.environ.get("MATCH_SIM", "0.40"))
OUTLIER_SIM = 0.25
MIN_CLUSTER = 2
MAX_K = 9


def month_clusters(idxs, emb):
    from sklearn.cluster import KMeans
    n = len(idxs)
    k = min(MAX_K, max(1, n // 3))
    X = emb[idxs]
    km = KMeans(n_clusters=k, n_init=4, random_state=0).fit(X)
    cent = km.cluster_centers_
    cent /= np.linalg.norm(cent, axis=1, keepdims=True)
    keep = np.einsum("ij,ij->i", X, cent[km.labels_]) >= OUTLIER_SIM
    return [(np.where((km.labels_ == c) & keep)[0], cent[c]) for c in range(k)]


def main():
    docs = [json.loads(line) for line in open(os.path.join(ANALYSIS, "docs_clean.jsonl"))]
    emb_file = os.environ.get("EMB_FILE", "embeddings_bge_m3.npy")
    emb = np.load(os.path.join(ANALYSIS, emb_file))
    emb = emb - emb.mean(0, keepdims=True)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    rows = [json.loads(line) for line in open(os.path.join(ANALYSIS, "coarse_labels.jsonl"))]
    feed_month = {r["feed_id"]: r["month"] for r in rows if r["month"]}
    by_month_idx = defaultdict(list)
    for i, d in enumerate(docs):
        m = feed_month.get(d["feed_id"])
        if m:
            by_month_idx[m].append(i)
    months = sorted(by_month_idx)

    global_df = Counter()
    for d in docs:
        global_df.update(set(tokenize(d.get("text", ""))))
    n_docs = len(docs)

    topics = []
    month_blocks = {}
    for m in months:
        idxs = sorted(by_month_idx[m], key=lambda i: docs[i].get("create_time", 0))
        n = len(idxs)
        contrib = Counter()
        new_ids = set()
        gray = 0
        buffer = []

        def centroids():
            """主题记忆：各主题累积均值质心，L2 归一。"""
            if not topics:
                return None
            c = np.stack([t["emb_sum"] / t["n"] for t in topics])
            return c / np.linalg.norm(c, axis=1, keepdims=True)

        mem = centroids()
        for i in idxs:
            x = emb[i]
            if topics:
                b = int(np.argmax(mem @ x))
                if mem[b] @ x >= MATCH_SIM:
                    t = topics[b]
                    t["emb_sum"] += x
                    t["n"] += 1
                    t["posts"].append((m, i))
                    contrib[b] += 1
                    continue
            buffer.append(i)

        if buffer:
            from sklearn.cluster import KMeans
            if len(buffer) >= 3:
                B = emb[buffer]
                kb = min(6, len(buffer) // 3)
                kb = max(1, kb)
                km = KMeans(n_clusters=kb, n_init=4, random_state=0).fit(B)
                cents = km.cluster_centers_
                cents /= np.linalg.norm(cents, axis=1, keepdims=True)
                for c in range(kb):
                    sel = np.where(km.labels_ == c)[0]
                    if len(sel) >= 3:
                        t = {"id": len(topics), "emb_sum": cents[c] * len(sel),
                             "n": len(sel), "posts": [], "months": Counter()}
                        for j in sel:
                            t["posts"].append((m, buffer[j]))
                        t["months"][m] = len(sel)
                        topics.append(t)
                        contrib[t["id"]] = len(sel)
                        new_ids.add(t["id"])
                    else:
                        for j in sel:
                            buffer_left = None
                            x = emb[buffer[j]]
                            if topics:
                                b2 = int(np.argmax(mem @ x))
                                if mem[b2] @ x >= MATCH_SIM - 0.10:
                                    topics[b2]["emb_sum"] += x
                                    topics[b2]["n"] += 1
                                    topics[b2]["posts"].append((m, buffer[j]))
                                    contrib[b2] += 1
                                    continue
                            gray += 1
            else:
                for i in buffer:
                    x = emb[i]
                    if topics:
                        b2 = int(np.argmax(mem @ x))
                        if mem[b2] @ x >= MATCH_SIM - 0.10:
                            topics[b2]["emb_sum"] += x
                            topics[b2]["n"] += 1
                            topics[b2]["posts"].append((m, i))
                            contrib[b2] += 1
                            continue
                    gray += 1

        for tid in contrib:
            topics[tid]["months"][m] = contrib[tid]
        blocks = [(sz, tid, tid in new_ids) for tid, sz in contrib.items()]
        if gray:
            blocks.append((gray, None, False))
        month_blocks[m] = [(sz / n, tid, new) for sz, tid, new in
                           sorted(blocks, key=lambda v: -v[0])]

    chosen = set()
    for t in sorted(topics, key=lambda v: -v["n"]):
        texts = [docs[i]["text"] for _, i in t["posts"]]
        words = top_keywords(texts, global_df, n_docs, k=10)
        label = next((w for w in words if w not in chosen), None)
        if label is None:
            label = (words[0] + "′") if words else f"主题{t['id']}"
        chosen.add(label)
        t["label"] = label
        t["words"] = words
        t["birth"] = min(t["months"])
        t["death"] = max(t["months"])
        t["peak"] = max(t["months"], key=lambda mm: t["months"][mm])

    tag = "" if emb_file == "embeddings.npy" else "_" + emb_file.replace("embeddings_", "").replace(".npy", "")
    names_path = os.path.join(ANALYSIS, f"topic_names{tag}.json")
    if os.path.exists(names_path):
        names = json.load(open(names_path))
        for t in topics:
            t["label"] = names.get(str(t["id"]), t["label"])

    lifespans = [(t["death"][:4], t["death"][5:]) > (t["birth"][:4], t["birth"][5:])
                 for t in topics]
    span_months = []
    for t in topics:
        mi = months.index(t["birth"])
        mj = months.index(t["death"])
        span_months.append(mj - mi + 1)
    top5 = sorted(topics, key=lambda t: -t["n"])[:5]
    print(f"主题总数 {len(topics)} | 活跃月数中位数 {sorted(span_months)[len(span_months)//2]}"
          f" | 只活1个月 {sum(1 for s in span_months if s==1)}"
          f" | 最大主题 {top5[0]['n']} 帖 | top5 " +
          ", ".join(f"{t['label']}={t['n']}" for t in top5))

    years = sorted({int(m[:4]) for m in months})
    cell_in = (AX_R - AX_L) * FIG_W_IN / 12
    fig_h = cell_in * len(years) / (AX_T - AX_B)
    fig, ax = plt.subplots(figsize=(FIG_W_IN, fig_h))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, len(years))
    ax.invert_yaxis()
    ax.axis("off")
    cell_pt = cell_in * PT

    stats = {"blocks": 0, "labeled": 0, "one": 0, "multi": 0}
    text_items = []
    new_rects = []
    for ci in range(12):
        ax.text(ci + 0.5, -0.34, f"{ci + 1}月", ha="center", va="bottom",
                fontsize=17, color="#444444")
    for ri, y in enumerate(years):
        ax.text(-0.15, ri + 0.5, str(y), ha="right", va="center",
                fontsize=26, fontweight="bold", color="#333333")
        for ci in range(12):
            m = f"{y}-{ci + 1:02d}"
            ax.add_patch(Rectangle((ci + 0.03, ri + 0.03), 0.94, 0.94,
                                   facecolor="white", edgecolor="#d8d8d8", linewidth=0.6))
            if m not in month_blocks:
                continue
            n = len(by_month_idx[m])
            cell_area = 0.94 * 0.94
            items = [(sh * cell_area, (tid, new)) for sh, tid, new in month_blocks[m] if sh > 0]
            rects = []
            squarify(items, ci + 0.03, ri + 0.03, 0.94, 0.94, rects)
            stats["blocks"] += len(rects)
            for rx, ry, rw, rh, (tid, new) in rects:
                if tid is None:
                    rgb = matplotlib.colors.to_rgb(GRAY)
                    word = None
                else:
                    rgb = matplotlib.colors.to_rgb(TAB[topics[tid]["id"] % 20])
                    word = topics[tid]["label"]
                ax.add_patch(Rectangle((rx, ry), rw, rh, facecolor=rgb,
                                       edgecolor="white", linewidth=0.9))
                lines, f = wrap_label(word, rw * cell_pt, rh * cell_pt)
                if lines:
                    stats["labeled"] += 1
                    stats["one"] += sum(map(len, lines)) == 1
                    stats["multi"] += len(lines) > 1
                    t = ax.text(rx + rw / 2, ry + rh / 2, "\n".join(lines), ha="center",
                                va="center", fontsize=f, linespacing=1.3,
                                color=text_color(rgb))
                    text_items.append((t, (rx, ry, rw, rh)))
            ax.text(ci + 0.93, ri + 0.10, f"{n}", ha="right", va="center",
                    fontsize=10, color="#333333",
                    bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.6})
            for rx, ry, rw, rh, (tid, new) in rects:
                if new:
                    new_rects.append((rx, ry, rw, rh))
                    ax.add_patch(Rectangle((rx, ry), rw, rh, facecolor="none",
                                           edgecolor="#222222", linewidth=1.7,
                                           joinstyle="miter"))

    handles = [Rectangle((0, 0), 1, 1, facecolor=GRAY, edgecolor="#aaaaaa"),
               Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="#222222", linewidth=1.2)]
    ax.legend(handles, ["离群帖", "当月新生主题"], loc="upper center",
              bbox_to_anchor=(0.5, -0.03), ncol=2, frameon=False, fontsize=17)
    fig.suptitle("主题生命周期 treemap（按月因果生成：先匹配历史主题记忆，残差诞生新主题·深边框）· "
                 "同主题跨月同色 · 灰=离群帖", fontsize=27, y=0.985)
    fig.subplots_adjust(left=AX_L, right=AX_R, top=AX_T, bottom=AX_B)
    if stats["blocks"]:
        print(f"标签覆盖 {stats['labeled']}/{stats['blocks']} 块 "
              f"({stats['labeled'] / stats['blocks']:.0%}) | 单字 {stats['one']} 多行 {stats['multi']}")
    if os.environ.get("OVERFLOW_CHECK"):
        fig.canvas.draw()
        rend = fig.canvas.get_renderer()
        bad = 0
        for t, (rx, ry, rw, rh) in text_items:
            bb = t.get_window_extent(rend)
            x0, y0 = ax.transData.transform((rx, ry))
            x1, y1 = ax.transData.transform((rx + rw, ry + rh))
            if bb.x0 < min(x0, x1) - 1 or bb.x1 > max(x0, x1) + 1 or \
               bb.y0 < min(y0, y1) - 1 or bb.y1 > max(y0, y1) + 1:
                bad += 1
        print(f"溢出检查 {bad}/{len(text_items)} 块文字越界")
    out = os.path.join(ANALYSIS, "causal_topics.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)

    if os.environ.get("BORDER_CHECK"):
        from PIL import Image
        img = Image.open(out).convert("RGB")
        W, H = img.size

        def to_px(x, y):
            return ((AX_L + x / 12 * (AX_R - AX_L)) * W,
                    ((1 - AX_T) + y / len(years) * (AX_T - AX_B)) * H)

        def dark_at(cx, cy, nx, ny):
            for d in range(-3, 4):
                x, y = int(cx + nx * d), int(cy + ny * d)
                if 0 <= x < W and 0 <= y < H:
                    c = img.getpixel((x, y))
                    if max(c) < 110:
                        return True
            return False

        bad = []
        for (rx, ry, rw, rh) in new_rects:
            edges = {
                "top": lambda t: to_px(rx + 0.02 + t * (rw - 0.04), ry) + (0, -1),
                "bottom": lambda t: to_px(rx + 0.02 + t * (rw - 0.04), ry + rh) + (0, 1),
                "left": lambda t: to_px(rx, ry + 0.02 + t * (rh - 0.04)) + (-1, 0),
                "right": lambda t: to_px(rx + rw, ry + 0.02 + t * (rh - 0.04)) + (1, 0),
            }
            for name, fn in edges.items():
                hits = 0
                for k in range(25):
                    cx, cy, nx, ny = fn(k / 24)
                    hits += dark_at(cx, cy, nx, ny)
                if hits / 25 < 0.8:
                    bad.append((round(rx, 2), round(ry, 2), round(rw, 2), round(rh, 2),
                                name, hits / 25))
        print(f"新生块 {len(new_rects)} 个，缺边 {len(bad)} 处")
        for b in bad[:12]:
            print("  缺口", b)

    with open(os.path.join(ANALYSIS, "topic_month_counts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "n_posts"] + [str(t["id"]) for t in topics])
        for m in months:
            counts = {tid: round(sh * len(by_month_idx[m]))
                      for sh, tid, _ in month_blocks[m] if tid is not None}
            w.writerow([m, len(by_month_idx[m])] + [counts.get(t["id"], 0) for t in topics])

    with open(os.path.join(ANALYSIS, "topic_samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["topic_id", "label", "n_posts", "birth", "death", "hints", "samples"])
        for t in sorted(topics, key=lambda v: -v["n"]):
            seen, samples = set(), []
            for _, i in t["posts"]:
                x = " ".join(docs[i].get("text", "").split())[:110]
                if x and x not in seen:
                    seen.add(x)
                    samples.append(x)
                if len(samples) == 6:
                    break
            w.writerow([t["id"], t["label"], t["n"], t["birth"], t["death"],
                        " ".join(t.get("words", [])[:6]), " || ".join(samples)])

    with open(os.path.join(ANALYSIS, "topic_lifecycle.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["topic_id", "label", "birth", "death", "active_months",
                    "n_posts", "peak_month", "peak_share"])
        for t in topics:
            w.writerow([t["id"], t["label"], t["birth"], t["death"], len(t["months"]),
                        t["n"], t["peak"],
                        f"{t['months'][t['peak']] / len(by_month_idx[t['peak']]):.0%}"])
    print(out)

    short = [t for t in topics if len(t["months"]) <= 4 and t["n"] >= 8]
    short.sort(key=lambda t: -max(t["months"].values()))
    print("短命但当日重要（≤4活跃月、≥8帖）Top10：")
    for t in short[:10]:
        peak_n = max(t["months"].values())
        print(f"  {t['label']:8s} {t['birth']}~{t['death']} "
              f"活跃{len(t['months'])}月 {t['n']}帖 峰值{peak_n}帖@{t['peak']}")


if __name__ == "__main__":
    main()
