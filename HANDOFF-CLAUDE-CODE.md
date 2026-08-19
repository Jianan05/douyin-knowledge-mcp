# Claude Code 交接：抖音链接转文字桌面工具

## 目标

把公开且用户本人能在浏览器中正常播放的抖音视频，通过链接取得媒体流，随后用本机
`faster-whisper` 转成原文。当前阶段先做独立桌面工具；不要接入 `opp` V1，也暂不配置 MCP。

核心需求是“链接 → 媒体 → 本地转录”，本地文件上传不是主要方案。

## 工作目录与启动方式

- 工具目录：`C:\Users\qjn15\Desktop\douyin-transcribe`
- 启动入口：`启动抖音转文字.bat`
- 本地页面：`http://127.0.0.1:7860`
- 私有 Python：`runtime\python\python.exe`（Python 3.13.15）
- Playwright 浏览器：`runtime\ms-playwright`
- Hugging Face 缓存：`runtime\huggingface`

不要扫描、提交或修改 `runtime\` 中的第三方包和二进制文件，除非确有依赖问题。

## 已完成

1. 从 `ZY-ZhichaoYu/douyin-transcribe` 安装源码并做过人工安全检查。
2. 依赖已完整安装，Chromium 可以正常启动，Gradio 页面能返回 HTTP 200。
3. `server.py` 原先关闭 HTTPS 证书校验，现已恢复系统证书校验；不要重新设置
   `check_hostname = False` 或 `CERT_NONE`。
4. MCP 2.0 与源码不兼容，`requirements.txt` 已固定为 `mcp>=1.0,<2`，当前为 1.29.0。
5. 启动脚本已改为使用目录内的私有 Python、Playwright 和模型缓存。

## 可复现故障

测试链接：

`https://v.douyin.com/5u4sbrFNnCE/`

它会解析到视频 ID：

`7675691164510522687`

网页报错摘要：

```text
Playwright 未能拦截到视频信息 (TimeoutError)
iesdouyin 分享页：未找到 video 字段
douyin 视频页：ValueError
```

另用已安装的最新版 `yt-dlp` 对同一链接测试，返回：

```text
Fresh cookies (not necessarily logged in) are needed
```

这说明链接解析正常，失败点是抖音媒体获取层，不是 Whisper、Gradio 或用户输入。

## 原因判断

当前实现每次创建全新的无痕 headless Chromium：

```python
browser = await p.chromium.launch(headless=True)
ctx = await browser.new_context(...)
```

它没有持久化 Cookie/LocalStorage，也没有人工登录或验证流程。当前抖音页面没有触发代码只监听的
`aweme/v1/web/aweme/detail` 响应，移动分享页的 `window._ROUTER_DATA` 也不再包含预期视频字段。

不能把问题简单处理成“给 yt-dlp 传一个 Cookie 文件”：2026 年仍有最新版 yt-dlp 在有效 Cookie 下
返回 Fresh cookies 的报告。应优先复用真实浏览器页面产生的签名请求和媒体请求。

## 推荐实现（按顺序）

### 1. 独立、持久化、可见的抖音浏览器

- 为本工具建立专用 profile，例如 `data\douyin-browser-profile\`。
- 增加“首次登录抖音”入口，使用 Playwright `launch_persistent_context(..., headless=False)`。
- 由用户本人扫码或完成验证码；程序不得接触密码。
- 后续抓取复用同一 profile，不读取用户日常 Chrome/Edge 的 Cookie。
- Cookie/profile 属于敏感本地数据，必须加入 `.gitignore`，错误日志不得输出 Cookie 值。

### 2. 从真实页面捕获媒体，而不是只等一个 API 名称

导航到最终视频页面时，同时捕获：

- `aweme/detail` 等返回视频结构的 JSON 响应；
- `video/*`、`audio/*`、MP4、M3U8、带 Range 请求的媒体响应；
- 页面中 `video.currentSrc` / `video.src`；
- `performance.getEntriesByType('resource')` 中的媒体资源 URL。

如果 CDN URL 依赖 Cookie、Referer 或 Range Header，下载时应复用该 browser context 的 Cookie 和必要请求头；
不要把内部 Cookie 打印到终端。优先取得音频流；没有独立音频时取包含音频的 MP4。

### 3. 浏览器能播放但直链仍无法复用时

媒体既然能播放，字节一定已进入浏览器。可把 CDP Network 事件作为下一层兜底，捕获真实请求 URL、
请求头和响应；必要时研究浏览器内的 `captureStream()`/MediaRecorder，但录制属于最后手段，实时耗时且
实现复杂，不能作为第一方案。

### 4. 保留当前 Whisper 和 UI

- 不替换本地 `faster-whisper`。
- 保留当前 `base` 默认模型及流式进度。
- 成功后增加自动保存 `.txt`，方便以后作为第一批 RAG/摘要测试语料。
- 本地文件转录可以作为次要保底，但不要把它当成解决本故障的主要交付。

## 验收标准

1. 用户只需首次在专用浏览器中登录一次。
2. 上述复现链接能够取得媒体并完成转录，或明确证明视频在同一专用浏览器中也无法播放。
3. 再测试至少 4 条不同作者、不同长度的普通公开视频。
4. 可播放的普通视频成功率至少 4/5；失败时明确区分登录失效、验证码、媒体获取、下载和转录阶段。
5. 重启工具后登录态仍可用。
6. 结果自动保存为 UTF-8 TXT，并在网页上显示路径。
7. 服务仍只监听 `127.0.0.1`，TLS 校验保持开启，日志不泄露 Cookie。

## 验证命令

在本目录执行：

```powershell
.\runtime\python\python.exe -m py_compile server.py app.py
.\runtime\python\python.exe -m pip check
```

启动网页：

```powershell
.\run_web.ps1
```

## 代码修改注意事项

- 先阅读 `server.py` 的 `_get_video_object`、分享页兜底和下载函数，再设计边界；避免到处打临时补丁。
- 不要改 `C:\Users\qjn15\Desktop\opp`；这是独立实验工具，等 `opp` V1 完成后再讨论集成。
- 当前目录不是原项目的 Git 工作树。修改前建议创建只包含源码的小型本地 Git 基线，并排除
  `runtime/`、浏览器 profile、Cookie、模型、下载文件、转录产物和安装日志。
- 不要为了“成功”绕过 HTTPS 校验，也不要调用不透明的第三方解析网站或上传视频内容。

## 可参考但不要盲目整套替换

- `CatWong1983/douyin-video-whisper`：可见浏览器、Chrome CDP、本地 Whisper、TXT/Markdown 输出。
- `pazwusimple-netizen/douyin-mcp` / README 中实际指向的 `wuyuxiang2/douyinmcp`：持久化登录和
  Cookie 管理更完整，但范围较大，转录默认依赖云端 ASR Key，不符合当前“本地 Whisper、先做桌面工具”的重点。

优先借鉴其浏览器登录/媒体获取思路，不要在未审计代码和依赖前直接替换现有安装。
