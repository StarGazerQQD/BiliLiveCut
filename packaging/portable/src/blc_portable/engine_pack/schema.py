"""Engine Pack unified schema."""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 4


@dataclass
class ContentManifest:
    """Internal manifest — describes archive contents, not archive hashes."""

    schema_version: int = SCHEMA_VERSION
    engine_pack_version: str = ""
    compatible_app: dict = field(default_factory=dict)
    source_commit: str = ""
    builder_commit: str = ""
    model_lock_sha256: str = ""
    artifact_class: str = "production"
    engines: list[dict] = field(default_factory=list)
    files: dict = field(default_factory=dict)
    total_files: int = 0
    total_uncompressed_size: int = 0

    def to_dict(self) -> dict:
        """Convert content manifest to dict."""
        return {
            "schema_version": self.schema_version,
            "engine_pack_version": self.engine_pack_version,
            "compatible_app": self.compatible_app,
            "source_commit": self.source_commit,
            "builder_commit": self.builder_commit,
            "model_lock_sha256": self.model_lock_sha256,
            "artifact_class": self.artifact_class,
            "engines": self.engines,
            "files": self.files,
            "total_files": self.total_files,
            "total_uncompressed_size": self.total_uncompressed_size,
        }


@dataclass
class ExternalMetadata:
    """External metadata — engine_pack_info.json."""

    format_version: int = SCHEMA_VERSION
    engine_pack_version: str = ""
    filename: str = ""
    size_bytes: int = 0
    crc32: str = ""
    sha256: str = ""
    content_manifest_sha256: str = ""
    model_lock_sha256: str = ""
    artifact_class: str = "production"
    source_commit: str = ""
    builder_commit: str = ""
    build_timestamp: str = ""
    expected_engine_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert external metadata to dict."""
        return {
            "format_version": self.format_version,
            "engine_pack_version": self.engine_pack_version,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "crc32": self.crc32,
            "sha256": self.sha256,
            "content_manifest_sha256": self.content_manifest_sha256,
            "model_lock_sha256": self.model_lock_sha256,
            "artifact_class": self.artifact_class,
            "source_commit": self.source_commit,
            "builder_commit": self.builder_commit,
            "build_timestamp": self.build_timestamp,
            "expected_engine_ids": self.expected_engine_ids,
        }

    def validate(self) -> list[str]:
        """Validate external metadata completeness."""
        errors = []
        if not self.crc32 or len(self.crc32) != 8:
            errors.append("CRC32 empty or invalid")
        if not self.sha256 or len(self.sha256) != 64:
            errors.append("SHA-256 empty or invalid")
        if not self.content_manifest_sha256 or len(self.content_manifest_sha256) != 64:
            errors.append("content_manifest_sha256 empty or invalid")
        if not self.size_bytes:
            errors.append("size_bytes is zero")
        if not self.expected_engine_ids:
            errors.append("expected_engine_ids empty")
        return errors
