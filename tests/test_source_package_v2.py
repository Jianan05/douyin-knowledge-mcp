from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

import server
from source_package import (
    SourcePackageError,
    build_protected_fields,
    canonical_json_bytes,
    write_source_package_v2,
)


PROMPT_HASH = hashlib.sha256(
    server.build_initial_prompt(server.DEFAULT_TERMS).encode("utf-8")
).hexdigest()


def _fixture(*, text: str = "示例原句", ocr: str = "画面文字") -> dict:
    return {
        "fixture_version": "1.0.0",
        "source": {
            "platform": "douyin",
            "source_id": "synthetic-video-001",
            "url": "https://fixture.invalid/synthetic-video-001",
            "title": "合成示例 #synthetic",
            "tags": ["synthetic"],
            "duration_seconds": "2.2",
        },
        "transcript": {
            "text": text,
            "segments": [
                {
                    "start_seconds": "0",
                    "end_seconds": "2.2",
                    "raw_text": text,
                    "cleaned_text": text,
                    "cleaning": [],
                    "avg_logprob": "-0.125",
                    "no_speech_prob": "0.02",
                    "compression_ratio": "1.1",
                    "temperature": "0",
                }
            ],
            "asr": {
                "model": "tiny",
                "language": "zh",
                "vad_filter": True,
                "device": "cpu",
                "compute_type": "int8",
                "initial_prompt_enabled": True,
                "initial_prompt_sha256": PROMPT_HASH,
                "condition_on_previous_text": False,
                "beam_size": 5,
                "vad_threshold": "0.3",
                "min_silence_duration_ms": 800,
                "language_probability": "0.99",
                "detected_duration_seconds": "2.2",
            },
        },
        "ocr": {
            "screen_ocr": ocr,
            "screen_candidates": [{"text": "ProjectName", "frame_count": 2}],
        },
    }


def _protected(
    *,
    text: str = "示例原句",
    ocr: str = "画面文字",
    policy: str = "sparse-audio-douyin-v1",
    title: str = "合成示例 #synthetic",
    model: str = "tiny",
):
    fixture = _fixture(text=text, ocr=ocr)
    fixture["source"]["title"] = title
    fixture["transcript"]["asr"]["model"] = model
    return build_protected_fields(
        source=fixture["source"],
        segments=fixture["transcript"]["segments"],
        asr=fixture["transcript"]["asr"],
        screen_ocr=ocr,
        screen_candidates=fixture["ocr"]["screen_candidates"],
        ocr_policy=policy,
    )


def _fixed_now() -> datetime:
    return datetime(2026, 9, 19, 1, 2, 3, tzinfo=timezone.utc)


def _write_fixture(root: Path, value: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "synthetic-video-001.json"
    path.write_text(json.dumps(value or _fixture(), ensure_ascii=False), encoding="utf-8")
    return path


def test_v2_package_is_canonical_idempotent_and_keeps_old_results(tmp_path: Path) -> None:
    protected, projection = _protected()
    first = write_source_package_v2(tmp_path, protected, projection, now=_fixed_now)
    second = write_source_package_v2(
        tmp_path,
        protected,
        projection,
        now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    assert second == first
    path = Path(first["source_package_path"])
    raw = path.read_bytes()
    package = json.loads(raw)
    assert raw == canonical_json_bytes(package)
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert hashlib.sha256(raw).hexdigest() == first["package_sha256"]
    assert package["created_at"] == "2026-09-19T01:02:03Z"
    assert package["source"]["duration_ms"] == 2200
    assert package["segments"][0]["segment_id"] == "synthetic-video-001#000000"
    assert len(list((tmp_path / "_source_packages").glob("*.json"))) == 1

    changed, changed_projection = _protected(ocr="不同画面文字")
    changed_receipt = write_source_package_v2(
        tmp_path, changed, changed_projection, now=_fixed_now
    )
    assert changed_receipt["package_id"] != first["package_id"]
    assert Path(changed_receipt["source_package_path"]).exists()


def test_committed_v2_fixture_is_canonical_and_self_consistent() -> None:
    path = Path(__file__).parent / "fixtures" / "source-package-v2.synthetic.json"
    raw = path.read_bytes()
    package = json.loads(raw)
    protected = {
        key: package[key]
        for key in (
            "schema_version",
            "package_type",
            "source",
            "processing",
            "segments",
            "supplements",
        )
    }
    protected_sha = hashlib.sha256(canonical_json_bytes(protected)[:-1]).hexdigest()
    projected = " ".join(
        (
            package["segments"][0]["cleaned_text"]
            + "\n\n"
            + package["supplements"]["screen_ocr"]
        ).split()
    )
    assert raw == canonical_json_bytes(package)
    assert package["integrity"]["protected_fields_sha256"] == protected_sha
    assert package["package_id"] == f"pkg_{protected_sha[:32]}"
    assert package["integrity"]["projection_sha256"] == hashlib.sha256(
        projected.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "不同正文"},
        {"title": "不同来源标题"},
        {"model": "base"},
        {"policy": "disabled", "ocr": ""},
    ],
)
def test_each_protected_dimension_changes_package_identity(tmp_path: Path, kwargs: dict) -> None:
    original, original_projection = _protected()
    changed, changed_projection = _protected(**kwargs)
    first = write_source_package_v2(tmp_path, original, original_projection, now=_fixed_now)
    second = write_source_package_v2(tmp_path, changed, changed_projection, now=_fixed_now)
    assert first["package_id"] != second["package_id"]
    assert Path(first["source_package_path"]).read_bytes() != Path(
        second["source_package_path"]
    ).read_bytes()


def test_v2_publish_failure_is_recoverable_without_partial_files(tmp_path: Path) -> None:
    protected, projection = _protected()

    def fail_before(stage: str) -> None:
        if stage == "before_publish":
            raise OSError("private body must not escape")

    with pytest.raises(SourcePackageError) as caught:
        write_source_package_v2(
            tmp_path, protected, projection, now=_fixed_now, fault_hook=fail_before
        )
    assert caught.value.code == "PERSISTENCE_FAILURE"
    package_root = tmp_path / "_source_packages"
    assert not list(package_root.glob("*.json"))
    assert not list(package_root.glob("*.tmp"))

    def fail_after(stage: str) -> None:
        if stage == "after_publish":
            raise OSError("response publication failed")

    with pytest.raises(SourcePackageError):
        write_source_package_v2(
            tmp_path, protected, projection, now=_fixed_now, fault_hook=fail_after
        )
    recovered = write_source_package_v2(tmp_path, protected, projection)
    assert Path(recovered["source_package_path"]).is_file()
    assert not list(package_root.glob("*.tmp"))


def test_existing_same_id_with_changed_bytes_is_collision(tmp_path: Path) -> None:
    protected, projection = _protected()
    receipt = write_source_package_v2(tmp_path, protected, projection, now=_fixed_now)
    path = Path(receipt["source_package_path"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["source"]["title"] = "被篡改"
    path.write_bytes(canonical_json_bytes(value))

    with pytest.raises(SourcePackageError) as caught:
        write_source_package_v2(tmp_path, protected, projection)
    assert caught.value.code == "PACKAGE_ID_COLLISION"


def test_v2_publish_race_returns_one_original_receipt(tmp_path: Path) -> None:
    protected, projection = _protected()
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _: write_source_package_v2(
                    tmp_path, protected, projection, now=_fixed_now
                ),
                range(32),
            )
        )
    assert all(receipt == receipts[0] for receipt in receipts)
    package_root = tmp_path / "_source_packages"
    assert len(list(package_root.glob("*.json"))) == 1
    assert not list(package_root.glob("*.tmp"))


def test_writer_rejects_projection_digest_not_derived_from_protected_fields(
    tmp_path: Path,
) -> None:
    protected, _ = _protected()
    with pytest.raises(SourcePackageError) as caught:
        write_source_package_v2(tmp_path, protected, "0" * 64, now=_fixed_now)
    assert caught.value.code == "INVALID_PACKAGE_SCHEMA"
    assert not (tmp_path / "_source_packages").exists()


def test_empty_voice_and_ocr_is_rejected() -> None:
    with pytest.raises(SourcePackageError) as caught:
        _protected(text="", ocr="")
    assert caught.value.code == "EMPTY_PROJECTED_TEXT"


@pytest.mark.parametrize(
    ("text", "ocr"),
    [("只有语音", ""), ("", "只有画面"), ("语音", "画面")],
)
def test_projection_supports_voice_ocr_and_combined(text: str, ocr: str) -> None:
    protected, projection = _protected(text=text, ocr=ocr)
    expected = " ".join((f"{text}\n\n{ocr}" if text and ocr else text or ocr).split())
    assert projection == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert protected["supplements"]["screen_ocr"] == ocr


def test_fixture_loader_rejects_unknown_fields_and_text_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    value = _fixture()
    value["unexpected"] = True
    _write_fixture(root, value)
    with pytest.raises(ValueError):
        server.load_fixture(root, "https://fixture.invalid/synthetic-video-001", "tiny")

    value = _fixture()
    value["transcript"]["text"] = "不匹配"
    _write_fixture(root, value)
    with pytest.raises(ValueError):
        server.load_fixture(root, "https://fixture.invalid/synthetic-video-001", "tiny")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["source"].pop("title"),
        lambda value: value.__setitem__("fixture_version", "2.0.0"),
        lambda value: value["source"].__setitem__("duration_seconds", "NaN"),
    ],
)
def test_fixture_loader_rejects_missing_unsupported_and_nonfinite(
    tmp_path: Path, mutate
) -> None:
    root = tmp_path / "fixtures"
    value = _fixture()
    mutate(value)
    _write_fixture(root, value)
    with pytest.raises(ValueError):
        server.load_fixture(root, "https://fixture.invalid/synthetic-video-001", "tiny")

@pytest.mark.parametrize(
    ("failure_at", "expected_code"),
    [("download", "DOWNLOAD_FAILED"), ("transcribe", "TRANSCRIPTION_FAILED")],
)
def test_real_backend_maps_external_failures_without_leaking(
    tmp_path: Path, monkeypatch, failure_at: str, expected_code: str
) -> None:
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", None)

    async def fake_download(_url, out_dir, on_progress=None):
        if failure_at == "download":
            raise RuntimeError(r"cookie=secret C:\private\body.txt")
        media = Path(out_dir) / "media.m4a"
        media.write_bytes(b"synthetic")
        return str(media), "douyin"

    def fake_transcribe(*_args, **_kwargs):
        if failure_at == "transcribe":
            raise RuntimeError("private transcript fragment")
        return server.TimestampedTranscript("有效正文", segments=[], asr={})

    monkeypatch.setattr(server, "_download_transcription_media", fake_download)
    monkeypatch.setattr(server, "capture_meta", lambda _path: {"video_id": "123", "title": "测试"})
    monkeypatch.setattr(server, "media_duration", lambda _path: 2.0)
    monkeypatch.setattr(server, "_transcribe_segments_sync", fake_transcribe)

    with pytest.raises(SourcePackageError) as caught:
        asyncio.run(
            server._process_source(
                {"stage": "queued"},
                "https://www.douyin.com/video/123",
                "tiny",
                False,
            )
        )
    assert caught.value.code == expected_code
    error = server.safe_error(caught.value.code, retryable=caught.value.retryable)
    assert "secret" not in str(error)
    assert "private" not in str(error)


def test_real_backend_rejects_source_id_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", None)

    async def fake_download(_url, out_dir, on_progress=None):
        media = Path(out_dir) / "media.m4a"
        media.write_bytes(b"synthetic")
        return str(media), "douyin"

    monkeypatch.setattr(server, "_download_transcription_media", fake_download)
    monkeypatch.setattr(server, "capture_meta", lambda _path: {"video_id": "999", "title": "错稿"})

    with pytest.raises(SourcePackageError) as caught:
        asyncio.run(
            server._process_source(
                {"stage": "queued"},
                "https://www.douyin.com/video/123",
                "tiny",
                False,
            )
        )
    assert caught.value.code == "SOURCE_ID_MISMATCH"


async def _wait_for_package(job_id: str) -> dict:
    for _ in range(100):
        result = await server.get_source_package_status("1.0.0", job_id)
        payload = result.structuredContent
        if payload["data"]["status"] in {"succeeded", "failed"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("source package job did not finish")


def test_mcp_job_uses_fixture_without_network_and_keeps_success(tmp_path: Path, monkeypatch) -> None:
    fixture_root = tmp_path / "fixtures"
    _write_fixture(fixture_root)
    output_root = tmp_path / "library"
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", fixture_root)
    monkeypatch.setenv("DOUYIN_SOURCE_PACKAGE_ROOT", str(output_root))

    async def no_network(*_args, **_kwargs):
        raise AssertionError("fixture mode attempted network")

    monkeypatch.setattr(server, "_download_transcription_media", no_network)
    monkeypatch.setattr(server, "_read_screen_text", no_network)

    async def exercise() -> tuple[dict, dict, dict, dict]:
        created = await server.create_source_package(
            "1.0.0",
            "https://fixture.invalid/synthetic-video-001",
            "tiny",
            True,
        )
        create_payload = created.structuredContent
        completed = await _wait_for_package(create_payload["data"]["job_id"])
        repeated = await server.get_source_package_status(
            "1.0.0", create_payload["data"]["job_id"]
        )
        retried = await server.create_source_package(
            "1.0.0",
            "https://fixture.invalid/synthetic-video-001",
            "tiny",
            True,
        )
        retried_completed = await _wait_for_package(
            retried.structuredContent["data"]["job_id"]
        )
        return create_payload, completed, repeated.structuredContent, retried_completed

    created, completed, repeated, retried = asyncio.run(exercise())
    assert created["data"]["status"] == "queued"
    assert completed == repeated
    assert completed["data"]["status"] == "succeeded"
    assert retried["data"]["job_id"] != completed["data"]["job_id"]
    for key in ("schema_version", "package_id", "package_sha256", "source_package_path"):
        assert retried["data"][key] == completed["data"][key]
    serialized = json.dumps(completed, ensure_ascii=False)
    assert "示例原句" not in serialized
    assert "画面文字" not in serialized
    package = json.loads(Path(completed["data"]["source_package_path"]).read_text(encoding="utf-8"))
    assert package["processing"]["ocr_policy"] == "sparse-audio-douyin-v1"
    assert package["supplements"]["screen_ocr"] == "画面文字"
    assert len(list((output_root / "_source_packages").glob("*.json"))) == 1


def test_legacy_async_tool_keeps_text_result_and_cannot_pop_package_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", None)

    async def fake_download(_url, out_dir, on_progress=None):
        media = Path(out_dir) / "media.m4a"
        media.write_bytes(b"synthetic")
        return str(media), "douyin"

    transcript = server.TimestampedTranscript(
        "旧客户端正文",
        segments=[],
        asr={},
    )
    monkeypatch.setattr(server, "_download_transcription_media", fake_download)
    monkeypatch.setattr(server, "capture_meta", lambda _path: {})
    monkeypatch.setattr(server, "media_duration", lambda _path: 2.0)
    monkeypatch.setattr(server, "_transcribe_segments_sync", lambda *_args: transcript)

    async def exercise_legacy() -> str:
        created = await server.douyin_to_text(
            "https://www.douyin.com/video/123", "tiny"
        )
        job_id = created.split("job_id=", 1)[1].split(")", 1)[0]
        for _ in range(100):
            job = server._JOBS[job_id]
            if job["status"] != "running":
                break
            await asyncio.sleep(0.01)
        return await server.get_transcript_result(job_id, 0.5)

    assert asyncio.run(exercise_legacy()) == "旧客户端正文"

    package_job_id = "package1"
    server._JOBS[package_job_id] = {
        "kind": "source_package",
        "status": "succeeded",
        "stage": "packaging",
        "result": {"private": "receipt"},
        "started": 0.0,
        "done_at": 0.0,
    }
    result = asyncio.run(server.get_transcript_result(package_job_id, 0.5))
    assert result == f"未知或已过期的 job_id: {package_job_id}"
    assert package_job_id in server._JOBS
    server._JOBS.pop(package_job_id)


def test_include_ocr_false_produces_distinct_disabled_package(tmp_path: Path, monkeypatch) -> None:
    fixture_root = tmp_path / "fixtures"
    _write_fixture(fixture_root)
    output_root = tmp_path / "library"
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", fixture_root)
    monkeypatch.setenv("DOUYIN_SOURCE_PACKAGE_ROOT", str(output_root))

    async def exercise() -> dict:
        created = await server.create_source_package(
            "1.0.0",
            "https://fixture.invalid/synthetic-video-001",
            "tiny",
            False,
        )
        return await _wait_for_package(created.structuredContent["data"]["job_id"])

    completed = asyncio.run(exercise())
    package = json.loads(Path(completed["data"]["source_package_path"]).read_text(encoding="utf-8"))
    assert package["processing"]["ocr_policy"] == "disabled"
    assert package["supplements"] == {"screen_ocr": "", "screen_candidates": []}


def test_job_result_failure_recovers_existing_package_on_retry(tmp_path: Path, monkeypatch) -> None:
    fixture_root = tmp_path / "fixtures"
    _write_fixture(fixture_root)
    output_root = tmp_path / "library"
    monkeypatch.setattr(server, "_TEST_FIXTURE_ROOT", fixture_root)
    monkeypatch.setenv("DOUYIN_SOURCE_PACKAGE_ROOT", str(output_root))

    def publish_then_fail(root, protected, projection_sha256):
        def fail(stage: str) -> None:
            if stage == "after_publish":
                raise OSError("lost task result")

        return write_source_package_v2(
            root, protected, projection_sha256, fault_hook=fail
        )

    monkeypatch.setattr(server, "write_source_package_v2", publish_then_fail)

    async def submit() -> dict:
        created = await server.create_source_package(
            "1.0.0",
            "https://fixture.invalid/synthetic-video-001",
            "tiny",
            True,
        )
        return await _wait_for_package(created.structuredContent["data"]["job_id"])

    failed = asyncio.run(submit())
    assert failed["data"]["status"] == "failed"
    assert failed["data"]["error"]["code"] == "PERSISTENCE_FAILURE"
    assert len(list((output_root / "_source_packages").glob("*.json"))) == 1

    monkeypatch.setattr(server, "write_source_package_v2", write_source_package_v2)
    recovered = asyncio.run(submit())
    assert recovered["data"]["status"] == "succeeded"
    assert len(list((output_root / "_source_packages").glob("*.json"))) == 1


def test_mcp_failures_are_strict_and_do_not_create_jobs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DOUYIN_SOURCE_PACKAGE_ROOT", raising=False)
    before = set(server._JOBS)
    missing = asyncio.run(
        server.create_source_package(
            "1.0.0", "https://fixture.invalid/synthetic-video-001"
        )
    )
    assert missing.isError is True
    assert missing.structuredContent["error"]["code"] == "SOURCE_PACKAGE_ROOT_NOT_CONFIGURED"
    assert set(server._JOBS) == before

    unsupported = asyncio.run(
        server.create_source_package(
            "9.9.9", "https://fixture.invalid/synthetic-video-001"
        )
    )
    assert unsupported.isError is True
    assert unsupported.structuredContent["error"]["code"] == "UNSUPPORTED_PROTOCOL_VERSION"
    assert set(server._JOBS) == before

    monkeypatch.setenv(
        "DOUYIN_SOURCE_PACKAGE_ROOT", str(tmp_path / "_source_packages")
    )
    nested_root = asyncio.run(
        server.create_source_package(
            "1.0.0", "https://fixture.invalid/synthetic-video-001"
        )
    )
    assert nested_root.isError is True
    assert nested_root.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
    assert set(server._JOBS) == before

    rejected = asyncio.run(
        server.mcp.call_tool(
            "create_source_package",
            {
                "protocol_version": "1.0.0",
                "url": "https://fixture.invalid/synthetic-video-001",
                "unexpected": True,
            },
        )
    )
    assert rejected.isError is True
    assert rejected.structuredContent["error"]["code"] == "INVALID_ARGUMENT"

    missing_required = asyncio.run(
        server.mcp.call_tool(
            "create_source_package",
            {"protocol_version": "1.0.0"},
        )
    )
    assert missing_required.isError is True
    assert missing_required.structuredContent["error"]["code"] == "INVALID_ARGUMENT"

    wrong_type = asyncio.run(
        server.mcp.call_tool(
            "create_source_package",
            {
                "protocol_version": "1.0.0",
                "url": "https://fixture.invalid/synthetic-video-001",
                "include_ocr": 1,
            },
        )
    )
    assert wrong_type.isError is True
    assert wrong_type.structuredContent["error"]["code"] == "INVALID_ARGUMENT"

    unknown = asyncio.run(server.get_source_package_status("1.0.0", "missing"))
    assert unknown.isError is True
    assert unknown.structuredContent["error"]["code"] == "JOB_NOT_FOUND"


def test_tool_catalog_keeps_legacy_tools_and_declares_handoff_annotations() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    legacy = {
        "analyze_douyin",
        "analyze_video",
        "download_douyin",
        "download_video",
        "douyin_to_text",
        "video_to_text",
        "get_transcript_result",
        "transcribe_video",
    }
    assert legacy <= set(tools)
    create = tools["create_source_package"]
    status = tools["get_source_package_status"]
    assert create.inputSchema["additionalProperties"] is False
    assert status.inputSchema["additionalProperties"] is False
    assert create.annotations is not None
    assert (
        create.annotations.readOnlyHint,
        create.annotations.destructiveHint,
        create.annotations.idempotentHint,
        create.annotations.openWorldHint,
    ) == (False, False, False, True)
    assert status.annotations is not None
    assert (
        status.annotations.readOnlyHint,
        status.annotations.destructiveHint,
        status.annotations.idempotentHint,
        status.annotations.openWorldHint,
    ) == (True, False, True, False)


def test_stdio_fixture_process_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    fixture_root = tmp_path / "fixtures"
    output_root = tmp_path / "library"
    _write_fixture(fixture_root)

    async def exercise() -> tuple[list[str], dict, dict, list[dict], str]:
        env = os.environ.copy()
        env["DOUYIN_SOURCE_PACKAGE_ROOT"] = str(output_root)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(project_root / "server.py"),
                "--test-fixture-root",
                str(fixture_root),
            ],
            cwd=project_root,
            env=env,
        )
        err_path = tmp_path / "server.stderr"
        with err_path.open("w+", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    failures = []
                    for name, arguments, expected_code in (
                        ("get_source_package_status", {"protocol_version": "1.0.0", "job_id": "missing"}, "JOB_NOT_FOUND"),
                        ("get_source_package_status", {"protocol_version": "0.9.0", "job_id": "missing"}, "UNSUPPORTED_PROTOCOL_VERSION"),
                        ("create_source_package", {"protocol_version": "1.0.0", "url": "https://fixture.invalid/synthetic-video-001", "model_size": "invalid"}, "INVALID_ARGUMENT"),
                    ):
                        failure = await session.call_tool(name, arguments)
                        assert failure.isError is True
                        assert failure.structuredContent["ok"] is False
                        assert failure.structuredContent["error"]["code"] == expected_code
                        failures.append(failure.structuredContent)
                    created = await session.call_tool(
                        "create_source_package",
                        {
                            "protocol_version": "1.0.0",
                            "url": "https://fixture.invalid/synthetic-video-001",
                            "model_size": "tiny",
                            "include_ocr": True,
                        },
                    )
                    job_id = created.structuredContent["data"]["job_id"]
                    completed = None
                    for _ in range(100):
                        status = await session.call_tool(
                            "get_source_package_status",
                            {"protocol_version": "1.0.0", "job_id": job_id},
                        )
                        if status.structuredContent["data"]["status"] in {
                            "succeeded",
                            "failed",
                        }:
                            completed = status.structuredContent
                            break
                        await asyncio.sleep(0.02)
                    rejected = await session.call_tool(
                        "get_source_package_status",
                        {
                            "protocol_version": "1.0.0",
                            "job_id": job_id,
                            "unexpected": True,
                        },
                    )
            errlog.seek(0)
            stderr = errlog.read()
        assert completed is not None
        return [tool.name for tool in tools.tools], completed, rejected.structuredContent, failures, stderr

    names, completed, rejected, failures, stderr = asyncio.run(exercise())
    assert "create_source_package" in names
    assert "get_source_package_status" in names
    assert completed["data"]["status"] == "succeeded"
    assert rejected["error"]["code"] == "INVALID_ARGUMENT"
    assert len(failures) == 3
    assert "[test mode] fixture backend enabled" in stderr
    assert "Traceback" not in stderr
    assert len(list((output_root / "_source_packages").glob("*.json"))) == 1
