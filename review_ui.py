"""旧收藏审阅页：只在本机提供按钮界面，所有决定追加到状态日志。"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import review_book
from note_sections import clean_screenshot_ocr, is_technical_failure_placeholder

TYPE_OPTIONS = (
    ("口播/口播剪辑", "已有备注线索"),
    ("引用素材+作者评论", "已确认 2 条"),
    ("纯口播", "初始候选"),
    ("口播+画面", "初始候选"),
    ("画面/教程优先", "初始候选"),
    ("餐饮/做饭", "初始候选"),
    ("营销号", "初始候选"),
    ("特效/精美画面", "初始候选"),
)
JUDGMENT_OPTIONS = (
    ("高互动量，研究传播/剪辑手法", "已有备注线索"),
    ("文本疑似不全/漏转", "历史风险 1 条"),
    ("AI变声/算法音色导致严重识别失真", "本批已确认 1 条"),
    ("观点视频的嵌入素材音频漏转", "已确认 2 条"),
    ("画面信息重要，重点保留步骤/配料", "初始候选"),
    ("图片 OCR 含平台界面噪声，待清洗", "初始候选"),
    ("原片与转录文本无关，疑似错配", "初始候选"),
)


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>旧收藏整理状态本</title><style>
:root{color-scheme:dark;--bg:#10131a;--card:#1a1f2b;--muted:#9aa4b2;--line:#303849;--accent:#79a7ff}*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:#eef2f8;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{width:min(1050px,calc(100% - 28px));margin:28px auto 80px}
h1{margin-bottom:4px}.intro,.meta,.hint{color:var(--muted)}.summary{position:sticky;top:0;z-index:2;padding:10px 0;background:linear-gradient(var(--bg) 75%,transparent)}
article{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0}h2{margin:0 0 6px;font-size:19px}a{color:var(--accent)}
.badge{display:inline-block;padding:2px 9px;border:1px solid var(--line);border-radius:999px;margin:2px 5px 2px 0;color:#cbd5e1}.preview,.transcript{white-space:pre-wrap;background:#111722;border-left:3px solid #52637e;padding:12px;border-radius:6px}.transcript{max-height:70vh;overflow:auto}details{margin:10px 0}summary{cursor:pointer;color:var(--accent);font-weight:600}
textarea{width:100%;min-height:76px;resize:vertical;color:#eef2f8;background:#111722;border:1px solid var(--line);border-radius:8px;padding:10px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
button{cursor:pointer;border:1px solid #526078;border-radius:8px;padding:8px 12px;color:#edf2f7;background:#293247}button:hover{border-color:var(--accent)}button.active{background:#2457a6;border-color:#79a7ff}.types,.judgments{display:flex;flex-wrap:wrap;gap:7px;margin:7px 0 13px}.type-tag,.quick-note{padding:6px 10px}.section-label{display:block;margin-top:13px}.evidence{font-size:11px;color:#aeb8c8}
button.delete{margin-left:auto;color:#ffb4b4;border-color:#714448;background:#382429}.saved{color:#7ee2a8;min-height:24px;margin-top:5px}@media(max-width:650px){button.delete{margin-left:0}}
</style></head><body><main><h1>旧收藏整理状态本</h1>
<p class="intro">当前状态本中的首批条目。这里的按钮只追加审阅状态，不修改转录稿、不取消收藏，也不删除文件。抖音禁止被本地页面嵌入；“Chrome 左右并排”会另开两个应用窗口，左侧保留本条审阅，右侧显示原片。</p><div class="summary" id="summary">正在读取……</div><section id="items"></section>
</main><script>
const labels={pending:'待审阅',reference:'可参考',deep_dive:'重点深挖',needs_correction:'转录需修正',exclude:'不纳入知识库',delete_requested:'申请删除（等待二次确认）'};
const normal=[['reference','可参考'],['deep_dive','重点深挖'],['needs_correction','转录需修正'],['exclude','不纳入知识库']];let items=[];
const typeOptions=__TYPE_OPTIONS__;const judgmentOptions=__JUDGMENT_OPTIONS__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function summary(){const c={};items.forEach(x=>c[x.review_status]=(c[x.review_status]||0)+1);document.querySelector('#summary').textContent=Object.entries(labels).map(([k,v])=>`${v} ${c[k]||0}`).join(' · ')}
function card(x,i){const tags=(x.tags||[]).map(t=>`<span class="badge">${esc(t)}</span>`).join('')||'<span class="hint">无标签</span>';const risks=(x.quality?.flags||[]).join('；')||'未发现明显静态风险';const full=x.quality?.preview||'';const original=x.quality?.original_preview??full;const clean=x.quality?.ocr_cleaning||{};const removed=clean.removed_lines||[];const flagged=clean.flagged_lines||[];const short=full.length>240?full.slice(0,240)+'……':full;const cutoff=x.quality?.preview_truncated?'<div class="hint">注意：该稿超过 50,000 字，状态源已明确标记截断，需回原始 Markdown 核验。</div>':'';const audit=clean.applied?`<div class="hint">截图 OCR 旁路清洗：移除 ${removed.length} 行；疑似噪声但保留 ${flagged.length} 行。${flagged.length?' 保留项：'+esc(flagged.map(v=>v.text).join('；')):''}</div>`:'';const segment=x.reference_clip_segment;const segmentInfo=segment?`<div class="hint">引用素材段旁路：${esc(segment.status)}；${esc(segment.audio)}；${esc(segment.visual)}；时间范围和候选转写待取得原媒体后填写。</div>`:'';const selected=new Set(x.video_types||[]);const typeButtons=typeOptions.map(([v,e])=>`<button class="type-tag ${selected.has(v)?'active':''}" data-type="${esc(v)}">${esc(v)} <span class="evidence">${esc(e)}</span></button>`).join('');const judgmentButtons=judgmentOptions.map(([v,e])=>`<button class="quick-note" data-judgment="${esc(v)}">${esc(v)} <span class="evidence">${esc(e)}</span></button>`).join('');
const buttons=normal.map(([k,v])=>`<button data-status="${k}" class="${x.review_status===k?'active':''}">${v}</button>`).join('');return `<article data-id="${esc(x.video_id)}"><h2>${String(i+1).padStart(2,'0')}. ${esc(x.title||x.video_id)}</h2>
<div class="meta">ID：${esc(x.video_id)} · 当前：<strong class="current">${esc(labels[x.review_status]||x.review_status)}</strong> · <button class="side-by-side">尝试 Chrome 左右并排</button> <button class="open-original" data-url="${esc(x.url)}">尝试独立打开原片</button> <button class="copy-link" data-url="${esc(x.url)}">复制原片链接</button></div><div>${tags}</div>
<p><strong>内容类型：</strong>${esc(x.content_type_note||'尚无人工类型修正')}<br><strong>核验提示：</strong>${esc(x.quality?.verification||'尚未核验')}<br><strong>文字抽查：</strong>${esc(x.quality_review_note||'尚未抽样检查')}<br><strong>映射状态：</strong>${esc(x.mapping_status||'尚未发现已确认错配')}<br><strong>自动风险：</strong>${esc(risks)}</p>${audit}${segmentInfo}<strong>清洗后短预览</strong><div class="preview">${esc(short||'（无可用作品正文；可展开原始稿查看技术记录）')}</div>
<details><summary>展开/收起清洗后全文（${full.length} 字）</summary><div class="transcript">${esc(full||'（无可用转写全文）')}</div></details><details><summary>查看原始 OCR/转写全文（${original.length} 字）</summary><div class="transcript">${esc(original||'（无可用原文）')}</div></details>${cutoff}
<strong class="section-label">审阅状态</strong><div class="actions">${buttons}<button class="delete" data-status="delete_requested">申请删除</button></div><strong class="section-label">视频类型（可多选，再点一次取消）</strong><div class="types">${typeButtons}</div><strong class="section-label">常用判断（追加到备注，不会重复）</strong><div class="judgments">${judgmentButtons}</div><p><label><strong>自由备注</strong><textarea>${esc(x.human_note||'')}</textarea></label></p>
<div class="saved"></div><div class="hint">“申请删除”只记录请求。此页面没有真实删除接口；之后仍需单独输入完全一致的作品 ID 二次确认。</div></article>`}
async function saveAnnotations(card,current,payload){const msg=card.querySelector('.saved');msg.textContent='保存中……';const r=await fetch('/api/annotations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_id:card.dataset.id,...payload})});const out=await r.json();if(!r.ok)throw Error(out.error||'保存失败');current.video_types=out.video_types||[];current.human_note=out.human_note||'';card.querySelector('textarea').value=current.human_note;msg.textContent='类型/备注已追加保存';return out}
async function load(){const r=await fetch('/api/items');if(!r.ok)throw Error(await r.text());items=await r.json();document.querySelector('#items').innerHTML=items.map(card).join('');summary()}
document.querySelector('#items').addEventListener('click',async e=>{const any=e.target.closest('button');if(!any)return;const card=any.closest('article');
const current=items.find(v=>v.video_id===card.dataset.id);
if(any.classList.contains('type-tag')){const chosen=new Set(current.video_types||[]);chosen.has(any.dataset.type)?chosen.delete(any.dataset.type):chosen.add(any.dataset.type);any.disabled=true;try{await saveAnnotations(card,current,{video_types:[...chosen],human_note:card.querySelector('textarea').value});any.classList.toggle('active',chosen.has(any.dataset.type))}catch(err){card.querySelector('.saved').textContent='保存失败：'+err.message}finally{any.disabled=false}return}
if(any.classList.contains('quick-note')){any.disabled=true;try{await saveAnnotations(card,current,{human_note:card.querySelector('textarea').value,append_judgment:any.dataset.judgment})}catch(err){card.querySelector('.saved').textContent='保存失败：'+err.message}finally{any.disabled=false}return}
if(any.classList.contains('open-original')){const popup=window.open(any.dataset.url,'douyin-original','popup=yes,width=560,height=900,resizable=yes,scrollbars=yes');card.querySelector('.saved').textContent=popup?'原片已在独立窗口打开；审阅页保持在这里':'浏览器阻止了弹窗，请点击“复制原片链接”后在另一窗口打开';return}
if(any.classList.contains('copy-link')){try{await navigator.clipboard.writeText(any.dataset.url);card.querySelector('.saved').textContent='原片链接已复制'}catch(_){prompt('请复制原片链接',any.dataset.url)}return}
if(any.classList.contains('side-by-side')){const msg=card.querySelector('.saved');msg.textContent='正在请求 Chrome 左右并排……';try{const r=await fetch('/api/side-by-side',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_id:card.dataset.id})});const out=await r.json();if(!r.ok)throw Error(out.error||'无法启动');msg.textContent=out.arranged?'两个 Chrome 窗口已经自动左右并排':'两个窗口已经打开，但 Chrome 忽略了自动位置。最少手动步骤：选审阅窗口按 Win+左，再选原片窗口按 Win+右。'}catch(err){msg.textContent='无法创建两个窗口：'+err.message+'。请复制链接后手动使用 Windows 贴靠布局。'}return}
const b=any.closest('button[data-status]');if(!b)return;const id=card.dataset.id;
const status=b.dataset.status,note=card.querySelector('textarea').value;if(status==='delete_requested'&&!confirm('这里只会记录“申请删除”，不会真实删除。确认提交申请吗？'))return;
b.disabled=true;const msg=card.querySelector('.saved');msg.textContent='保存中……';try{const r=await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_id:id,status,note})});const out=await r.json();if(!r.ok)throw Error(out.error||'保存失败');
current.review_status=out.review_status;current.human_note=out.human_note;card.querySelector('.current').textContent=labels[out.review_status];card.querySelectorAll('button[data-status]').forEach(q=>q.classList.toggle('active',q.dataset.status===out.review_status));msg.textContent='已追加记录';summary()}catch(err){msg.textContent='保存失败：'+err.message}finally{b.disabled=false}});
document.querySelector('#items').addEventListener('change',async e=>{if(!e.target.matches('textarea'))return;const card=e.target.closest('article'),current=items.find(v=>v.video_id===card.dataset.id);try{await saveAnnotations(card,current,{human_note:e.target.value})}catch(err){card.querySelector('.saved').textContent='保存失败：'+err.message}});
load().then(()=>{const id=location.hash.slice(1);if(id)document.querySelector(`article[data-id="${CSS.escape(id)}"]`)?.scrollIntoView()}).catch(err=>document.querySelector('#summary').textContent='读取失败：'+err.message);
</script></body></html>"""

PAGE = PAGE.replace("__TYPE_OPTIONS__", json.dumps(TYPE_OPTIONS, ensure_ascii=False))
PAGE = PAGE.replace("__JUDGMENT_OPTIONS__", json.dumps(JUDGMENT_OPTIONS, ensure_ascii=False))


def first_batch(root: Path, limit: int = 15) -> list[dict]:
    states = review_book.load_states(root / review_book.STATE_NAME)
    rows = sorted(states.values(), key=lambda row: (str(row.get("prepared_at") or ""), int(row.get("batch_order") or 0)))
    batch_rows = [
        row for row in rows
        if row.get("review_batch_id") and row.get("event") != "candidate_rejected"
    ]
    if batch_rows:
        newest = max(batch_rows, key=lambda row: str(row.get("prepared_at") or "")).get("review_batch_id")
        selected = sorted(
            [row for row in batch_rows if row.get("review_batch_id") == newest],
            key=lambda row: int(row.get("batch_order") or 0),
        )[:limit]
    else:
        selected = rows[:limit]
    # 状态日志是追加式快照；展示时从只读原稿实时派生清洗文本，绝不回写 Markdown。
    enriched = []
    for row in selected:
        current = dict(row)
        quality = dict(current.get("quality") or {})
        path = Path(str(current.get("path") or ""))
        if path.is_file():
            original = review_book.transcript_body(path)
            cleaning = clean_screenshot_ocr(original)
            technical_failure = is_technical_failure_placeholder(cleaning["cleaned_text"])
            usable = "" if technical_failure else cleaning["cleaned_text"]
            quality["preview"] = usable[:50_000]
            quality["original_preview"] = original[:50_000]
            quality["preview_truncated"] = len(usable) > 50_000
            quality["original_preview_truncated"] = len(original) > 50_000
            quality["ocr_cleaning"] = {key: cleaning[key] for key in ("applied", "removed_lines", "flagged_lines")}
            if technical_failure:
                flags = list(quality.get("flags") or [])
                warning = "原始稿只有画面 OCR 失败占位符，不能当作作品正文"
                if warning not in flags:
                    flags.append(warning)
                quality["flags"] = flags
                quality["verification"] = "无可用作品正文；原始稿仅含画面 OCR 技术失败占位符"
                quality["content_availability"] = "technical_failure_placeholder"
            current["quality"] = quality
        enriched.append(current)
    return enriched


def save_review(root: Path, payload: dict) -> dict:
    video_id = str(payload.get("video_id") or "").strip()
    allowed_ids = {str(row.get("video_id") or "") for row in first_batch(root)}
    if video_id not in allowed_ids:
        raise ValueError("该作品不在首批 15 条中")
    status = str(payload.get("status") or "").strip()
    allowed = {"pending", "reference", "deep_dive", "needs_correction", "exclude", "delete_requested"}
    if status not in allowed:
        raise ValueError("不支持的审阅状态")
    review_book.set_status(root, video_id, status, str(payload.get("note") or ""))
    state = review_book.load_states(root / review_book.STATE_NAME)[video_id]
    return {"video_id": video_id, "review_status": state.get("review_status"), "human_note": state.get("human_note", "")}


def save_annotations(root: Path, payload: dict) -> dict:
    video_id = str(payload.get("video_id") or "").strip()
    allowed_ids = {str(row.get("video_id") or "") for row in first_batch(root)}
    if video_id not in allowed_ids:
        raise ValueError("该作品不在当前 15 条中")
    state = review_book.set_annotations(
        root,
        video_id,
        video_types=payload.get("video_types") if "video_types" in payload else None,
        human_note=str(payload.get("human_note") or "") if "human_note" in payload else None,
        append_judgment=str(payload.get("append_judgment") or "") if "append_judgment" in payload else None,
        allowed_types={value for value, _ in TYPE_OPTIONS},
        allowed_judgments={value for value, _ in JUDGMENT_OPTIONS},
    )
    return {
        "video_id": video_id,
        "review_status": state.get("review_status"),
        "video_types": state.get("video_types") or [],
        "human_note": state.get("human_note") or "",
    }


def chrome_path() -> Path | None:
    for path in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ):
        if path.is_file():
            return path
    return None


def chrome_windows() -> dict[int, tuple[int, int, int, int]]:
    """返回当前可见 Chrome 顶层窗口及其屏幕坐标，用于验证而非盲报成功。"""
    user32 = ctypes.windll.user32
    windows: dict[int, tuple[int, int, int, int]] = {}
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_proc
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != "Chrome_WidgetWin_1":
            return True
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            windows[int(hwnd)] = (rect.left, rect.top, rect.right, rect.bottom)
        return True

    # wintypes 需显式导入后才会成为 ctypes 的属性。
    from ctypes import wintypes  # noqa: F401
    user32.EnumWindows(collect, 0)
    return windows


def launch_side_by_side(root: Path, video_id: str, port: int) -> dict:
    item = next((row for row in first_batch(root) if str(row.get("video_id") or "") == video_id), None)
    if not item:
        raise ValueError("该作品不在首批 15 条中")
    chrome = chrome_path()
    if not chrome:
        raise ValueError("未找到 Google Chrome")
    user32 = ctypes.windll.user32
    width, height = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    if width < 800 or height < 600:
        raise ValueError("当前屏幕尺寸不足以自动并排")
    half = width // 2
    review_url = f"http://127.0.0.1:{port}/#{video_id}"
    video_url = str(item.get("url") or "")
    before = set(chrome_windows())
    common = [str(chrome), "--new-window"]
    subprocess.Popen(common + [f"--app={review_url}", "--window-position=0,0", f"--window-size={half},{height}"])
    subprocess.Popen(common + [f"--app={video_url}", f"--window-position={half},0", f"--window-size={width-half},{height}"])
    new_windows: dict[int, tuple[int, int, int, int]] = {}
    for _ in range(20):
        time.sleep(0.25)
        current = chrome_windows()
        new_windows = {hwnd: rect for hwnd, rect in current.items() if hwnd not in before}
        if len(new_windows) >= 2:
            break
    if len(new_windows) < 2:
        raise ValueError("Chrome 没有实际创建两个可验证的新窗口")
    rects = list(new_windows.values())
    left_and_right = any(rect[0] < half for rect in rects) and any(rect[0] >= half - 80 for rect in rects)
    return {
        "launched": True,
        "arranged": left_and_right,
        "review_url": review_url,
        "video_url": video_url,
        "window_count": len(new_windows),
    }


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route == "/": self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/items": self._send(200, json.dumps(first_batch(root), ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")
            else: self._send(404, b"not found", "text/plain; charset=utf-8")
        def do_POST(self) -> None:
            route = urlparse(self.path).path
            if route not in {"/api/status", "/api/annotations", "/api/side-by-side"}: self._send(404, b"not found", "text/plain; charset=utf-8"); return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 16_384: raise ValueError("请求过大")
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if route == "/api/status":
                    result = save_review(root, payload)
                elif route == "/api/annotations":
                    result = save_annotations(root, payload)
                else:
                    result = launch_side_by_side(root, str(payload.get("video_id") or ""), int(self.server.server_address[1]))
                self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        def log_message(self, format: str, *args) -> None: return
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="启动旧收藏本地审阅界面"); parser.add_argument("--dir", type=Path, default=review_book.DEFAULT_LIBRARY); parser.add_argument("--port", type=int, default=8765); args = parser.parse_args(); root = args.dir.expanduser().resolve()
    if not first_batch(root): raise SystemExit("状态本为空；请先运行 review_book.py prepare --limit 15")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(root)); print(f"审阅页面：http://127.0.0.1:{args.port}/", flush=True); print("按 Ctrl+C 关闭。页面只有追加状态接口，没有真实删除接口。", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
