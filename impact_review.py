"""Local review UI for source-to-curated-note impact candidates.

Decisions are append-only.  Draft generation writes discussion artifacts only;
this module never edits confirmed notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import review_book


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
REPORT_DIR = Path("_讨论工作区") / "影响扫描"
DRAFT_DIR = Path("_讨论工作区") / "知识修改草案"
EVENTS_NAME = "_impact_review_events.jsonl"
RELATIONS = {"duplicate", "supports", "refines", "contradicts", "unrelated"}
ACTIONS = {"no_change", "supplement", "supersede", "new_topic", "source_only"}
_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def latest_report_path(root: Path) -> Path:
    directory = root / REPORT_DIR
    candidates: list[Path] = []
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if report.get("report_status") != "superseded" and report.get("results"):
                candidates.append(path)
    if not candidates:
        raise ValueError("没有可审核的影响扫描 JSON；先运行 impact_scan.py")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def load_report(root: Path, report_path: Path | None = None) -> tuple[Path, dict]:
    path = (report_path or latest_report_path(root)).expanduser().resolve()
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError as exc:
        raise ValueError("影响报告必须位于当前知识库内") from exc
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("report_status") == "superseded":
        raise ValueError("该影响报告已作废，请改用后续版本")
    return path, report


def candidate_id(report_path: Path, source_id: str, note_path: str) -> str:
    raw = f"{report_path.name}\0{source_id}\0{note_path}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def load_decisions(path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not path.is_file():
        return latest
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(event.get("candidate_id") or "")
            if key and event.get("event") == "impact_relation_reviewed":
                latest[key] = event
    return latest


def review_items(root: Path, report_path: Path | None = None) -> tuple[Path, dict, list[dict]]:
    path, report = load_report(root, report_path)
    decisions = load_decisions(root / EVENTS_NAME)
    items: list[dict] = []
    for result in report.get("results") or []:
        for candidate in result.get("candidates") or []:
            key = candidate_id(path, str(result.get("source_id") or ""), str(candidate.get("note_path") or ""))
            item = {
                "candidate_id": key,
                "report_path": str(path),
                "source_id": str(result.get("source_id") or ""),
                "source_title": str(result.get("source_title") or ""),
                "source_path": str(result.get("source_path") or ""),
                "routing_status": str(result.get("routing_status") or ""),
                **candidate,
            }
            if candidate.get("already_cited"):
                item["decision"] = {
                    "relation": "already_incorporated",
                    "action": "no_change",
                    "note": "正式知识已通过 source_ids 明确引用该来源。",
                    "locked": True,
                }
            else:
                item["decision"] = decisions.get(key)
            items.append(item)
    return path, report, items


def save_decision(root: Path, payload: dict, report_path: Path | None = None) -> dict:
    path, _, items = review_items(root, report_path)
    key = str(payload.get("candidate_id") or "").strip()
    available = {item["candidate_id"]: item for item in items}
    item = available.get(key)
    if not item:
        raise ValueError("候选不存在或不属于当前影响报告")
    if item.get("already_cited"):
        raise ValueError("该来源已经被正式知识明确引用，无需重复判断")
    relation = str(payload.get("relation") or "").strip()
    action = str(payload.get("action") or "").strip()
    if relation not in RELATIONS:
        raise ValueError("不支持的知识关系")
    if action not in ACTIONS:
        raise ValueError("不支持的建议动作")
    event = {
        "schema_version": 1,
        "event": "impact_relation_reviewed",
        "candidate_id": key,
        "report_path": str(path),
        "source_id": item["source_id"],
        "note_path": item["note_path"],
        "note_sha256": (
            hashlib.sha256(Path(item["note_path"]).read_bytes()).hexdigest()
            if Path(item["note_path"]).is_file() else ""
        ),
        "relation": relation,
        "action": action,
        "note": str(payload.get("note") or "").strip()[:4000],
        "proposed_change": str(payload.get("proposed_change") or "").strip()[:12000],
        "reviewed_at": _now(),
        "review_source": "本地影响审核页人工选择",
    }
    review_book.append_event(root / EVENTS_NAME, event)
    return event


def render_draft(root: Path, report_path: Path | None = None) -> tuple[Path, str]:
    path, report, items = review_items(root, report_path)
    reviewed = [item for item in items if item.get("decision") and not item.get("already_cited")]
    if not reviewed:
        raise ValueError("当前报告还没有人工关系判断，不能生成修改草案")
    lines = [
        f"# {report.get('title') or '影响审核'}——知识修改草案",
        "",
        f"> 生成时间：{_now()}",
        "> 状态：讨论草案，不是正式知识；不会自动修改 `notes/`。",
        "> 写入门：逐项核对来源后，仍须用户明确确认才能更新精选知识。",
        f"> 来源报告：`{path}`",
        "",
    ]
    for number, item in enumerate(reviewed, 1):
        decision = item["decision"]
        lines.extend([
            f"## {number}. {item['note_title']}",
            "",
            f"- 来源：{item['source_title']}（`{item['source_id']}`）",
            f"- 正式笔记：`{item['note_path']}`",
            f"- 机器相似度：`{float(item['score']):.4f}`（语义相似度路由，弱）",
            f"- 人工关系：`{decision['relation']}`（人工关系判断）",
            f"- 建议动作：`{decision['action']}`（人工关系判断）",
            f"- 人工备注：{decision.get('note') or '（未填写）'}（人工填写）",
            "",
            "### 拟议修改",
            "",
            (decision.get("proposed_change") or "（尚未填写具体修改文字；不得据此更新正式知识。）")
            + "（人工填写，尚未确认）",
            "",
            "### 核验摘录",
            "",
            f"> 新来源片段：{item.get('material_preview') or '（无）'}",
            "",
            f"> 正式知识片段：{item.get('curated_preview') or '（无）'}",
            "",
        ])
    safe_title = _SAFE_NAME.sub("-", str(report.get("title") or "impact-review")).strip(" .-")[:70]
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    output = root / DRAFT_DIR / f"{timestamp}_{safe_title}_知识修改草案.md"
    text = "\n".join(lines).rstrip() + "\n"
    _atomic_write(output, text)
    return output, text


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>知识影响审核</title><style>
:root{color-scheme:dark;--bg:#0f131a;--card:#1a202c;--line:#344054;--muted:#9ba7b7;--accent:#7aa7ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef3f9;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{width:min(1100px,calc(100% - 28px));margin:28px auto 70px}article{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0}.meta,.hint{color:var(--muted)}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}.excerpt{white-space:pre-wrap;background:#111722;padding:12px;border-radius:8px;max-height:260px;overflow:auto}select,textarea,button{color:#eef3f9;background:#111722;border:1px solid var(--line);border-radius:8px;padding:9px}select{margin-right:8px}textarea{width:100%;min-height:74px;resize:vertical}button{cursor:pointer;background:#29354a}button:hover{border-color:var(--accent)}button.primary{background:#2457a6}.saved{color:#7ee2a8;min-height:24px}.locked{color:#7ee2a8}@media(max-width:720px){.pair{grid-template-columns:1fr}}</style></head><body><main><h1>知识影响审核</h1><p class="hint">这里只追加人工关系判断并生成讨论草案，不会直接修改正式知识。</p><div id="summary">读取中……</div><section id="items"></section><button id="draft" class="primary">生成知识修改草案</button><div id="draftResult" class="saved"></div></main><script>
const relations=[['','选择关系'],['duplicate','重复'],['supports','支持'],['refines','补充/细化'],['contradicts','冲突'],['unrelated','无关']];const actions=[['','选择动作'],['no_change','不改'],['supplement','补充'],['supersede','作废替代'],['new_topic','新建专题'],['source_only','仅登记来源']];let items=[];const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const opts=(xs,v)=>xs.map(([k,l])=>`<option value="${k}" ${k===v?'selected':''}>${l}</option>`).join('');
function card(x,i){const d=x.decision||{};if(x.already_cited)return `<article><h2>${i+1}. ${esc(x.note_title)}</h2><div class="locked">已明确纳入：正式笔记 source_ids 已引用来源 ${esc(x.source_id)}</div></article>`;return `<article data-id="${x.candidate_id}"><h2>${i+1}. ${esc(x.source_title)} → ${esc(x.note_title)}</h2><div class="meta">相似度 ${Number(x.score).toFixed(4)} · 来源 ${esc(x.source_id)}</div><div class="pair"><div><h3>新来源片段</h3><div class="excerpt">${esc(x.material_preview)}</div></div><div><h3>正式知识片段</h3><div class="excerpt">${esc(x.curated_preview)}</div></div></div><p><select class="relation">${opts(relations,d.relation)}</select><select class="action">${opts(actions,d.action)}</select></p><label>判断备注<textarea class="note">${esc(d.note||'')}</textarea></label><label>拟议修改文字<textarea class="change">${esc(d.proposed_change||'')}</textarea></label><p><button class="save">追加保存判断</button></p><div class="saved"></div></article>`}
async function load(){const r=await fetch('/api/items');const out=await r.json();if(!r.ok)throw Error(out.error||'读取失败');items=out.items;document.querySelector('#summary').textContent=`报告：${out.title} · 候选 ${items.length} 条 · 待人工判断 ${items.filter(x=>!x.already_cited).length} 条`;document.querySelector('#items').innerHTML=items.map(card).join('')}
document.querySelector('#items').addEventListener('click',async e=>{if(!e.target.classList.contains('save'))return;const a=e.target.closest('article'),msg=a.querySelector('.saved');msg.textContent='保存中……';try{const r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidate_id:a.dataset.id,relation:a.querySelector('.relation').value,action:a.querySelector('.action').value,note:a.querySelector('.note').value,proposed_change:a.querySelector('.change').value})});const out=await r.json();if(!r.ok)throw Error(out.error||'保存失败');msg.textContent='已追加保存'}catch(err){msg.textContent='保存失败：'+err.message}});
document.querySelector('#draft').addEventListener('click',async()=>{const msg=document.querySelector('#draftResult');msg.textContent='生成中……';try{const r=await fetch('/api/draft',{method:'POST'});const out=await r.json();if(!r.ok)throw Error(out.error||'生成失败');msg.textContent='草案已生成：'+out.path}catch(err){msg.textContent='生成失败：'+err.message}});load().catch(err=>document.querySelector('#summary').textContent='读取失败：'+err.message);
</script></body></html>'''


def make_handler(root: Path, report_path: Path | None = None):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, value: dict) -> None:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/items":
                try:
                    _, report, items = review_items(root, report_path)
                    self._send_json(200, {"title": report.get("title"), "items": items})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self._send_json(400, {"error": str(exc)})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                if route == "/api/decision":
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 32_768:
                        raise ValueError("请求过大")
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    self._send_json(200, save_decision(root, payload, report_path))
                elif route == "/api/draft":
                    output, _ = render_draft(root, report_path)
                    self._send_json(200, {"path": str(output)})
                else:
                    self._send_json(404, {"error": "not found"})
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="启动知识影响候选本地审核页")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()
    report_path = args.report.expanduser().resolve() if args.report else None
    path, report, items = review_items(root, report_path)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(root, path))
    print(f"影响审核页面：http://127.0.0.1:{args.port}/", flush=True)
    print(f"报告：{report.get('title')}；候选 {len(items)} 条。按 Ctrl+C 关闭。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
