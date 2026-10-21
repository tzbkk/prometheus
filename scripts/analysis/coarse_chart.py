#!/usr/bin/env python3
import csv
import json
import os
from collections import Counter, defaultdict
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANALYSIS = os.path.join(ROOT, "analysis")
sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))
ORDER = ["搬运策展", "言说讨论", "仪式寒暄", "同人创作", "求助与供享", "谷子交易", "社区运维", "自我展演", "不确定"]
COLORS = {
    "搬运策展": "#4e79a7", "言说讨论": "#f28e2b", "仪式寒暄": "#59a14f",
    "同人创作": "#e15759", "求助与供享": "#76b7b2", "谷子交易": "#edc948",
    "社区运维": "#b07aa1", "自我展演": "#9c755f", "不确定": "#bab0ac",
}
LABELS = {"不确定": "未定"}


def main():
    rows = [json.loads(line) for line in open(os.path.join(ANALYSIS, "coarse_labels.jsonl"))]
    months = sorted({r["month"] for r in rows if r["month"]})
    midx = {m: i for i, m in enumerate(months)}
    counts = defaultdict(lambda: np.zeros(len(months)))
    for r in rows:
        if r["month"]:
            counts[r["coarse"]][midx[r["month"]]] += 1
    order = [k for k in ORDER if k in counts]
    texts = {json.loads(line)["feed_id"]: json.loads(line).get("text", "")
             for line in open(os.path.join(ANALYSIS, "docs_clean.jsonl"))}
    from treemap_utils import tokenize
    members = defaultdict(list)
    for r in rows:
        members[r["coarse"]].append(texts.get(r["feed_id"], ""))
    global_df = Counter()
    for t in texts.values():
        global_df.update(set(tokenize(t)))
    n_docs = len(texts)
    kw = {}
    for cat in order:
        df = Counter()
        for t in members[cat]:
            df.update(set(tokenize(t)))
        scored = [(w, (c / max(len(members[cat]), 1)) / ((global_df[w] + 1) / (n_docs + 1)))
                  for w, c in df.items() if c >= 3]
        scored.sort(key=lambda x: -x[1])
        kw[cat] = "/".join(w for w, _ in scored[:4])
    total = sum(counts.values())
    share = {k: np.divide(v, np.where(total > 0, total, np.nan)) * 100 for k, v in counts.items()}

    with open(os.path.join(ANALYSIS, "coarse_trend.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "total"] + ORDER)
        for m in months:
            i = midx[m]
            w.writerow([m, int(total[i])] + [int(counts[k][i]) for k in ORDER])

    plt.rcParams["font.sans-serif"] = ["SimHei", "FangSong_GB2312"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(26, 15),
                                   gridspec_kw={"height_ratios": [1, 1], "hspace": 0.13})
    fig.subplots_adjust(left=0.055, right=0.80, top=0.93, bottom=0.07)
    x = np.arange(len(months))
    zero = np.zeros(len(months))
    ax1.stackplot(x, [counts[k] for k in order], labels=[f"{LABELS.get(k, k)} ({int(counts[k].sum())}) · {kw[k]}" for k in order],
                  colors=[COLORS[k] for k in order], alpha=0.92, edgecolor="white", linewidth=0.3)
    ax2.stackplot(x, [share[k] for k in order], labels=[f"{LABELS.get(k, k)} ({int(counts[k].sum())}) · {kw[k]}" for k in order],
                  colors=[COLORS[k] for k in order], alpha=0.92, edgecolor="white", linewidth=0.3)
    ax2.set_ylim(0, 100)
    ax1.set_ylabel("发帖量（篇/月）", fontsize=13)
    ax2.set_ylabel("月度构成占比 %", fontsize=13)
    ax1.set_xlim(0, len(months) - 1)
    ax2.set_xlim(0, len(months) - 1)
    ax2.set_xticks(x)
    ax2.set_xticklabels(months, rotation=90, ha="center", fontsize=8.5)
    years = [("2022", "2022-01", "2022-12"), ("2023", "2023-01", "2023-12"),
             ("2024", "2024-01", "2024-12"), ("2025", "2025-01", "2025-12"),
             ("2026", "2026-01", "2026-10")]
    for ax in (ax1, ax2):
        for _, m0, _ in years[1:]:
            ax.axvline(midx[m0] - 0.5, color="k", lw=1.2, ls="--", alpha=0.5)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    trans = ax1.get_xaxis_transform()
    for y, m0, m1 in years:
        ax1.text((midx[m0] + midx[m1]) / 2, 1.02, y, ha="center", va="bottom",
                 fontsize=15, fontweight="bold", transform=trans)
    ax1.set_title("8 粗类 · 逐月发帖量", fontsize=15, pad=26)
    ax2.set_title("8 粗类 · 月度构成占比", fontsize=15, pad=8)
    handles, labels = ax1.get_legend_handles_labels()
    ax1.legend(handles, labels, ncol=1, fontsize=11, loc="upper left",
               bbox_to_anchor=(1.005, 1.0), frameon=False, handlelength=1.3, labelspacing=0.7)
    path = os.path.join(ANALYSIS, "coarse_trend_dual.png")
    fig.savefig(path, dpi=150)
    print(path)


if __name__ == "__main__":
    main()
