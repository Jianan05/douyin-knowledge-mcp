"""生成话题目录：库里到底在讨论哪些事，每个话题多少条、多少字、该怎么读。

流程：切 chunk → embedding → 聚类 → 按「同一话题下有多少字」给出阅读建议。
话题名先用簇内高频词凑，够用；要更好的名字再交给 LLM，但那是按簇跑，不是按条跑。
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chunks
import cluster

ROOT = pathlib.Path.home() / "Desktop" / "DouyinNotes"
OUT_MD = ROOT / "话题目录.md"
CACHE = pathlib.Path(__file__).resolve().parent / "data" / "chunk_vectors.npz"

STOP = set("""的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有
看 好 自己 这 那 他 她 它 我们 你们 他们 什么 这个 那个 就是 可以 因为 所以 但是 如果
其实 这样 那样 现在 时候 一个 一样 这些 那些 已经 还是 或者 然后 不是 大家 东西 问题
真的 一下 一定 只是 而且 比如 这种 那种 之后 之前 里面 出来 起来 下来 过来 知道 觉得
对吧 第一 第二 第三 第四 备忘录 就会 可能 需要 应该 怎么 为什么 这样子 一些 很多 非常
其实 直接 特别 完全 基本 主要 通过 进行 包括 以及 对于 关于 这里 那里 今天 大概 这条
视频 内容 大家 我会 你会 它会 我们的 你们的 一起 然后呢 所以说 也就是说
""".split())
_EN = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{2,}")


def _words(text: str) -> list[str]:
    """\u4e2d\u6587\u5fc5\u987b\u5206\u8bcd\u624d\u80fd\u62ff\u6765\u547d\u540d\u3002

    \u26a0\ufe0f \u522b\u7528 [\u4e00-\u9fff]{2,6} \u8fd9\u79cd\u6b63\u5219\u5192\u5145"\u8bcd"\uff1a\u4e2d\u6587\u6ca1\u6709\u7a7a\u683c\uff0c\u5b83\u6293\u5230\u7684\u662f
    \u4efb\u610f\u8fde\u7eed\u6c49\u5b57\u4e32\u3002\u6309\u8bcd\u9891\u6392\u8fd8\u770b\u4e0d\u51fa\u95ee\u9898\uff08\u5e38\u89c1\u4e32\u78b0\u5de7\u5c31\u662f\u771f\u8bcd\uff09\uff0c\u4e00\u65e6\u6309\u7a00\u6709\u5ea6
    \u6392\u5e8f\uff0c\u51fa\u6765\u7684\u5168\u662f\u300c\u90a3\u4e0d\u77e5\u9053\u6211\u8bb2\u300d\u300c\u4ee5\u67e5\u770b\u6a71\u7a97\u54e6\u300d\u8fd9\u79cd\u968f\u673a\u7247\u6bb5\u3002
    """
    import jieba

    out = [w for w in jieba.cut(text) if len(w) >= 2 and not w.isspace()]
    out.extend(_EN.findall(text))
    return out


def _doc_freq(all_texts: list[list[str]]) -> collections.Counter:
    """每个词出现在多少个簇里 —— 用来惩罚"哪个簇都有"的词。"""
    df = collections.Counter()
    for texts in all_texts:
        words = set()
        for t in texts:
            words.update(w for w in _words(t) if w.lower() not in STOP and w not in STOP)
        df.update(words)
    return df


def name_cluster(texts: list[str], df: collections.Counter, n_clusters: int, top: int = 4) -> str:
    """簇名 = **在这个簇里频繁、但全库罕见**的词。

    ⚠️ 直接用簇内高频词不行：Agent、RAG、大模型 在每个簇里都高频，
    结果 20 个簇有 15 个叫「Agent / RAG / …」，等于没命名。
    """
    import math

    # cf = 这个词在簇内多少个段落里出现过（不是出现几次）
    cf = collections.Counter()
    for t in texts:
        cf.update(w for w in set(_words(t)) if w.lower() not in STOP and w not in STOP)

    scored = []
    for word, n in cf.items():
        ratio = n / len(texts)
        # ⚠️ 只按稀有度排会把**转写错字**顶上来（「检素」「照回」「途毙」这类
        # 听错的词天生稀有）。所以先要求它在簇内反复出现，再谈稀有度。
        if n < 3 and ratio < 0.34:
            continue
        if df[word] > n_clusters * 0.25:   # 哪个簇都有的词没有区分度
            continue
        scored.append((ratio * math.log(n_clusters / max(df[word], 1) + 1), word))
    scored.sort(reverse=True)
    picked = [w for _, w in scored[:top]]
    return " / ".join(picked) or " / ".join(w for w, _ in cf.most_common(top)) or "（未命名）"


def build(threshold: float = 0.35, rebuild: bool = False):
    import numpy as np

    rows = chunks.chunk_library(ROOT)
    print(f"chunk 总数 {len(rows)}", flush=True)

    ids = [r["chunk_id"] for r in rows]
    vecs = None
    if CACHE.exists() and not rebuild:
        data = np.load(CACHE, allow_pickle=True)
        if list(data["ids"]) == ids:
            vecs = data["vecs"]
            print("复用缓存的向量", flush=True)
    if vecs is None:
        print("开始 embedding…", flush=True)
        vecs = cluster.embed([r["text"] for r in rows], batch_size=16)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez(CACHE, ids=np.array(ids, dtype=object), vecs=vecs)
        print("向量已缓存", flush=True)

    labels = cluster.cluster(vecs, threshold=threshold)
    print(f"聚出 {len(set(labels))} 个簇", flush=True)

    groups = collections.defaultdict(list)
    for row, lab in zip(rows, labels):
        groups[int(lab)].append(row)

    # 一个话题的"体量"按去重后的视频数和总字数算，不是 chunk 数
    all_texts = [[it["text"] for it in items] for items in groups.values()]
    df = _doc_freq(all_texts)

    summary = []
    for lab, items in groups.items():
        videos = {}
        for it in items:
            videos.setdefault(it["video_id"], it["title"])
        total = sum(len(it["text"]) for it in items)
        summary.append({
            "label": lab,
            "name": name_cluster([it["text"] for it in items], df, len(groups)),
            "chunks": len(items),
            "videos": len(videos),
            "chars": total,
            "titles": list(videos.values()),
        })
    summary.sort(key=lambda s: (-s["videos"], -s["chars"]))

    out = ["# 话题目录", "",
           f"由 {len(rows)} 个 chunk 聚成 {len(groups)} 个话题（阈值 {threshold}）。",
           "建议列的判据：总字数 < 1.2 万直接全看，< 6 万先摘要，再多必须先摘要。", ""]
    for s in summary:
        if s["videos"] < 2:
            continue   # 只有一条的不算话题
        hint = "全看" if s["chars"] < 12000 else ("先摘要" if s["chars"] < 60000 else "必须先摘要")
        out.append(f"## {s['name']}")
        out.append(f"{s['videos']} 条视频 / {s['chunks']} 段 / {s['chars']} 字 → **{hint}**")
        out.append("")
        for t in s["titles"][:12]:
            out.append(f"- {t}")
        if len(s["titles"]) > 12:
            out.append(f"- …还有 {len(s['titles']) - 12} 条")
        out.append("")
    OUT_MD.write_text("\n".join(out), encoding="utf-8")
    singles = sum(1 for s in summary if s["videos"] < 2)
    print(f"写入 {OUT_MD}", flush=True)
    print(f"多视频话题 {len(summary) - singles} 个，单条独立 {singles} 个", flush=True)


if __name__ == "__main__":
    build(rebuild="--rebuild" in sys.argv)
