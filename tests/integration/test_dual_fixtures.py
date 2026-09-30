"""双 MCP 两份人工来源夹具的真实打包进程验收。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest


@pytest.mark.parametrize("name", ["reference", "pending"])
def test_dual_fixture_actual_source_process(tmp_path: Path, name: str) -> None:
    """跨 STDIO 打包人工夹具并核对身份、版本和磁盘摘要。"""
    repo = Path(__file__).resolve().parents[2]
    fixture = json.loads((repo / "tests/fixtures/dual_mcp" / (name + ".json")).read_text(encoding="utf-8"))
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / (fixture["source"]["source_id"] + ".json")).write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE"))}
    env["DOUYIN_SOURCE_PACKAGE_ROOT"] = str(tmp_path / "library")
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(repo / "server.py"), "--test-fixture-root", str(fixture_root)], cwd=repo, env=env)

    async def exercise() -> dict:
        """只调用公开工具，不导入服务器业务函数。"""
        with (tmp_path / "server.stderr").open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    created = await client.call_tool("create_source_package", {
                        "protocol_version": "1.0.0", "url": fixture["source"]["url"],
                        "model_size": "tiny", "include_ocr": True})
                    assert created.structuredContent["ok"] is True
                    for _ in range(100):
                        result = await client.call_tool("get_source_package_status", {
                            "protocol_version": "1.0.0", "job_id": created.structuredContent["data"]["job_id"]})
                        envelope = result.structuredContent
                        assert envelope["ok"] is True
                        data = envelope["data"]
                        assert data["status"] != "failed", data
                        if data["status"] == "succeeded":
                            return data
                        await asyncio.sleep(0.05)
                    raise AssertionError("人工来源包生成超时")
    data = asyncio.run(exercise())
    assert data["schema_version"] == "2.0.0"
    path = Path(data["source_package_path"])
    assert path.is_relative_to(tmp_path / "library")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == data["package_sha256"]
    package = json.loads(path.read_text(encoding="utf-8"))
    assert package["source"]["source_id"] == fixture["source"]["source_id"]
