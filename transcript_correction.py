"""Versioned transcript corrections that never overwrite source evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
EVENTS_NAME = "_correction_events.jsonl"
CORRECTIONS_DIR = "_corrections"
APPROVABLE_STATES = {"reference", "deep_dive"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _append(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def load_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    events: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("video_id"):
                events.append(event)
    return events


def _catalog(root: Path) -> dict[str, dict]:
    import review_book

    return {
        str(row.get("video_id") or ""): row
        for row in review_book.load_index(root / "index.jsonl")
        if row.get("video_id")
    }


def correction_versions(root: Path, video_id: str) -> list[dict]:
    return [
        event for event in load_events(root / EVENTS_NAME)
        if str(event.get("video_id") or "") == video_id
        and event.get("event") == "correction_created"
    ]


def create_correction(
    root: Path,
    video_id: str,
    corrected_text: str,
    *,
    basis: str,
    corrected_by: str,
) -> dict:
    """Save an immutable candidate beside the source; do not activate it."""
    import review_book

    states = review_book.load_states(root / review_book.STATE_NAME)
    state = states.get(video_id) or {}
    if state.get("review_status") != "needs_correction":
        raise ValueError("只有“转录需修正”状态的作品才能创建修正版")
    row = _catalog(root).get(video_id) or state
    original = Path(str(row.get("path") or state.get("path") or "")).expanduser()
    if not original.is_absolute():
        original = root / original
    if not original.is_file() or not _inside(root, original):
        raise ValueError("原始转录路径不存在或不在当前知识库内")
    text = corrected_text.strip()
    if not text:
        raise ValueError("修正版正文不能为空")
    if not basis.strip():
        raise ValueError("必须记录校正依据")
    version = len(correction_versions(root, video_id)) + 1
    target = root / CORRECTIONS_DIR / video_id / f"v{version:04d}.md"
    if target.exists():
        raise ValueError("目标修正版本已存在，拒绝覆盖")
    title = str(row.get("title") or state.get("title") or video_id)
    source = str(row.get("url") or state.get("url") or "")
    platform = str(row.get("platform") or "douyin")
    original_hash = _sha256(original)
    front = {
        "title": title,
        "source": source,
        "video_id": video_id,
        "platform": platform,
        "category": str(row.get("category") or ""),
        "tags": list(row.get("tags") or state.get("tags") or []),
        "correction_version": version,
        "original_path": str(original.resolve()),
        "original_sha256": original_hash,
        "corrected_by": corrected_by.strip(),
        "correction_basis": basis.strip(),
        "created_at": _now(),
    }
    lines = ["---"]
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items())
    lines.extend([
        "---", "", "## 转写稿", "", text, "", "## 校正说明", "",
        "- 原始转录未修改；本文件是独立候选版本。",
        f"- 校正依据：{basis.strip()}",
        "- 未经显式批准，本版本不会进入下游索引。", "",
    ])
    _atomic_write(target, "\n".join(lines))
    event = {
        "schema_version": 1,
        "event": "correction_created",
        "video_id": video_id,
        "version": version,
        "correction_path": str(target.resolve()),
        "corrected_sha256": _sha256(target),
        "original_path": str(original.resolve()),
        "original_sha256": original_hash,
        "basis": basis.strip(),
        "corrected_by": corrected_by.strip(),
        "created_at": front["created_at"],
    }
    _append(root / EVENTS_NAME, event)
    return event


def _created_event(root: Path, video_id: str, version: int) -> dict:
    for event in reversed(load_events(root / EVENTS_NAME)):
        if (
            event.get("event") == "correction_created"
            and str(event.get("video_id") or "") == video_id
            and int(event.get("version") or 0) == version
        ):
            return event
    raise ValueError("找不到指定修正版本")


def _validate_created(root: Path, event: dict) -> tuple[Path, Path]:
    original = Path(str(event.get("original_path") or ""))
    correction = Path(str(event.get("correction_path") or ""))
    if not (_inside(root, original) and _inside(root, correction)):
        raise ValueError("修正记录包含知识库外路径")
    if not original.is_file() or _sha256(original) != event.get("original_sha256"):
        raise ValueError("原始转录已变化，必须基于新原稿创建下一版修正")
    if not correction.is_file() or _sha256(correction) != event.get("corrected_sha256"):
        raise ValueError("修正版缺失或哈希不一致，拒绝批准")
    return original, correction


def approve_correction(
    root: Path,
    video_id: str,
    version: int,
    *,
    review_status: str,
    approved_by: str,
    note: str = "",
) -> dict:
    """Activate a verified correction and queue it for normal impact review."""
    import review_book

    if review_status not in APPROVABLE_STATES:
        raise ValueError("批准后的状态只能是 reference 或 deep_dive")
    previous_state = review_book.load_states(root / review_book.STATE_NAME).get(video_id) or {}
    previous_status = str(previous_state.get("review_status") or "")
    created = _created_event(root, video_id, version)
    _validate_created(root, created)
    event = {
        "schema_version": 1,
        "event": "correction_approved",
        "video_id": video_id,
        "version": version,
        "correction_path": created["correction_path"],
        "corrected_sha256": created["corrected_sha256"],
        "original_path": created["original_path"],
        "original_sha256": created["original_sha256"],
        "review_status": review_status,
        "note": note.strip(),
        "approved_by": approved_by.strip(),
        "approved_at": _now(),
    }
    _append(root / EVENTS_NAME, event)
    review_book.set_status(
        root,
        video_id,
        review_status,
        note or f"修正版 v{version:04d} 已批准；原始转录保留",
        source=f"修正版人工批准：{approved_by.strip()}",
    )
    if previous_status in review_book.IMPACT_REVIEW_STATES:
        review_book.append_event(root / review_book.IMPACT_QUEUE_NAME, {
            "schema_version": 1,
            "event": "queued",
            "video_id": video_id,
            "review_status": review_status,
            "queued_at": event["approved_at"],
            "reason": f"已批准新的转录修正版 v{version:04d}，正文发生变化",
        })
    return event


def revoke_correction(root: Path, video_id: str, *, note: str, revoked_by: str) -> dict:
    """Deactivate the current correction without deleting any version."""
    import review_book

    active = load_active_corrections(root).get(video_id)
    if not active:
        raise ValueError("该作品当前没有生效的修正版")
    event = {
        "schema_version": 1,
        "event": "correction_revoked",
        "video_id": video_id,
        "version": active["version"],
        "note": note.strip(),
        "revoked_by": revoked_by.strip(),
        "revoked_at": _now(),
    }
    _append(root / EVENTS_NAME, event)
    review_book.set_status(
        root,
        video_id,
        "needs_correction",
        note or f"修正版 v{int(active['version']):04d} 已撤销",
        source=f"修正版撤销：{revoked_by.strip()}",
    )
    return event


def load_active_corrections(root: Path) -> dict[str, dict]:
    """Return only active corrections whose source and correction hashes still match."""
    created: dict[tuple[str, int], dict] = {}
    active: dict[str, dict] = {}
    for event in load_events(root / EVENTS_NAME):
        video_id = str(event.get("video_id") or "")
        version = int(event.get("version") or 0)
        key = (video_id, version)
        if event.get("event") == "correction_created":
            created[key] = event
        elif event.get("event") == "correction_approved" and key in created:
            active[video_id] = {**created[key], **event}
        elif event.get("event") == "correction_revoked":
            current = active.get(video_id)
            if current and int(current.get("version") or 0) == version:
                active.pop(video_id, None)
    valid: dict[str, dict] = {}
    for video_id, event in active.items():
        try:
            _validate_created(root, event)
        except ValueError:
            continue
        valid[video_id] = event
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description="创建、批准和撤销独立转录修正版")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="创建候选修正版；不会进入索引")
    create.add_argument("video_id")
    create.add_argument("--text-file", type=Path, required=True)
    create.add_argument("--basis", required=True)
    create.add_argument("--corrected-by", required=True)
    approve = sub.add_parser("approve", help="校验并激活一个修正版本")
    approve.add_argument("video_id")
    approve.add_argument("version", type=int)
    approve.add_argument("--status", choices=sorted(APPROVABLE_STATES), required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--note", default="")
    revoke = sub.add_parser("revoke", help="撤销当前修正版；不删除历史")
    revoke.add_argument("video_id")
    revoke.add_argument("--note", required=True)
    revoke.add_argument("--revoked-by", required=True)
    sub.add_parser("status", help="列出当前生效修正版")
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()
    if args.command == "create":
        event = create_correction(
            root, args.video_id, args.text_file.read_text(encoding="utf-8"),
            basis=args.basis, corrected_by=args.corrected_by,
        )
        print(f"已创建候选修正版 v{event['version']:04d}：{event['correction_path']}")
    elif args.command == "approve":
        event = approve_correction(
            root, args.video_id, args.version, review_status=args.status,
            approved_by=args.approved_by, note=args.note,
        )
        print(f"已批准修正版 v{event['version']:04d}：{args.video_id}")
    elif args.command == "revoke":
        event = revoke_correction(root, args.video_id, note=args.note, revoked_by=args.revoked_by)
        print(f"已撤销修正版 v{event['version']:04d}：{args.video_id}")
    else:
        print(json.dumps(load_active_corrections(root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
