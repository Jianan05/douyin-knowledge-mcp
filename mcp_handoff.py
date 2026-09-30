"""T19 MCP 信封、严格 FastMCP 适配器与离线 fixture 模式。"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROTOCOL_VERSION = "1.0.0"
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?")
_FIXTURE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def _is_negative_zero(value: str) -> bool:
    """只拒绝 -0、-0.0 等负零，不拒绝 -0.125。"""

    try:
        return value.startswith("-") and Decimal(value) == 0
    except InvalidOperation:
        return False

_SAFE_MESSAGES = {
    "DOWNLOAD_FAILED": "媒体下载失败，可安全重试。",
    "EMPTY_PROJECTED_TEXT": "来源包投影正文为空。",
    "INTERNAL_ERROR": "内部处理失败。",
    "INVALID_ARGUMENT": "工具参数不合法。",
    "INVALID_FIXTURE": "测试夹具不符合约定。",
    "INVALID_PACKAGE_SCHEMA": "来源包字段不符合模式。",
    "JOB_NOT_FOUND": "任务不存在或已过期。",
    "PACKAGE_ID_COLLISION": "来源包身份发生碰撞。",
    "PERSISTENCE_FAILURE": "来源包持久化失败，可安全重试。",
    "SOURCE_ID_MISMATCH": "来源身份与请求不匹配。",
    "SOURCE_PACKAGE_ROOT_NOT_CONFIGURED": "未配置来源包生成根。",
    "TRANSCRIPTION_FAILED": "语音转录失败，可安全重试。",
    "UNSUPPORTED_PROTOCOL_VERSION": "不支持该协议版本。",
}


class StrictModel(BaseModel):
    """严格拒绝未知字段和隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ErrorData(StrictModel):
    code: str
    message: str
    retryable: bool


class FailureEnvelope(StrictModel):
    protocol_version: Literal["1.0.0"] = PROTOCOL_VERSION
    ok: Literal[False] = False
    error: ErrorData


class CreateData(StrictModel):
    job_id: str
    status: Literal["queued"] = "queued"


class CreateEnvelope(StrictModel):
    protocol_version: Literal["1.0.0"] = PROTOCOL_VERSION
    ok: Literal[True] = True
    data: CreateData


class RunningData(StrictModel):
    job_id: str
    status: Literal["queued", "running"]
    stage: Literal[
        "queued",
        "extracting_url",
        "downloading",
        "transcribing",
        "ocr",
        "packaging",
    ]


class PackageData(StrictModel):
    job_id: str
    status: Literal["succeeded"] = "succeeded"
    schema_version: Literal["2.0.0"] = "2.0.0"
    package_id: str
    package_sha256: str
    source_package_path: str


class FailedJobData(StrictModel):
    job_id: str
    status: Literal["failed"] = "failed"
    error: ErrorData


class StatusEnvelope(StrictModel):
    protocol_version: Literal["1.0.0"] = PROTOCOL_VERSION
    ok: Literal[True] = True
    data: RunningData | PackageData | FailedJobData


class FixtureSource(StrictModel):
    platform: Literal["douyin", "bilibili"]
    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tags: list[str]
    duration_seconds: str

    @field_validator("duration_seconds")
    @classmethod
    def validate_decimal(cls, value: str) -> str:
        if (
            not _DECIMAL_RE.fullmatch(value)
            or value.startswith("-")
            or (value.startswith("0") and value != "0" and not value.startswith("0."))
        ):
            raise ValueError("invalid decimal")
        return value


class FixtureSegment(StrictModel):
    start_seconds: str
    end_seconds: str
    raw_text: str
    cleaned_text: str
    cleaning: list[str]
    avg_logprob: str | None
    no_speech_prob: str | None
    compression_ratio: str | None
    temperature: str | None

    @field_validator(
        "start_seconds",
        "end_seconds",
        "avg_logprob",
        "no_speech_prob",
        "compression_ratio",
        "temperature",
    )
    @classmethod
    def validate_decimal(cls, value: str | None) -> str | None:
        if value is not None and (
            not _DECIMAL_RE.fullmatch(value) or _is_negative_zero(value)
        ):
            raise ValueError("invalid decimal")
        return value


class FixtureAsr(StrictModel):
    model: str = Field(min_length=1)
    language: str = Field(min_length=1)
    vad_filter: bool
    device: str = Field(min_length=1)
    compute_type: str = Field(min_length=1)
    initial_prompt_enabled: bool
    initial_prompt_sha256: str | None
    condition_on_previous_text: bool
    beam_size: int = Field(ge=1)
    vad_threshold: str | None
    min_silence_duration_ms: int | None
    language_probability: str | None
    detected_duration_seconds: str

    @field_validator("vad_threshold", "language_probability", "detected_duration_seconds")
    @classmethod
    def validate_decimal(cls, value: str | None) -> str | None:
        if value is not None and (
            not _DECIMAL_RE.fullmatch(value) or _is_negative_zero(value)
        ):
            raise ValueError("invalid decimal")
        return value


class FixtureTranscript(StrictModel):
    text: str
    segments: list[FixtureSegment]
    asr: FixtureAsr

    @model_validator(mode="after")
    def text_matches_segments(self) -> "FixtureTranscript":
        expected = "\n".join(
            segment.cleaned_text for segment in self.segments if segment.cleaned_text
        )
        if self.text != expected:
            raise ValueError("transcript text mismatch")
        return self


class FixtureCandidate(StrictModel):
    text: str = Field(min_length=1)
    frame_count: int = Field(ge=1)


class FixtureOcr(StrictModel):
    screen_ocr: str
    screen_candidates: list[FixtureCandidate]


class ProcessingFixture(StrictModel):
    fixture_version: Literal["1.0.0"]
    source: FixtureSource
    transcript: FixtureTranscript
    ocr: FixtureOcr


def _json_no_constants(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_fixture(root: Path, url: str, model_size: str) -> ProcessingFixture:
    """从已解析测试根严格读取单个人工预处理夹具。"""

    parsed = urlparse(url)
    fixture_id = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "fixture.invalid"
        or parsed.query
        or parsed.fragment
        or not _FIXTURE_ID_RE.fullmatch(fixture_id)
    ):
        raise ValueError("invalid fixture URL")
    resolved_root = root.resolve(strict=True)
    path = (resolved_root / f"{fixture_id}.json").resolve(strict=True)
    try:
        path.relative_to(resolved_root)
    except ValueError:
        raise ValueError("fixture outside root") from None
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("fixture BOM")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs_without_duplicates,
        parse_constant=_json_no_constants,
    )
    fixture = ProcessingFixture.model_validate(value)
    if fixture.source.url != url or fixture.source.source_id != fixture_id:
        raise ValueError("fixture source mismatch")
    if fixture.transcript.asr.model != model_size:
        raise ValueError("fixture model mismatch")
    return fixture


def safe_error(code: str, *, retryable: bool = False) -> ErrorData:
    """只按固定代码生成安全错误。"""

    safe_code = code if code in _SAFE_MESSAGES else "INTERNAL_ERROR"
    return ErrorData(
        code=safe_code,
        message=_SAFE_MESSAGES[safe_code],
        retryable=retryable if safe_code != "INTERNAL_ERROR" else False,
    )


def _result(payload: dict, *, is_error: bool) -> CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=is_error,
    )


def success_result(envelope: StrictModel) -> CallToolResult:
    return _result(envelope.model_dump(mode="json"), is_error=False)


def failure_result(code: str, *, retryable: bool = False) -> CallToolResult:
    envelope = FailureEnvelope(error=safe_error(code, retryable=retryable))
    return _result(envelope.model_dump(mode="json"), is_error=True)


class StrictFastMCP(FastMCP):
    """在 FastMCP 转换前执行公开 schema，并统一未处理异常。"""

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        result: list[Tool] = []
        for tool in tools:
            input_schema = dict(tool.inputSchema)
            input_schema["additionalProperties"] = False
            output_schema = self._with_failure_schema(tool.outputSchema)
            result.append(
                tool.model_copy(
                    update={"inputSchema": input_schema, "outputSchema": output_schema}
                )
            )
        return result

    @staticmethod
    def _with_failure_schema(output_schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if output_schema is None:
            return None
        success = deepcopy(output_schema)
        failure = FailureEnvelope.model_json_schema()
        definitions = success.pop("$defs", {})
        for name, definition in failure.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise RuntimeError("MCP output schema definition conflict")
            definitions[name] = definition
        combined: dict[str, Any] = {"anyOf": [success, failure]}
        if definitions:
            combined["$defs"] = definitions
        return combined

    @staticmethod
    def _arguments_are_valid(schema: dict[str, Any], arguments: object) -> bool:
        """校验本服务器工具使用的顶层标量参数，不引入额外运行依赖。"""

        if not isinstance(arguments, dict):
            return False
        properties = schema.get("properties", {})
        required = set(schema.get("required", ()))
        if not isinstance(properties, dict) or required - set(arguments):
            return False
        if set(arguments) - set(properties):
            return False
        type_checks = {
            "string": lambda value: isinstance(value, str),
            "boolean": lambda value: isinstance(value, bool),
            "number": lambda value: isinstance(value, (int, float))
            and not isinstance(value, bool),
            "integer": lambda value: isinstance(value, int)
            and not isinstance(value, bool),
        }
        for name, value in arguments.items():
            definition = properties.get(name, {})
            expected = definition.get("type")
            check = type_checks.get(expected)
            if check is not None and not check(value):
                return False
            if "const" in definition and value != definition["const"]:
                return False
            if "enum" in definition and value not in definition["enum"]:
                return False
        return True

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tools = {tool.name: tool for tool in await self.list_tools()}
        tool = tools.get(name)
        if tool is None:
            return failure_result("INVALID_ARGUMENT")
        if not self._arguments_are_valid(tool.inputSchema, arguments):
            return failure_result("INVALID_ARGUMENT")
        try:
            if name in {"create_source_package", "get_source_package_status"}:
                # 两个交接工具自行返回已验证的成功或失败信封；SDK 的成功
                # 模型转换会错误拒绝合法失败分支。旧工具仍用原有转换。
                return await self._tool_manager.call_tool(
                    name, arguments, context=None, convert_result=False
                )
            return await super().call_tool(name, arguments)
        except Exception:
            return failure_result("INTERNAL_ERROR")


CreateResult = Annotated[CallToolResult, CreateEnvelope]
StatusResult = Annotated[CallToolResult, StatusEnvelope]
