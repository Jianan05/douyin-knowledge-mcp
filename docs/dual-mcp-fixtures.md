# 双 MCP 人工来源夹具

`tests/fixtures/dual_mcp/reference.json` 与 `pending.json` 完全人工构造，不含登录态或真实视频。ASR 元数据为静态夹具字段，不表示实际执行过 ASR。

两份文本讲述同一合成减脂观点，来源身份及正文不同。reference 先进入人工审核流程；其批准后，pending 用于验证非空跨来源候选。不得直接改 SQLite 或自动批准夹具。

```powershell
python -m pytest -q tests/integration/test_dual_fixtures.py
```

测试通过真实 STDIO create/status 和异步打包器，逐值核对包来源、版本及磁盘摘要。完整双 MCP 演示位于 ViDRAG 仓库的 `docs/dual-mcp-demo.md`；两个仓库独立保留源码，来源仓库不导入 ViDRAG 内部函数。

当前只完成人工编码前交接。socket/模型封锁与正向探针由双 MCP 演示进程验证；完整关系、人工审核及批准后检索尚未验收。
