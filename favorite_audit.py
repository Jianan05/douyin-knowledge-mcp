"""低成本审计抖音收藏元数据与本地库，不打印或保存转写正文。"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


_ID_RE = re.compile(r"(?:douyin\.com/(?:video|note)/|douyin[_-](?:图文[_-]|文字[_-])?)(\d{10,})")
_NON_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_PROMPT_MARKERS = (
    "以下是普通话的句子请用简体中文转写",
    "内容可能涉及这些术语",
)


def _flat(value: object, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _norm_title(value: object) -> str:
    return _NON_WORD_RE.sub("", str(value or "").lower())


def title_similarity(left: object, right: object) -> float:
    a, b = _norm_title(left), _norm_title(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _kind_from_local(row: dict) -> str:
    platform = str(row.get("platform") or "")
    name = Path(str(row.get("path") or "")).name
    if "图文" in platform or "图文" in name:
        return "image"
    if "文字" in platform or "文字" in name:
        return "text"
    return "video" if float(row.get("duration") or 0) > 0 else "unknown"


def _id_in_text(value: object) -> str:
    match = _ID_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _transcript_body(path: Path) -> str:
    """只在进程内取正文做模式检测；调用方不得把返回值写入报告或 stdout。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "\n## 转写稿\n"
    pos = text.find(marker)
    return text[pos + len(marker):] if pos >= 0 else ""


def has_prompt_echo(path: Path) -> bool:
    body = _transcript_body(path)
    if not body:
        return False
    normalized = _NON_WORD_RE.sub("", body.lower())
    # 不能按 Agent/RAG/token 等术语频次判：技术类真内容会天然高频。
    # 只认 initial_prompt 的固定句式，宁可漏掉片段化回声，也不自动隔离真稿。
    return any(marker in normalized for marker in _PROMPT_MARKERS)


@dataclass
class Finding:
    aweme_id: str
    level: str
    reasons: list[str] = field(default_factory=list)
    remote_title: str = ""
    local_title: str = ""
    remote_kind: str = "unknown"
    local_kind: str = "unknown"
    remote_duration: float = 0.0
    local_duration: float = 0.0
    url: str = ""
    path: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def load_latest_index(index_path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not index_path.exists():
        return latest
    with index_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(row.get("video_id") or "")
            if key:
                latest[key] = row
    return latest


def audit_rows(remote_items: list[dict], local: dict[str, dict], liked_items: list[dict] | None = None) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    remote_by_id = {str(item.get("aweme_id") or ""): item for item in remote_items}

    for aweme_id, item in remote_by_id.items():
        row = local.get(aweme_id)
        if not row:
            continue
        remote_title = _flat(item.get("desc"))
        local_title = _flat(row.get("title"))
        remote_kind = str(item.get("kind") or "unknown")
        local_kind = _kind_from_local(row)
        remote_duration = float(item.get("duration_ms") or 0) / 1000
        local_duration = float(row.get("duration") or 0)
        reasons: list[str] = []
        confirmed = False

        source_id = _id_in_text(row.get("url"))
        path_id = _id_in_text(row.get("path"))
        if source_id and source_id != aweme_id:
            reasons.append(f"来源链接作品 ID 为 {source_id}")
            confirmed = True
        if path_id and path_id != aweme_id:
            reasons.append(f"文件名作品 ID 为 {path_id}")
            confirmed = True

        if remote_kind != "unknown" and local_kind != "unknown" and remote_kind != local_kind:
            reasons.append(f"类型冲突：接口 {remote_kind} / 本地 {local_kind}")
            confirmed = True

        duration_ratio = 1.0
        if remote_duration > 0 and local_duration > 0:
            duration_ratio = max(remote_duration, local_duration) / min(remote_duration, local_duration)
            delta = abs(remote_duration - local_duration)
            if delta >= 30 and duration_ratio >= 2.0:
                reasons.append(f"时长严重冲突：接口 {remote_duration:.1f}s / 本地 {local_duration:.1f}s")
                confirmed = True
            elif delta >= max(8, remote_duration * 0.25):
                reasons.append(f"时长明显偏差：接口 {remote_duration:.1f}s / 本地 {local_duration:.1f}s")

        similarity = title_similarity(remote_title, local_title)
        if not remote_title and local_title:
            reasons.append("接口标题为空，但本地保存了具体标题")
        elif remote_title and local_title and min(len(_norm_title(remote_title)), len(_norm_title(local_title))) >= 5 and similarity < 0.18:
            reasons.append(f"标题严重不相似（相似度 {similarity:.2f}）")
            # 两个独立元数据冲突同时出现时才自动确认，单一标题差异留给人工。
            if duration_ratio >= 1.5 or remote_kind != local_kind:
                confirmed = True

        local_path = Path(str(row.get("path") or ""))
        if local_path.is_file() and has_prompt_echo(local_path):
            reasons.append("本地检测到明显提示词回声")
            confirmed = True

        if reasons:
            findings.append(Finding(
                aweme_id=aweme_id,
                level="confirmed" if confirmed else "review",
                reasons=reasons,
                remote_title=remote_title,
                local_title=local_title,
                remote_kind=remote_kind,
                local_kind=local_kind,
                remote_duration=remote_duration,
                local_duration=local_duration,
                url=str(item.get("url") or f"https://www.douyin.com/video/{aweme_id}"),
                path=str(row.get("path") or ""),
            ))

    # 与当前收藏是否匹配不影响本地完整性检查：提示词回声、URL/文件 ID
    # 冲突属于文件自身证据，应覆盖手动入库和历史作品在内的整个本地库。
    for aweme_id, row in local.items():
        if aweme_id in remote_by_id:
            continue
        reasons: list[str] = []
        source_id = _id_in_text(row.get("url"))
        path_id = _id_in_text(row.get("path"))
        if source_id and source_id != aweme_id:
            reasons.append(f"来源链接作品 ID 为 {source_id}")
        if path_id and path_id != aweme_id:
            reasons.append(f"文件名作品 ID 为 {path_id}")
        local_path = Path(str(row.get("path") or ""))
        if local_path.is_file() and has_prompt_echo(local_path):
            reasons.append("本地检测到明显提示词回声")
        if reasons:
            findings.append(Finding(
                aweme_id=aweme_id,
                level="confirmed",
                reasons=reasons,
                local_title=_flat(row.get("title")),
                local_kind=_kind_from_local(row),
                local_duration=float(row.get("duration") or 0),
                url=str(row.get("url") or f"https://www.douyin.com/video/{aweme_id}"),
                path=str(row.get("path") or ""),
            ))

    # 点赞接口命中但当前收藏接口没有命中，只是污染证据候选；历史手动入库也可能满足，绝不自动隔离。
    for item in liked_items or []:
        aweme_id = str(item.get("aweme_id") or "")
        row = local.get(aweme_id)
        if not row or aweme_id in remote_by_id:
            continue
        findings.append(Finding(
            aweme_id=aweme_id,
            level="review",
            reasons=["当前点赞流命中、收藏流未命中；可能是旧过滤器污染，也可能是历史手动入库"],
            remote_title=_flat(item.get("desc")),
            local_title=_flat(row.get("title")),
            remote_kind=str(item.get("kind") or "unknown"),
            local_kind=_kind_from_local(row),
            remote_duration=float(item.get("duration_ms") or 0) / 1000,
            local_duration=float(row.get("duration") or 0),
            url=str(item.get("url") or ""),
            path=str(row.get("path") or ""),
        ))

    stats = {
        "remote": len(remote_by_id),
        "local_unique": len(local),
        "matched": len(set(remote_by_id) & set(local)),
        "remote_not_local": len(set(remote_by_id) - set(local)),
        "local_not_remote": len(set(local) - set(remote_by_id)),
        "confirmed": sum(f.level == "confirmed" for f in findings),
        "review": sum(f.level == "review" for f in findings),
    }
    return findings, stats


def write_reports(
    report_dir: Path,
    findings: list[Finding],
    stats: dict,
    inventory: dict,
    remote_items: list[dict] | None = None,
    liked_items: list[dict] | None = None,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"{stamp}-全库收藏审计.json"
    md_path = report_dir / f"{stamp}-全库收藏审计.md"
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inventory": inventory,
        "stats": stats,
        "findings": [f.as_dict() for f in findings],
        # 保存轻量接口快照，便于离线复算规则；不保存 _video、签名媒体地址或正文。
        "remote_items": [
            {
                "aweme_id": str(item.get("aweme_id") or ""),
                "desc": _flat(item.get("desc"), 500),
                "kind": str(item.get("kind") or "unknown"),
                "duration_ms": int(item.get("duration_ms") or 0),
                "url": str(item.get("url") or ""),
            }
            for item in (remote_items or [])
        ],
        "liked_items": [
            {
                "aweme_id": str(item.get("aweme_id") or ""),
                "desc": _flat(item.get("desc"), 500),
                "kind": str(item.get("kind") or "unknown"),
                "duration_ms": int(item.get("duration_ms") or 0),
                "url": str(item.get("url") or ""),
            }
            for item in (liked_items or [])
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 抖音收藏全库自动审计",
        "",
        "> 只包含元数据和异常原因；程序未把任何转写正文写入本报告。",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 当前收藏接口：{stats['remote']} 条（完整：{'是' if inventory.get('complete') else '否'}）",
        f"- 本地唯一作品：{stats['local_unique']} 条；与当前收藏匹配：{stats['matched']} 条",
        f"- 明确错误：{stats['confirmed']} 条；需人工复核：{stats['review']} 条",
        "- 安全策略：仅当前不在收藏列表不算错误；点赞流单独命中也不自动隔离。",
        "",
    ]
    for level, heading in (("confirmed", "明确错误"), ("review", "人工复核")):
        selected = [f for f in findings if f.level == level]
        lines.extend([f"## {heading}（{len(selected)} 条）", ""])
        if not selected:
            lines.append("- 无。")
        for f in selected:
            title = (f.remote_title or f.local_title or f"作品 {f.aweme_id}").replace("[", "［").replace("]", "］")
            reasons = "；".join(f.reasons)
            lines.append(f"- [{title}]({f.url}) (`{f.aweme_id}`) — {reasons} — [本地文件](<{f.path}>)")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def quarantine_confirmed(root: Path, findings: list[Finding], index_path: Path) -> tuple[Path | None, int]:
    confirmed = [f for f in findings if f.level == "confirmed" and Path(f.path).is_file()]
    if not confirmed:
        return None, 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = root / "_审计隔离" / stamp
    target_dir.mkdir(parents=True, exist_ok=False)
    if index_path.exists():
        shutil.copy2(index_path, target_dir / "index.before.jsonl")
    manifest = []
    for finding in confirmed:
        source = Path(finding.path)
        target = target_dir / source.name
        suffix = 2
        while target.exists():
            target = target_dir / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        source.replace(target)
        manifest.append({
            "aweme_id": finding.aweme_id,
            "original_path": str(source),
            "quarantined_path": str(target),
            "reasons": finding.reasons,
        })
    (target_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target_dir, len(manifest)
