"""Apply an explicitly confirmed impact decision to an existing curated note.

Old conclusions are preserved in an in-note history block.  This command only
accepts append-only impact-review decisions and refuses stale note snapshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import impact_review
import knowledge_notes
import review_book


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside_notes(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to((root / "notes").resolve())
        return True
    except (OSError, ValueError):
        return False


def _latest_decision(root: Path, candidate_id: str) -> dict:
    decision = impact_review.load_decisions(root / impact_review.EVENTS_NAME).get(candidate_id)
    if not decision:
        raise ValueError("没有该候选的人工影响判断")
    return decision


def _already_applied(root: Path, candidate_id: str) -> bool:
    path = root / knowledge_notes.EVENTS_NAME
    if not path.is_file():
        return False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "knowledge_updated" and event.get("candidate_id") == candidate_id:
                return True
    return False


def _section(text: str, title: str) -> str:
    matches = list(_H2.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1).strip() != title:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return text[match.end():end].strip()
    raise ValueError(f"正式笔记缺少 `## {title}`，拒绝自动更新")


def _replace_section(text: str, title: str, body: str) -> str:
    matches = list(_H2.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1).strip() != title:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        suffix = text[end:].lstrip("\n")
        replacement = f"## {title}\n\n{body.strip()}\n\n"
        return text[:match.start()] + replacement + suffix
    raise ValueError(f"正式笔记缺少 `## {title}`，拒绝自动更新")


def _append_history(text: str, entry: str) -> str:
    try:
        current = _section(text, "更新历史")
        return _replace_section(text, "更新历史", current + "\n\n" + entry)
    except ValueError:
        marker = re.search(r"^##\s+来源\s*$", text, re.MULTILINE)
        if not marker:
            raise ValueError("正式笔记缺少 `## 来源`，无法安全插入更新历史")
        return text[:marker.start()] + f"## 更新历史\n\n{entry}\n\n" + text[marker.start():]


def _update_frontmatter(text: str, source_ids: list[str], when: str, confirmed_by: str) -> str:
    frontmatter = re.match(r"\A---\n(.*?)\n---\n?", text, re.DOTALL)
    if not frontmatter:
        raise ValueError("正式笔记 frontmatter 不完整")
    header = frontmatter.group(1)
    rendered_ids = ", ".join(json.dumps(value, ensure_ascii=False) for value in source_ids)
    if not re.search(r"^source_ids:\s*\[.*\]\s*$", header, re.MULTILINE):
        raise ValueError("正式笔记缺少 source_ids，拒绝自动更新")
    header = re.sub(
        r"^source_ids:\s*\[.*\]\s*$",
        f"source_ids: [{rendered_ids}]",
        header,
        count=1,
        flags=re.MULTILINE,
    )
    header = re.sub(r"^last_updated_(?:by|at):.*\n?", "", header, flags=re.MULTILINE).rstrip()
    metadata = (
        f"last_updated_by: {json.dumps(confirmed_by, ensure_ascii=False)}\n"
        f"last_updated_at: {json.dumps(when, ensure_ascii=False)}\n"
    )
    return f"---\n{header}\n{metadata}---\n\n" + text[frontmatter.end():].lstrip("\n")


def _source_line(root: Path, source_id: str) -> str:
    catalog = {
        str(row.get("video_id") or ""): row
        for row in review_book.load_index(root / "index.jsonl")
    }
    row = catalog.get(source_id)
    if not row:
        raise ValueError(f"索引中没有来源 {source_id}")
    state = str((review_book.load_states(root / review_book.STATE_NAME).get(source_id) or {}).get("review_status") or "pending")
    if state not in knowledge_notes.ALLOWED_SOURCE_STATES:
        raise ValueError(f"来源 {source_id} 当前状态为 {state}，不能用于正式知识更新")
    title = str(row.get("title") or source_id).replace("\n", " ")
    return (
        f"- [{title}]({review_book.canonical_url(row)}) — `video_id={source_id}`；"
        f"审阅状态 `{state}`；本地来源 `{row.get('path') or ''}`"
    )


def apply_update(
    root: Path,
    *,
    candidate_id: str,
    update_text: str,
    rationale: str,
    scope: str,
    confirmed_by: str,
    user_confirmed: bool,
) -> Path:
    root = root.expanduser().resolve()
    if not user_confirmed:
        raise ValueError("缺少人工确认；正式知识更新必须由用户明确确认")
    update_text = update_text.strip()
    rationale = rationale.strip()
    confirmed_by = confirmed_by.strip()
    if not update_text or not rationale or not confirmed_by:
        raise ValueError("更新文字、更新理由和确认人都不能为空")
    if _already_applied(root, candidate_id):
        raise ValueError("该影响候选已经应用过，拒绝重复更新")

    decision = _latest_decision(root, candidate_id)
    action = str(decision.get("action") or "")
    if action not in {"supplement", "supersede", "source_only"}:
        if action == "new_topic":
            raise ValueError("该判断要求新建专题，请使用 knowledge_notes.py，而不是修改现有笔记")
        raise ValueError(f"建议动作 {action or '空'} 不需要修改正式知识")
    note_path = Path(str(decision.get("note_path") or "")).expanduser().resolve()
    if not note_path.is_file() or not _inside_notes(root, note_path):
        raise ValueError("目标正式笔记不存在或不在 notes/ 内")
    expected_hash = str(decision.get("note_sha256") or "")
    if not expected_hash:
        raise ValueError("影响判断缺少正式笔记快照哈希，请重新保存判断")
    if _sha256(note_path) != expected_hash:
        raise ValueError("正式笔记自影响判断后已经变化，请重新审核，拒绝覆盖")

    source_id = str(decision.get("source_id") or "")
    source_line = _source_line(root, source_id)
    original = note_path.read_text(encoding="utf-8")
    if f"`{candidate_id}`" in original:
        raise ValueError("该影响候选已经写入正式笔记，拒绝重复更新")
    old_conclusion = _section(original, "已确认结论")
    old_rationale = _section(original, "形成理由")
    old_scope = _section(original, "适用范围")
    existing_ids_match = re.search(r"^source_ids:\s*\[(.*)\]\s*$", original, re.MULTILINE)
    existing_ids = re.findall(r'"([^"]*)"', existing_ids_match.group(1)) if existing_ids_match else []
    source_ids = list(dict.fromkeys([*existing_ids, source_id]))
    when = _now()
    history_label = when.replace("T", " ")

    updated = original
    if action == "supplement":
        new_conclusion = old_conclusion + f"\n\n补充（{history_label}）：{update_text}（用户文字确认）"
        new_rationale = old_rationale + f"\n\n更新理由（{history_label}）：{rationale}（用户文字确认）"
        new_scope = old_scope + (f"\n\n范围补充（{history_label}）：{scope}（用户文字确认）" if scope.strip() else "")
        history = (
            f"### {history_label} — 补充\n\n"
            f"- 新增内容：{update_text}（用户文字确认）\n"
            f"- 更新理由：{rationale}（用户文字确认）\n"
            f"- 影响候选：`{candidate_id}`；新增来源：`{source_id}`"
        )
    elif action == "supersede":
        new_conclusion = update_text + "（用户文字确认）"
        new_rationale = rationale + "（用户文字确认）"
        new_scope = scope.strip() + "（用户文字确认）" if scope.strip() else old_scope
        history = (
            f"### {history_label} — 作废替代\n\n"
            f"- ❌ 作废的旧结论：{old_conclusion}\n"
            f"- 作废原因：{rationale}（用户文字确认）\n"
            f"- 替代结论：{update_text}（用户文字确认）\n"
            f"- 影响候选：`{candidate_id}`；新增来源：`{source_id}`"
        )
    else:
        new_conclusion, new_rationale, new_scope = old_conclusion, old_rationale, old_scope
        history = (
            f"### {history_label} — 仅登记来源\n\n"
            f"- 登记说明：{update_text}（用户文字确认）\n"
            f"- 理由：{rationale}（用户文字确认）\n"
            f"- 影响候选：`{candidate_id}`；新增来源：`{source_id}`"
        )

    updated = _replace_section(updated, "已确认结论", new_conclusion)
    updated = _replace_section(updated, "形成理由", new_rationale)
    updated = _replace_section(updated, "适用范围", new_scope)
    sources_body = _section(updated, "来源")
    if f"video_id={source_id}" not in sources_body:
        updated = _replace_section(updated, "来源", sources_body + "\n" + source_line)
    updated = _append_history(updated, history)
    updated = _update_frontmatter(updated, source_ids, when, confirmed_by)

    temporary = note_path.with_suffix(note_path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temporary, note_path)
    new_hash = _sha256(note_path)
    review_book.append_event(root / knowledge_notes.EVENTS_NAME, {
        "schema_version": 1,
        "event": "knowledge_updated",
        "candidate_id": candidate_id,
        "action": action,
        "path": str(note_path),
        "source_ids": source_ids,
        "confirmed_by": confirmed_by,
        "confirmed_at": when,
        "previous_sha256": expected_hash,
        "new_sha256": new_hash,
    })
    review_book.append_event(root / impact_review.EVENTS_NAME, {
        "schema_version": 1,
        "event": "knowledge_update_applied",
        "candidate_id": candidate_id,
        "path": str(note_path),
        "applied_at": when,
        "new_sha256": new_hash,
    })
    return note_path


def main() -> int:
    parser = argparse.ArgumentParser(description="把已确认的影响判断安全应用到正式知识")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--text", required=True, help="确认后的补充或替代文字")
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--scope", default="")
    parser.add_argument("--confirmed-by", required=True)
    parser.add_argument("--user-confirmed", action="store_true")
    args = parser.parse_args()
    path = apply_update(
        args.dir,
        candidate_id=args.candidate_id,
        update_text=args.text,
        rationale=args.rationale,
        scope=args.scope,
        confirmed_by=args.confirmed_by,
        user_confirmed=args.user_confirmed,
    )
    print(f"已安全更新正式知识：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
