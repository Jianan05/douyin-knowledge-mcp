"""把语义相关的笔记互相连上 [[链接]]，Obsidian 打开就是一张按内容连的图。

⚠️ 不用"笔记级 top-k"：实测这个库里 top-1 相似度中位数只有 0.738，
按可信阈值 0.78 算，781 条里 621 条一条都连不上 —— 因为相关性是**局部的**，
一条视频里某一段跟另一条的某一段讲同一件事，整条比对反而看不出来。

改用 chunk 级聚类的共簇关系：同一个话题簇里的笔记互相连。
同样的数据下孤立笔记从 621 降到 263。
"""

from __future__ import annotations

import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chunks
import cluster
import topic_index

ROOT = pathlib.Path.home() / "Desktop" / "DouyinNotes"
SECTION = "## 相关笔记"
MAX_LINKS = 8
MAX_CLUSTER = 40      # 比这更大的簇说明太泛，连了等于全连
THRESHOLD = 0.35      # 跟话题目录保持一致，链接和话题才对得上


def build_links(threshold: float = THRESHOLD):
    import numpy as np

    rows = chunks.chunk_library(ROOT)
    data = np.load(topic_index.CACHE, allow_pickle=True)
    if list(data["ids"]) != [r["chunk_id"] for r in rows]:
        raise SystemExit("向量缓存跟当前 chunk 对不上，先跑 topic_index.py")
    vecs = data["vecs"]

    labels = cluster.cluster(vecs, threshold=threshold)
    groups = collections.defaultdict(list)
    for row, lab in zip(rows, labels):
        groups[int(lab)].append(row)

    all_texts = [[it["text"] for it in items] for items in groups.values()]
    df = topic_index._doc_freq(all_texts)

    # video_id → {对方 video_id: [共处的话题名, …]}
    related = collections.defaultdict(lambda: collections.defaultdict(list))
    path_of, title_of = {}, {}
    for row in rows:
        path_of[row["video_id"]] = pathlib.Path(row["path"])
        title_of[row["video_id"]] = row["title"]

    for items in groups.values():
        members = {it["video_id"] for it in items}
        if not (2 <= len(members) <= MAX_CLUSTER):
            continue
        name = topic_index.name_cluster([it["text"] for it in items], df, len(groups))
        for v in members:
            for other in members - {v}:
                related[v][other].append(name)
    return related, path_of, title_of


def render(video_id: str, related: dict, path_of: dict, title_of: dict) -> str:
    """按"共处几个话题"排序 —— 共处越多，关系越硬。"""
    # ⚠️ 用 .get 不用 []：related 是 defaultdict，直接下标会给没链接的笔记
    # 凭空建一个空条目，统计"孤立多少条"时会全变成 0
    peers = sorted(related.get(video_id, {}).items(), key=lambda kv: -len(kv[1]))[:MAX_LINKS]
    if not peers:
        return ""
    lines = [SECTION, ""]
    for other, topics in peers:
        stem = path_of[other].stem
        why = topics[0] if len(topics) == 1 else f"{topics[0]} 等 {len(topics)} 个话题"
        lines.append(f"- [[{stem}]] — {why}")
    return "\n".join(lines) + "\n"


def apply(dry_run: bool = True) -> None:
    related, path_of, title_of = build_links()
    changed = 0
    for video_id, path in path_of.items():
        block = render(video_id, related, path_of, title_of)
        text = path.read_text(encoding="utf-8")
        # 幂等：已经有这一节就整块替换，不重复追加
        cleaned = re.split(r"\n## 相关笔记\n", text)[0].rstrip()
        new = cleaned + ("\n\n" + block if block else "\n")
        if new == text:
            continue
        changed += 1
        if not dry_run:
            path.write_text(new, encoding="utf-8")
    total_links = sum(min(len(v), MAX_LINKS) for v in related.values())
    print(f"{'将要更新' if dry_run else '已更新'} {changed} 条笔记，共 {total_links} 条链接")
    print(f"有链接的笔记 {len(related)} / {len(path_of)}，孤立 {len(path_of) - len(related)}")


if __name__ == "__main__":
    apply(dry_run="--apply" not in sys.argv)
