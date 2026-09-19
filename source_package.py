"""T19 v2 来源包的规范化、摘要与不覆盖原子发布。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


SCHEMA_VERSION = "2.0.0"
PACKAGE_TYPE = "timestamped_clean_transcript"
PIPELINE_VERSION = "douyin-transcribe/1"
CLEANING_VERSION = "clean-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class SourcePackageError(Exception):
    """只携带稳定错误代码，不把正文或原始异常带到 MCP。"""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def canonical_json_bytes(value: object) -> bytes:
    """编码为契约规定的规范 JSON，并在末尾保留单个 LF。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_decimal(value: object, *, nullable: bool = True) -> str | None:
    """把有限数值变成无指数、无尾零、无负零的十进制字符串。"""

    if value is None:
        if nullable:
            return None
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA") from None
    if not number.is_finite():
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _milliseconds(value: object) -> int:
    """按 ROUND_HALF_UP 把秒转换为非负毫秒。"""

    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA") from None
    if not seconds.is_finite() or seconds < 0:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _nonempty(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    return text


def _https_url(value: object) -> str:
    text = _nonempty(value)
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    return text


def _unique_strings(values: object, *, sort: bool = False) -> list[str]:
    if not isinstance(values, list):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    result: list[str] = []
    for value in values:
        text = str(value)
        if text not in result:
            result.append(text)
    return sorted(result) if sort else result


def _normalize_text(text: str) -> str:
    """与 vidrag.rag.chunker.normalize_text 保持同态。"""

    return " ".join(text.split())


def _projection(segments: list[dict], screen_ocr: str) -> tuple[str, str]:
    speech = "\n".join(
        text
        for row in segments
        if (text := str(row["cleaned_text"]).strip())
    )
    ocr = screen_ocr.strip()
    combined = speech
    if ocr:
        combined = f"{speech}\n\n{ocr}" if speech else ocr
    normalized = _normalize_text(combined)
    if not normalized:
        raise SourcePackageError("EMPTY_PROJECTED_TEXT")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return normalized, digest


def _normalize_segments(source_id: str, rows: object) -> list[dict]:
    if not isinstance(rows, list):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    result: list[dict] = []
    previous_start = 0
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
        start = _milliseconds(raw.get("start_seconds", raw.get("start")))
        end = _milliseconds(raw.get("end_seconds", raw.get("end")))
        if start > end or (index and start < previous_start):
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
        previous_start = start
        result.append(
            {
                "segment_id": f"{source_id}#{index:06d}",
                "start_ms": start,
                "end_ms": end,
                "raw_text": str(raw.get("raw_text") or ""),
                "cleaned_text": str(raw.get("cleaned_text") or ""),
                "cleaning": _unique_strings(raw.get("cleaning") or []),
                "avg_logprob": _canonical_decimal(raw.get("avg_logprob")),
                "no_speech_prob": _canonical_decimal(raw.get("no_speech_prob")),
                "compression_ratio": _canonical_decimal(raw.get("compression_ratio")),
                "temperature": _canonical_decimal(raw.get("temperature")),
            }
        )
    return result


def _normalize_asr(asr: object) -> dict:
    if not isinstance(asr, dict):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    vad_filter = asr.get("vad_filter")
    prompt_enabled = asr.get("initial_prompt_enabled", asr.get("initial_prompt"))
    condition = asr.get("condition_on_previous_text")
    if not all(isinstance(value, bool) for value in (vad_filter, prompt_enabled, condition)):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    prompt_hash = asr.get("initial_prompt_sha256")
    if prompt_enabled:
        if not isinstance(prompt_hash, str) or not _HEX64.fullmatch(prompt_hash):
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    elif prompt_hash is not None:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    vad_threshold = asr.get("vad_threshold")
    min_silence = asr.get("min_silence_duration_ms")
    if vad_filter:
        vad_threshold = _canonical_decimal(vad_threshold, nullable=False)
        if not isinstance(min_silence, int) or isinstance(min_silence, bool) or min_silence < 0:
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    elif vad_threshold is not None or min_silence is not None:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    beam_size = asr.get("beam_size")
    if not isinstance(beam_size, int) or isinstance(beam_size, bool) or beam_size < 1:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    detected = asr.get("detected_duration_seconds", asr.get("duration"))
    return {
        "model": _nonempty(asr.get("model")),
        "language": _nonempty(asr.get("language")),
        "vad_filter": vad_filter,
        "device": _nonempty(asr.get("device")),
        "compute_type": _nonempty(asr.get("compute_type")),
        "initial_prompt_enabled": prompt_enabled,
        "initial_prompt_sha256": prompt_hash,
        "condition_on_previous_text": condition,
        "beam_size": beam_size,
        "vad_threshold": vad_threshold,
        "min_silence_duration_ms": min_silence,
        "language_probability": _canonical_decimal(asr.get("language_probability")),
        "detected_duration_ms": _milliseconds(detected),
    }


def _normalize_candidates(values: object) -> list[dict]:
    if not isinstance(values, list):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    result: list[dict] = []
    for value in values:
        if isinstance(value, dict):
            text = _nonempty(value.get("text"))
            count = value.get("frame_count")
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            text = _nonempty(value[0])
            count = value[1]
        else:
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
        result.append({"text": text, "frame_count": count})
    return sorted(result, key=lambda item: (-item["frame_count"], item["text"]))


def build_protected_fields(
    *,
    source: dict,
    segments: list[dict],
    asr: dict,
    screen_ocr: str,
    screen_candidates: list,
    ocr_policy: str,
) -> tuple[dict, str]:
    """规范化处理结果并返回受保护字段与投影摘要。"""

    if not isinstance(source, dict):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    platform = _nonempty(source.get("platform"))
    if platform not in {"douyin", "bilibili"}:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    source_id = _nonempty(source.get("source_id"))
    normalized_segments = _normalize_segments(source_id, segments)
    supplements = {
        "screen_ocr": str(screen_ocr or ""),
        "screen_candidates": _normalize_candidates(screen_candidates),
    }
    _, projection_sha256 = _projection(normalized_segments, supplements["screen_ocr"])
    protected = {
        "schema_version": SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "source": {
            "platform": platform,
            "source_id": source_id,
            "url": _https_url(source.get("url")),
            "title": _nonempty(source.get("title")),
            "tags": _unique_strings(source.get("tags") or [], sort=True),
            "duration_ms": _milliseconds(
                source.get("duration_seconds", source.get("duration"))
            ),
        },
        "processing": {
            "pipeline_version": PIPELINE_VERSION,
            "cleaning_version": CLEANING_VERSION,
            "ocr_policy": _nonempty(ocr_policy),
            "asr": _normalize_asr(asr),
        },
        "segments": normalized_segments,
        "supplements": supplements,
    }
    return protected, projection_sha256


def _protected_digest(protected: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(protected)[:-1]).hexdigest()


def assemble_package(protected: dict, projection_sha256: str, created_at: str) -> dict:
    """由受保护字段组装完整 v2 包。"""

    protected_sha256 = _protected_digest(protected)
    package_id = f"pkg_{protected_sha256[:32]}"
    return {
        **protected,
        "package_id": package_id,
        "created_at": created_at,
        "integrity": {
            "projection_sha256": projection_sha256,
            "protected_fields_sha256": protected_sha256,
        },
    }


def _safe_source_id(source_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._")
    if not safe:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    return safe[:100]


def _validate_existing(path: Path, expected_protected_sha256: str) -> dict:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        raise SourcePackageError("PACKAGE_ID_COLLISION") from None
    if not isinstance(value, dict):
        raise SourcePackageError("PACKAGE_ID_COLLISION")
    if set(value) != {
        "schema_version",
        "package_type",
        "package_id",
        "created_at",
        "source",
        "processing",
        "segments",
        "supplements",
        "integrity",
    }:
        raise SourcePackageError("PACKAGE_ID_COLLISION")
    protected = {
        key: value[key]
        for key in (
            "schema_version",
            "package_type",
            "source",
            "processing",
            "segments",
            "supplements",
        )
    }
    actual_protected_sha256 = _protected_digest(protected)
    integrity = value.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {
        "projection_sha256",
        "protected_fields_sha256",
    }:
        raise SourcePackageError("PACKAGE_ID_COLLISION")
    try:
        _, actual_projection_sha256 = _projection(
            protected["segments"], protected["supplements"]["screen_ocr"]
        )
    except Exception:
        raise SourcePackageError("PACKAGE_ID_COLLISION") from None
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at
    ):
        raise SourcePackageError("PACKAGE_ID_COLLISION")
    if (
        actual_protected_sha256 != expected_protected_sha256
        or integrity.get("protected_fields_sha256") != actual_protected_sha256
        or integrity.get("projection_sha256") != actual_projection_sha256
        or value.get("package_id") != f"pkg_{actual_protected_sha256[:32]}"
        or canonical_json_bytes(value) != raw
    ):
        raise SourcePackageError("PACKAGE_ID_COLLISION")
    return {
        "schema_version": SCHEMA_VERSION,
        "package_id": value["package_id"],
        "package_sha256": hashlib.sha256(raw).hexdigest(),
        "source_package_path": str(path.resolve()),
        "created_at": created_at,
    }


def write_source_package_v2(
    root: Path,
    protected: dict,
    projection_sha256: str,
    *,
    now: Callable[[], datetime] | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> dict:
    """将 v2 包 fsync 后以不覆盖方式发布，并返回稳定回执。"""

    try:
        _, expected_projection_sha256 = _projection(
            protected["segments"], protected["supplements"]["screen_ocr"]
        )
    except (KeyError, TypeError):
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA") from None
    if projection_sha256 != expected_projection_sha256:
        raise SourcePackageError("INVALID_PACKAGE_SCHEMA")
    protected_sha256 = _protected_digest(protected)
    package_id = f"pkg_{protected_sha256[:32]}"
    target_dir = Path(root) / "_source_packages"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_id = protected["source"]["source_id"]
    target = target_dir / f"{_safe_source_id(source_id)}--{package_id}.json"
    if target.exists():
        return _validate_existing(target, protected_sha256)

    clock = now or (lambda: datetime.now(timezone.utc))
    created_at = clock().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    package = assemble_package(protected, projection_sha256, created_at)
    raw = canonical_json_bytes(package)
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=target_dir,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if fault_hook:
            fault_hook("before_publish")
        try:
            os.link(temp_path, target)
        except FileExistsError:
            return _validate_existing(target, protected_sha256)
        try:
            directory_fd = os.open(target_dir, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
        if fault_hook:
            fault_hook("after_publish")
    except SourcePackageError:
        raise
    except Exception:
        raise SourcePackageError("PERSISTENCE_FAILURE", retryable=True) from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    return {
        "schema_version": SCHEMA_VERSION,
        "package_id": package_id,
        "package_sha256": hashlib.sha256(raw).hexdigest(),
        "source_package_path": str(target.resolve()),
        "created_at": created_at,
    }
