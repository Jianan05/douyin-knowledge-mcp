# Douyin Knowledge Ingest

面向 Codex、MCP 和下游 RAG 系统的本地抖音知识采集工具。它不是一个网页下载器，而是把抖音链接、收藏视频、图文和纯文字作品转换成可索引的本地 Markdown、时间戳转录来源包和视觉资产。

> 本项目最初基于 [ZY-ZhichaoYu/douyin-transcribe](https://github.com/ZY-ZhichaoYu/douyin-transcribe) 开发，现已为个人知识库和 RAG 工作流大幅扩展与重构。上游项目及本衍生版均保留 MIT 许可证。
> 最初源码以不带 Git 历史的形式导入，因此无法确定当时对应的上游 commit。

## 能力

- 接收抖音短链、长链或 App 整段分享文本；普通公开 Bilibili 视频链接也可走同一条下载与转录链路。
- 用本地 `faster-whisper` 转录视频或音频，生成 Markdown 稿和带时间戳的 JSON 来源包。
- 批量清点和处理抖音「收藏」总列表，按作品 ID 去重、断点续跑并记录失败。
- 区分视频、图文和纯文字作品；图文使用 RapidOCR，视频转录过少时可抽帧 OCR 补充画面文字。
- 对收藏做视觉校准：保存原图或原视频、代表帧、联系表、OCR 和可解释的初步分类证据。
- 按 `categories.toml` 归类、按 frontmatter 状态同步文件，并维护 JSONL 索引。
- 只读审计远程收藏与本地索引；明确错误可移入可恢复隔离区，不直接删除。
- 以 MCP server 暴露链接转录、下载和本地媒体转录能力，便于 Codex 或其他 Agent 调用。

## 项目定位

这是一个「内容提取和入库层」，不包含独立 Web UI，也不自己实现向量数据库或问答界面。下游 RAG 系统可以消费：

- `inbox/` 与 `notes/` 中的 Markdown。
- `_source_packages/` 中的带时间戳 JSON。
- `assets/` 中的原始媒体、代表帧、联系表和 OCR。
- `index.jsonl` 中的去重与元数据索引。

## 环境要求

- Python 3.10+（64 位）。
- Playwright Chromium。
- 首次使用需要联网下载 Python 依赖、Chromium 和 Whisper 模型。
- 下载完整 Bilibili MP4 时建议安装 ffmpeg。
- 目前主要在 Windows 上开发和测试；收藏连续续跑的单实例文件锁为 Windows 实现。

## 安装

```powershell
# 在已克隆或已解压的仓库目录中执行
Set-Location C:\path\to\douyin-transcribe

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

有 NVIDIA CUDA 环境并希望使用 GPU 时，可额外安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt
```

Whisper 模型大小可用 `--model tiny|base|small|medium|large-v3` 指定；不指定时按设备自动选择。

## 快速使用

### 0. 不使用私人数据的知识闭环演示

这条命令只使用项目自带的合成文字，演示“来源 → 人工审阅 → 已确认知识”的状态和溯源关系；不会访问抖音、Cookie 或默认知识库：

```powershell
.\runtime\python\python.exe demo_workflow.py
```

命令会在系统临时目录生成原始材料、审阅状态本、确认事件和一条 `notes/` 正式笔记，并输出各文件路径。演示中的人工确认是明确标注的固定测试夹具，不冒充真实用户判断。

### 1. 登录抖音

首次使用或登录过期时，打开本项目专用的持久化 Chromium profile：

```powershell
python ingest.py --login
```

程序不读取日常 Chrome/Edge 的 Cookie，也不接触密码。登录状态保存在已忽略的 `data/` 目录。

随时可以只读查看库规模、审阅覆盖率、当前失败项、精选知识数量和索引状态：

```powershell
.\runtime\python\python.exe library_status.py --check-semantic
```

该命令不会输出转录正文；不加 `--check-semantic` 时连正文哈希扫描也会跳过。

### 2. 链接入库

```powershell
python ingest.py "<抖音链接或整段分享文本>"
python ingest.py "<链接1>" "<链接2>"
python ingest.py --file .\urls.txt
```

默认库目录是 `~/Desktop/DouyinNotes`。可以用 `--dir` 改到任意本地目录：

```powershell
python ingest.py --dir D:\Knowledge\Douyin "<链接>"
```

### 3. 分类与同步

仓库内的 `categories.toml` 是通用示例。如需个人化规则，将它复制为已被 Git 忽略的 `categories.local.toml`；程序会优先读取本地规则。也可通过 `DOUYIN_CATEGORIES_FILE` 指定任意规则文件。

```powershell
# 只预览分类
python ingest.py --classify

# 写入分类，然后按 frontmatter 状态归位并重建索引
python ingest.py --classify --apply
python ingest.py --sync

# 不读正文地查看库状态
python ingest.py --list
```

### 4. 处理收藏

建议首先做只读清点：

```powershell
python ingest.py --favorites --dry-run --brief
```

清点结果只统计接口实际返回的有效作品。抖音接口可能另报 `invalid_item_count`，通常对应作者隐藏、删除或当前账号不可见的历史收藏；该字段可能是单页值而非累计总数，程序只报告单次响应中观察到的最大值，不把它们伪装成可处理作品。若接口始终返回 `has_more=1`，默认模式会拒绝继续；`--keep-collected` 只处理当前稳定可见部分并保留全部收藏，方便以后去重补齐。

首次正式跑建议保留收藏，确认本地产物符合需求后再使用取消收藏流程：

```powershell
python ingest.py --favorites --keep-collected
```

如果不加 `--keep-collected`，只有处理结果为成功或安全跳过，并且索引对应路径确实存在且为普通文件的作品，才会进入待取消队列。`[warn]`、失败、未知类型、无语音且无有效 OCR、OCR 失败或视觉价值不确定的作品都会保留；取消前还会重新完整清点收藏。预计超过 30 分钟的取消任务会自动延后。`--keep-collected` 始终只保存，不写入或执行取消队列。

```powershell
python ingest.py --uncollect-pending
```

连续安全补齐收藏时，可用环境变量改库位置：

```powershell
$env:DOUYIN_NOTES_DIR = "D:\Knowledge\Douyin"
python continuous_favorites.py --target-min 100
```

### 5. 旧收藏小批量审阅

直接复用本地已有转录，不会重新转录整库：

```powershell
.\runtime\python\python.exe review_book.py prepare --limit 15
.\runtime\python\python.exe review_book.py prepare-ids <video_id1> <video_id2>
.\runtime\python\python.exe review_book.py set <video_id> 可参考 --note "为什么值得保留"
.\runtime\python\python.exe review_book.py set <video_id> 重点深挖 --note "重点看画面和剪辑"
.\runtime\python\python.exe review_book.py set <video_id> 转录需修正 --note "疑似错词"
.\runtime\python\python.exe review_book.py set <video_id> 不纳入知识库 --note "与目标无关"
```

需要直接点击操作时，启动只显示首批 15 条的本地审阅页：

```powershell
.\runtime\python\python.exe review_ui.py
```

然后打开 `http://127.0.0.1:8765/`。按钮与备注仍追加写入同一个 `_审阅状态.jsonl`；页面只有“申请删除”，没有真实删除接口。

状态采用追加式 `_审阅状态.jsonl`，可读视图是 `_旧收藏整理状态本.md`。“不纳入知识库”只让 chunk/RAG 跳过该作品，不删除文件、不取消收藏。

“彻底删除”是不同动作，必须先申请，再用完全一致的视频 ID 二次确认：

```powershell
.\runtime\python\python.exe review_book.py set <video_id> 彻底删除 --note "删除原因"
.\runtime\python\python.exe review_book.py delete <video_id> --confirm-video-id <video_id>
```

执行后会删除可精确归属的原稿、来源包、视觉资产、索引/失败队列记录并使 chunk 向量缓存失效，只留下不含原始内容的 `_删除标记.jsonl`，防止午夜收藏同步重新入库。旧的人工综合话题稿缺少来源 ID 映射，命令会明确警告，不能声称已自动清除其中无法追溯的改写内容。

### 6. 把人工确认的结论写入精选知识库

只有已经审为“可参考”或“重点深挖”的来源才能支持正式笔记，并且必须显式声明用户已经确认：

```powershell
.\runtime\python\python.exe knowledge_notes.py `
  --title "结论标题" `
  --conclusion "用户明确确认的结论" `
  --source-id <video_id> `
  --rationale "为什么形成这个结论" `
  --scope "适用范围" `
  --confirmed-by "用户姓名或标识" `
  --user-confirmed
```

命令会在 `notes/` 新建带来源 ID、确认时间和推导依据的笔记，并追加 `_knowledge_events.jsonl`。它不会自动摘要原稿，也不会覆盖同名正式笔记。

语义索引会给原始素材标记 `source_layer=material`，给正式笔记标记 `source_layer=curated`。可分别检索，也可跨层比较：

```powershell
.\runtime\python\python.exe semantic_index.py --build
.\runtime\python\python.exe semantic_index.py --status
.\runtime\python\python.exe semantic_index.py --query "具体问题" --layer curated
```

索引更新按正文哈希复用未变化的向量；`--status` 会扫描当前素材并明确报告 `current` 或 `stale`。

已有正式知识后，可以用明确来源 ID 或采集时间生成“可能受影响专题”报告：

```powershell
.\runtime\python\python.exe impact_scan.py `
  --source-id <video_id1> --source-id <video_id2>

.\runtime\python\python.exe impact_scan.py --since 2026-09-01

# 日常推荐：处理刚被人工标成“可参考/重点深挖”的来源
.\runtime\python\python.exe impact_scan.py --pending-reviewed
```

人工审阅首次进入“可参考/重点深挖”时会追加到 `_待影响扫描.jsonl`。`--pending-reviewed` 会按需增量更新过期的语义索引，成功生成报告后再在同一追加式队列中记录完成事件；中途失败不会吞掉队列。命令会同时写 Markdown 讨论包和 JSON 机器报告。正式笔记 `source_ids` 中已有的来源会直接标为 `already_incorporated`；其余来源的相似度只用于候选路由，`duplicate / supports / refines / contradicts / unrelated` 必须人工核对后填写。报告不会直接修改正式知识。

需要审核影响候选时，启动本地页面：

```powershell
.\runtime\python\python.exe impact_review.py
```

页面会读取最新的非作废影响报告，把关系判断追加到 `_impact_review_events.jsonl`，并可生成 `_讨论工作区/知识修改草案/` 下的讨论稿。讨论稿带机器路由与人工判断来源标记，但不会改写 `notes/`；正式知识仍须通过显式用户确认门。

### 7. 视觉校准与审计

```powershell
# 先看抽样清单，不下载、不取消收藏
python ingest.py --visual-calibration 20 --dry-run

# 保存校准资产与报告，仍不取消收藏
python ingest.py --visual-calibration 20

# 只读比较收藏接口和本地索引
python ingest.py --audit-favorites
```

`--audit-apply` 会把明确错误移入可恢复隔离区，应先人工阅读预览报告。

## MCP 工具

`server.py` 是使用 stdio 的 FastMCP server。配置时请使用虚拟环境中 Python 和 `server.py` 的绝对路径：

```json
{
  "mcpServers": {
    "douyin": {
      "command": "C:\\path\\to\\douyin-transcribe\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\douyin-transcribe\\server.py"]
    }
  }
}
```

| 工具 | 用途 |
|---|---|
| `analyze_video(url)` | 同步转录抖音或 Bilibili |
| `video_to_text(url)` | 异步启动转录并返回 `job_id` |
| `get_transcript_result(job_id)` | 查询异步任务 |
| `download_video(url)` | 下载源视频 |
| `transcribe_video(file_path)` | 转录本地音视频文件 |

`analyze_douyin`、`douyin_to_text` 和 `download_douyin` 是保留的兼容工具名。

## 库目录

```text
DouyinNotes/
├─ inbox/                  # 已提取，待整理的 Markdown
├─ notes/                  # status=noted 后归位的笔记
├─ _source_packages/       # 时间戳转录和 OCR 来源 JSON
├─ assets/                 # 视觉校准的原始媒体与代表帧
├─ index.jsonl             # 去重与元数据索引
├─ _转录进度.md           # 只含进度，不含转录正文
├─ _失败记录.jsonl        # 机器可读失败事件
├─ _失败记录.md           # 便于人工查看的失败摘要
└─ _待取消收藏.jsonl      # 已安全入库作品的待处理队列
```

## 安全与隐私

- 仓库内的 `data/`、`runtime/`、模型缓存和库产物目录均被 Git 忽略；这可防止敏感运行数据误提交。
- 默认的外部 `~/Desktop/DouyinNotes` 不属于本仓库，仍不得另行公开其中的收藏元数据、转录正文、媒体、审计产物或隔离内容。
- 不要关闭 HTTPS 证书验证，不要把 Cookie 或浏览器 profile 提交到仓库。
- 项目只处理你有权访问和保存的内容；公开代码时不要一并公开收藏作品、转录正文或视频文件。
- 取消收藏是有状态操作；先用 `--dry-run` 和 `--keep-collected` 验证流程。

## 开发与测试

```powershell
python -m py_compile server.py ingest.py douyin_browser.py douyin_collects.py
python -m unittest discover -s tests -v
```

测试用临时目录和 mock，不需要读取真实收藏或转录正文。

## 可选的旧话题聚类模块

`topic_index.py`、`cluster.py` 和 `link_notes.py` 保留了基于 BGE-M3 的本地聚类实验，目前不在默认入库流程中。如需运行，额外安装 `sentence-transformers`、`scikit-learn` 和 `jieba`；模型体积较大。

## 已知限制

- 抖音页面、接口或风控变化后，抓取可能需要更新。
- 收藏和验证码流程依赖持久化 Chromium 登录状态。
- Bilibili 支持目前只对普通公开、游客可见的单视频做过真实冒烟测试（2026-09-01：15 秒媒体下载成功；3 分 36 秒口播视频完成下载和 Whisper tiny 非空转录）。登录/会员内容、高清清晰度、地区限制、合集和分 P 未验证，应视为尽力支持。
- 转录准确率取决于音质、背景音乐、多人重叠和 Whisper 模型大小。
- 视觉分类是本地特征推断，校准报告会明确标记「推断，未证实」，不应盲目代替人工判断。

## License and attribution

MIT License，见 [LICENSE](./LICENSE)。

- Upstream: [ZY-ZhichaoYu/douyin-transcribe](https://github.com/ZY-ZhichaoYu/douyin-transcribe)
- Original copyright: `Copyright (c) 2026 ZY-ZhichaoYu`
- This derivative retains the upstream copyright notice and permission text as required by the MIT License.

## English summary

This is a local, agent-oriented ingestion pipeline for Douyin content, with best-effort support for public guest-accessible Bilibili videos. It turns links and Douyin favorites into Markdown, timestamped transcript packages, OCR output, visual assets, and a deduplicated local index for downstream Codex/MCP/RAG workflows. The project intentionally has no standalone web UI. See the Chinese sections above for installation, safety, verified scope, and command reference.
