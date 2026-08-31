"""chunk 级 embedding → 聚类（发现话题）+ top-k（相关阅读）。

⚠️ 这个模块**故意不 import ingest** —— ingest 会把 HF_HOME 指到 runtime/huggingface，
而 bge-m3 在用户默认的 ~/.cache/huggingface 里（4.3G，是 RAG 项目那边下的）。
指过去就找不到模型，会重新下一份。
"""

from __future__ import annotations

import json
import pathlib

MODEL_NAME = "BAAI/bge-m3"
CACHE = pathlib.Path(__file__).resolve().parent / "data" / "embeddings.npz"


def _model(device: str = ""):
    import torch
    from sentence_transformers import SentenceTransformer

    # CPU 上 bge-m3 实测 4.95 秒一个 chunk，全库要 3.5 小时；GPU 是几分钟。
    dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
    return SentenceTransformer(MODEL_NAME, device=dev)


def embed(texts: list[str], batch_size: int = 8, show: bool = True, device: str = ""):
    import numpy as np

    model = _model(device)
    vecs = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,   # 归一化之后，点积就是余弦相似度
        show_progress_bar=show,
    )
    return np.asarray(vecs, dtype="float32")


def cluster(vecs, threshold: float = 0.45):
    """凝聚层次聚类。不指定簇数 —— 话题有多少个事先不知道，
    用「距离超过阈值就不再合并」来自然停住。threshold 越小簇越碎。"""
    from sklearn.cluster import AgglomerativeClustering

    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=threshold,
        metric="cosine",
        linkage="average",
    )
    return model.fit_predict(vecs)


def top_k(vecs, k: int = 5):
    """每个 chunk 最像的 k 个（排除自己）。向量已归一化，点积即余弦。"""
    import numpy as np

    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    idx = np.argpartition(-sim, kth=k, axis=1)[:, :k]
    out = []
    for row, cols in enumerate(idx):
        cols = cols[np.argsort(-sim[row, cols])]
        out.append([(int(c), float(sim[row, c])) for c in cols])
    return out
