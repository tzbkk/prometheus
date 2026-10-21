"""treemap 布局与标注工具（coarse_chart.py / causal_topics.py 共用）。"""

import math
from collections import Counter

import matplotlib.pyplot as plt

TOP_K = 10
GRAY = "#c9c9c9"
TAB = plt.get_cmap("tab20").colors
PT = 72.0

STOP = set("""分享 图片 视频 表情 动画 动态 相册 文件 好看 可以 谢谢 厉害 太强 牛逼 真棒 真好 不错
哈哈 哈哈哈 感觉 有点 一下 真的 时候 什么 怎么 这个 那个 一个 没有 不是 还是 就是 因为 所以 但是 已经
现在 知道 觉得 可能 应该 大家 自己 我们 你们 他们 频道 帖子 评论 成员 楼主 大佬 一起 一直 非常
什么样 为什么 是不是 有没有 这么 那么 一点 东西 这种 那些 这些 之后 之前 以后 以前 刚才 然后 而且
虽然 如果 或者 还有 也是 都是 只是 不过 其实 反正 确实 直接 比较 更好 最好 好多 很多 多少 几个
第一 第二 第三 今天 昨天 明天 最近 原来 果然 居然 竟然 突然 马上 一定 肯定 需要 不会 不能 不用
不要 不行 嘿嘿 呜呜 啊啊 哇哇 捏 喵 哇 诶 嘞 啦 咯 嘛 呗 哦 噢 喔 咦 嗯 恩 呀 吧 呢 吗 啊 这 那""".split())


def tokenize(text):
    import jieba
    return [w for w in jieba.lcut(text)
            if len(w) >= 2 and any("\u4e00" <= c <= "\u9fff" for c in w) and w not in STOP]


def top_keywords(texts, global_df, n_docs, k=TOP_K):
    """判别式打分：簇内词频占比 / 全库词频占比。"""
    n = max(len(texts), 1)
    df = Counter()
    for t in texts:
        df.update(set(tokenize(t)))
    for thr in (min(10, max(2, math.ceil(0.02 * n))), 2, 1):
        scored = [(w, (c / n) / ((global_df[w] + 1) / (n_docs + 1)))
                  for w, c in df.items() if c >= thr]
        if scored:
            scored.sort(key=lambda x: -x[1])
            return [w for w, _ in scored[:k]]
    return []


def text_color(rgb):
    lum = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255
    return "#ffffff" if lum < 0.55 else "#1a1a1a"


def squarify(items, x, y, w, h, rects):
    """items: [(area, word|None)] desc by area, sum(area)==w*h. Bruls et al. squarified."""
    if not items or w <= 1e-9 or h <= 1e-9:
        return
    side = min(w, h)

    def worst(row):
        d = sum(a for a, _ in row) / side
        return max(max(d / (a / d), (a / d) / d) for a, _ in row)

    row = [items[0]]
    rest = items[1:]
    while rest:
        if worst(row + [rest[0]]) <= worst(row):
            row.append(rest.pop(0))
        else:
            break
    d = sum(a for a, _ in row) / side
    if w >= h:
        cy = y
        for a, p in row:
            ch = a / d
            rects.append((x, cy, d, ch, p))
            cy += ch
        squarify(rest, x + d, y, w - d, h, rects)
    else:
        cx = x
        for a, p in row:
            cw = a / d
            rects.append((cx, y, cw, d, p))
            cx += cw
        squarify(rest, x, y + d, w, h - d, rects)


def wrap_label(word, w_pt, h_pt):
    """返回 (分行列表, 字号)；只用横排，每行≥2 字，均衡折行，放不下则截断或省略。"""
    label = word if word else "无"
    n = len(label)
    for f in (11, 10, 9, 8, 7, 6.5, 6, 5.5, 5, 4.5, 4):
        cpl = int((w_pt - 4) / (f * 1.04))
        max_lines = int((h_pt - 2) / (f * 1.3))
        if cpl < 2 or max_lines < 1:
            continue
        nlines = -(-n // cpl)
        if nlines <= max_lines:
            per = -(-n // nlines)
            return [label[i:i + per] for i in range(0, n, per)], f
    f = 4
    cpl = int((w_pt - 4) / (f * 1.04))
    max_lines = int((h_pt - 2) / (f * 1.3))
    if cpl < 2 or max_lines < 1:
        return None, 0
    text = label[:cpl * max_lines]
    per = -(-len(text) // -(-len(text) // cpl))
    return [text[i:i + per] for i in range(0, len(text), per)], f
