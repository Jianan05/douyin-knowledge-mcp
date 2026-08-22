# 抖音链接 → 本地转录 → 分类入库

给 AI 助手看的操作说明。人看的完整文档在 README.md。

## 用户说「把这些抖音链接入库」时，跑这个

```bash
cd C:\Users\qjn15\Desktop\douyin-transcribe
.\runtime\python\python.exe ingest.py <链接1> <链接2> ...
.\runtime\python\python.exe ingest.py --classify --apply
.\runtime\python\python.exe ingest.py --sync
```

链接可以直接粘抖音的整段分享文本，脚本自己提取 URL。已入库的按 video_id 自动跳过。

## ⛔ 最重要的一条：转写稿不要读进上下文

一条 5 分钟视频约 2000 字。**除非用户明确说「这条我要看/要笔记」，
否则不要 cat 转写稿正文**，只看脚本回显的那一行摘要就够了。
这是这套工具存在的全部意义——把内容留在磁盘上，不在对话里烧 token。

## 其它命令

- `ingest.py --list` 看全库有什么、哪些还没写笔记（不打印正文）
- `ingest.py --classify` 只预览分类结果，不落盘
- `ingest.py --force <链接>` 无视去重重转，旧文件挪进 `_已替换/`

## 库在哪

`C:\Users\qjn15\Desktop\DouyinNotes\`
- `inbox/<分类>/` 转好了、还没写笔记
- `notes/<分类>/` 写完笔记的（把 frontmatter 的 status 改成 noted，再跑 --sync 自动归位）
- 分类规则在本项目的 `categories.toml`，顺序即优先级，第一条命中就定

## 硬约束

- ⛔ 不要 `git push`：这仓库故意没配远程，只做本地存档
- ⛔ 不要关 HTTPS 校验，不要把 Cookie 写进日志
- ⛔ 不要动 `data/`（抖音登录态）和 `runtime/`（私有 Python / 浏览器 / 模型）
- ⛔ 不要改 `C:\Users\qjn15\Desktop\opp`
