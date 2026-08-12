"""Engine Pack 外部元数据的当前且唯一 schema。"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCHEMA_VERSION = 5
_FIELDS = {
    "format_version",
    "artifact_class",
    "engine_pack_version",
    "portable_release_version",
    "engine_pack_api_version",
    "model_set_version",
    "filename",
    "size_bytes",
    "crc32",
    "sha256",
    "content_manifest_sha256",
    "model_lock_sha256",
    "source_commit",
    "builder_commit",
    "build_timestamp",
    "expected_engine_ids",
}
_EXPECTED_ENGINE_IDS = ["whisper", "paraformer", "sensevoice", "funasr_nano"]


@dataclass(frozen=True, slots=True)
class ExternalMetadata:
    """严格解析后的 ``engine_pack_info.json``。"""

    format_version: int
    artifact_class: str
    engine_pack_version: str
    portable_release_version: str
    engine_pack_api_version: int
    model_set_version: int
    filename: str
    size_bytes: int
    crc32: str
    sha256: str
    content_manifest_sha256: str
    model_lock_sha256: str
    source_commit: str
    builder_commit: str
    build_timestamp: str
    expected_engine_ids: list[str]

    @classmethod
    def from_dict(cls, raw: object) -> ExternalMetadata:
        """只接受字段、类型和 schema 都精确匹配的当前元数据。"""
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise ValueError("engine_pack_info.json 字段不符合当前格式")
        for field_name in (
            "artifact_class",
            "engine_pack_version",
            "portable_release_version",
            "filename",
            "crc32",
            "sha256",
            "content_manifest_sha256",
            "model_lock_sha256",
            "source_commit",
            "builder_commit",
            "build_timestamp",
        ):
            if not isinstance(raw[field_name], str):
                raise ValueError(f"engine_pack_info.json {field_name} 必须是字符串")
        for field_name in ("format_version", "engine_pack_api_version", "model_set_version", "size_bytes"):
            if not isinstance(raw[field_name], int) or isinstance(raw[field_name], bool):
                raise ValueError(f"engine_pack_info.json {field_name} 必须是整数")
        engine_ids = raw["expected_engine_ids"]
        if not isinstance(engine_ids, list) or any(not isinstance(item, str) for item in engine_ids):
            raise ValueError("engine_pack_info.json expected_engine_ids 必须是字符串数组")
        return cls(
            format_version=raw["format_version"],
            artifact_class=raw["artifact_class"],
            engine_pack_version=raw["engine_pack_version"],
            portable_release_version=raw["portable_release_version"],
            engine_pack_api_version=raw["engine_pack_api_version"],
            model_set_version=raw["model_set_version"],
            filename=raw["filename"],
            size_bytes=raw["size_bytes"],
            crc32=raw["crc32"],
            sha256=raw["sha256"],
            content_manifest_sha256=raw["content_manifest_sha256"],
            model_lock_sha256=raw["model_lock_sha256"],
            source_commit=raw["source_commit"],
            builder_commit=raw["builder_commit"],
            build_timestamp=raw["build_timestamp"],
            expected_engine_ids=list(engine_ids),
        )

    def to_dict(self) -> dict[str, object]:
        """序列化当前外部元数据。"""
        return {
            "format_version": self.format_version,
            "artifact_class": self.artifact_class,
            "engine_pack_version": self.engine_pack_version,
            "portable_release_version": self.portable_release_version,
            "engine_pack_api_version": self.engine_pack_api_version,
            "model_set_version": self.model_set_version,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "crc32": self.crc32,
            "sha256": self.sha256,
            "content_manifest_sha256": self.content_manifest_sha256,
            "model_lock_sha256": self.model_lock_sha256,
            "source_commit": self.source_commit,
            "builder_commit": self.builder_commit,
            "build_timestamp": self.build_timestamp,
            "expected_engine_ids": self.expected_engine_ids,
        }

    def validate(self) -> list[str]:
        """验证当前外部元数据的值域。"""
        errors: list[str] = []
        if self.format_version != SCHEMA_VERSION:
            errors.append(f"format_version invalid: {self.format_version}")
        if self.engine_pack_api_version != SCHEMA_VERSION or self.model_set_version != SCHEMA_VERSION:
            errors.append("engine_pack_api_version/model_set_version must match current schema")
        if self.artifact_class not in {"production", "fixture"}:
            errors.append("artifact_class must be production or fixture")
        if not self.engine_pack_version or self.portable_release_version != self.engine_pack_version:
            errors.append("portable_release_version must equal engine_pack_version")
        if self.filename != f"BiliLiveCut-EnginePack-{self.engine_pack_version}.zip":
            errors.append("filename does not match engine_pack_version")
        if self.size_bytes <= 0:
            errors.append("size_bytes must be positive")
        if re.fullmatch(r"[0-9A-F]{8}", self.crc32) is None:
            errors.append("CRC32 invalid")
        for field_name in ("sha256", "content_manifest_sha256", "model_lock_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, field_name)) is None:
                errors.append(f"{field_name} invalid")
        for field_name in ("source_commit", "builder_commit"):
            if re.fullmatch(r"[0-9a-f]{40}", getattr(self, field_name)) is None:
                errors.append(f"{field_name} invalid")
        if not self.build_timestamp:
            errors.append("build_timestamp empty")
        if self.expected_engine_ids != _EXPECTED_ENGINE_IDS:
            errors.append("expected_engine_ids does not match current engine set")
        return errors
