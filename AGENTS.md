# Douyin Knowledge Ingest — Agent Instructions

本项目是面向 Codex、MCP 和下游 RAG 的命令行内容采集工具，不包含独立 Web UI。

## 常用工作流

处理链接：

```powershell
python ingest.py <链接1> <链接2>
python ingest.py --classify --apply
python ingest.py --sync
```

链接可以是 URL 或抖音 App 的整段分享文本。已入库作品按 video_id 自动跳过。

处理收藏前先只读清点：

```powershell
python ingest.py --favorites --dry-run --brief
```

首次正式处理建议保留收藏：

```powershell
python ingest.py --favorites --keep-collected
```

## 收藏内容处理原则

- 区分视频、图文、纯文字和未知类型，不要一律当作音频转录。
- 口播内容保存转录稿，并在语音信息不足时使用画面 OCR 补充。
- 图文内容保留 OCR；视觉校准流程还应保存原图或原视频、代表帧和联系表。
- 只有确认已经写入本地库的作品才能进入待取消收藏队列。
- 失败、无法识别或视觉价值不确定的作品必须保留收藏。
- 取消收藏预计超过 30 分钟时，先保存待取消队列，不阻塞入库。

## 上下文纪律

除非用户明确要求查看、总结或整理某条内容，否则不要读取转录稿正文进入对话上下文。日常批处理只查看命令回显、元数据、状态和错误摘要。

## 本地库

默认库目录是 `~/Desktop/DouyinNotes`，可以用 `--dir` 指定其他位置。

- `inbox/`：已提取、待整理内容
- `notes/`：完成整理的笔记
- `_source_packages/`：带时间戳的机器可读来源包
- `assets/`：视觉校准资产
- `_index.jsonl`：去重与元数据索引

## 安全约束

- 不要读取、提交或修改 `data/` 中的登录 profile 和 Cookie。
- 不要提交或修改 `runtime/` 中的私有运行时、浏览器和模型缓存。
- 不要关闭 HTTPS 证书验证，也不要把 Cookie 写入日志或错误信息。
- 使用 `--audit-apply` 前先运行只读审计并检查报告。
- 未经用户明确要求，不要执行 `git push`。
- 不要修改用户未放入任务范围的其他项目或知识库。
