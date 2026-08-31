"""Engine Pack 当前内容清单的数据结构与严格校验。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "config")
if _CONFIG_DIR not in sys.path:
    sys.path.insert(0, _CONFIG_DIR)

from model_catalog import get_all_engine_ids, load_engines
from version_loader import (
    get_engine_pack_version,
    get_engine_pack_zip_name,
    get_source_commit_full,
    get_source_commit_short,
    get_version,
)

ENGINE_PACK_VERSION = get_engine_pack_version()
RELEASE_VERSION = get_version()
SOURCE_COMMIT_SHORT = get_source_commit_short()
SOURCE_COMMIT_FULL = get_source_commit_full()
MANIFEST_FORMAT_VERSION = 5
ARCHIVE_FILENAME = get_engine_pack_zip_name()

MODELSCOPE_MIRRORS = ["https://www.modelscope.cn"]
HF_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
_LICENSE_FIELDS = {
    "name",
    "spdx",
    "source",
    "evidence_url",
    "license_file",
    "verified_at",
    "redistribution_verified",
}
_SUB_MODEL_FIELDS = {"model_id", "hub", "revision", "target_subdir", "license"}
_COMPONENT_FIELDS = {"component_id", "display_name", "repository", "revision", "target_subdir", "license"}


def _strict_license(raw: object, owner: str) -> dict[str, object]:
    """校验并复制当前许可证结构。"""
    if not isinstance(raw, dict) or set(raw) != _LICENSE_FIELDS:
        raise ValueError(f"{owner}.license 字段不符合当前格式")
    for field_name in _LICENSE_FIELDS - {"redistribution_verified"}:
        if not isinstance(raw[field_name], str) or not raw[field_name]:
            raise ValueError(f"{owner}.license.{field_name} 必须是非空字符串")
    if not isinstance(raw["redistribution_verified"], bool):
        raise ValueError(f"{owner}.license.redistribution_verified 必须是布尔值")
    return dict(raw)


def _strict_sub_models(raw: object, owner: str) -> list[dict[str, object]]:
    """校验并复制当前子模型数组。"""
    if not isinstance(raw, list):
        raise ValueError(f"{owner}.sub_models 必须是数组")
    result: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        item_owner = f"{owner}.sub_models[{index}]"
        if not isinstance(item, dict) or set(item) != _SUB_MODEL_FIELDS:
            raise ValueError(f"{item_owner} 字段不符合当前格式")
        for field_name in ("model_id", "hub", "target_subdir"):
            if not isinstance(item[field_name], str) or not item[field_name]:
                raise ValueError(f"{item_owner}.{field_name} 必须是非空字符串")
        if item["revision"] is not None and (not isinstance(item["revision"], str) or not item["revision"]):
            raise ValueError(f"{item_owner}.revision 必须是非空字符串或 null")
        copied = dict(item)
        copied["license"] = _strict_license(item["license"], item_owner)
        result.append(copied)
    return result


def _strict_components(raw: object, owner: str) -> list[dict[str, object]]:
    """校验并复制当前随附组件数组。"""
    if not isinstance(raw, list):
        raise ValueError(f"{owner}.third_party_components 必须是数组")
    result: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        item_owner = f"{owner}.third_party_components[{index}]"
        if not isinstance(item, dict) or set(item) != _COMPONENT_FIELDS:
            raise ValueError(f"{item_owner} 字段不符合当前格式")
        for field_name in _COMPONENT_FIELDS - {"license"}:
            if not isinstance(item[field_name], str) or not item[field_name]:
                raise ValueError(f"{item_owner}.{field_name} 必须是非空字符串")
        copied = dict(item)
        copied["license"] = _strict_license(item["license"], item_owner)
        result.append(copied)
    return result


def _get_engines_for_manifest() -> list[dict[str, object]]:
    """从模型锁生成当前 Engine Pack 的引擎定义。"""
    result: list[dict[str, object]] = []
    for engine in load_engines():
        item: dict[str, object] = {
            "engine_id": engine.engine_id,
            "engine_name": engine.display_name,
            "model_id": engine.repo_id if engine.hub == "huggingface" else engine.repository,
            "hub": engine.hub,
            "revision": engine.resolved_revision or None,
            "target_path": engine.target_path,
            "model_repo": engine.repository if engine.hub == "huggingface" else None,
            "sub_models": [
                {
                    "model_id": sub.repository,
                    "hub": sub.hub,
                    "revision": sub.resolved_revision or None,
                    "target_subdir": sub.target_subdir,
                    "license": {
                        "name": sub.license.name,
                        "spdx": sub.license.spdx,
                        "source": sub.license.source,
                        "evidence_url": sub.license.evidence_url,
                        "license_file": sub.license.license_file,
                        "verified_at": sub.license.verified_at,
                        "redistribution_verified": sub.license.redistribution_verified,
                    },
                }
                for sub in engine.sub_models
            ],
            "license": {
                "name": engine.license.name,
                "spdx": engine.license.spdx,
                "source": engine.license.source,
                "evidence_url": engine.license.evidence_url,
                "license_file": engine.license.license_file,
                "verified_at": engine.license.verified_at,
                "redistribution_verified": engine.license.redistribution_verified,
            },
            "third_party_components": [
                {
                    "component_id": component.component_id,
                    "display_name": component.display_name,
                    "repository": component.repository,
                    "revision": component.revision,
                    "target_subdir": component.target_subdir,
                    "license": {
                        "name": component.license.name,
                        "spdx": component.license.spdx,
                        "source": component.license.source,
                        "evidence_url": component.license.evidence_url,
                        "license_file": component.license.license_file,
                        "verified_at": component.license.verified_at,
                        "redistribution_verified": component.license.redistribution_verified,
                    },
                }
                for component in engine.third_party_components
            ],
        }
        result.append(item)
    return result


@dataclass(slots=True)
class EngineDefinition:
    """单个 ASR 引擎的当前模型定义。"""

    engine_id: str
    engine_name: str
    model_id: str
    hub: str
    revision: str | None
    target_path: str
    model_repo: str | None = None
    sub_models: list[dict[str, object]] = field(default_factory=list)
    license: dict[str, object] = field(default_factory=dict)
    third_party_components: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class EnginePackManifest:
    """ZIP 内部内容清单；归档哈希只存在于外部元数据。"""

    format_version: int
    engine_pack_version: str
    portable_release_version: str
    source_commit: str
    source_commit_short: str
    builder_commit: str
    fixture: bool
    engines: list[EngineDefinition]
    total_files: int
    files: dict[str, dict[str, object]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnginePackManifest:
        """严格解析当前内容清单；旧字段和未知字段均拒绝。"""
        required = {
            "format_version",
            "engine_pack_version",
            "portable_release_version",
            "source_commit",
            "source_commit_short",
            "builder_commit",
            "fixture",
            "engines",
            "total_files",
            "files",
        }
        missing = required - set(data)
        unknown = set(data) - required
        if missing:
            raise ValueError(f"Manifest 缺少必需字段: {sorted(missing)}")
        if unknown:
            raise ValueError(f"Manifest 包含未知字段: {sorted(unknown)}")
        if not isinstance(data["engines"], list) or not isinstance(data["files"], dict):
            raise ValueError("Manifest engines/files 类型错误")
        if not isinstance(data["format_version"], int) or isinstance(data["format_version"], bool):
            raise ValueError("Manifest format_version 必须是整数")
        if not isinstance(data["total_files"], int) or isinstance(data["total_files"], bool):
            raise ValueError("Manifest total_files 必须是整数")
        if not isinstance(data["fixture"], bool):
            raise ValueError("Manifest fixture 必须是布尔值")

        engines: list[EngineDefinition] = []
        engine_fields = {
            "engine_id",
            "engine_name",
            "model_id",
            "hub",
            "revision",
            "target_path",
            "model_repo",
            "sub_models",
            "license",
            "third_party_components",
        }
        for raw in data["engines"]:
            if not isinstance(raw, dict):
                raise ValueError("Manifest engines 只能包含对象")
            missing_engine = engine_fields - set(raw)
            unknown_engine = set(raw) - engine_fields
            if missing_engine:
                raise ValueError(f"引擎定义缺少字段: {sorted(missing_engine)}")
            if unknown_engine:
                raise ValueError(f"引擎定义包含未知字段: {sorted(unknown_engine)}")
            for field_name in ("engine_id", "engine_name", "model_id", "hub", "target_path"):
                if not isinstance(raw[field_name], str) or not raw[field_name]:
                    raise ValueError(f"引擎 {field_name} 必须是非空字符串")
            revision = raw["revision"]
            if revision is not None and not isinstance(revision, str):
                raise ValueError("引擎 revision 必须是字符串或 null")
            model_repo = raw["model_repo"]
            if model_repo is not None and (not isinstance(model_repo, str) or not model_repo):
                raise ValueError("引擎 model_repo 必须是非空字符串或 null")
            owner = f"engine[{raw['engine_id']}]"
            sub_models = _strict_sub_models(raw["sub_models"], owner)
            license_info = _strict_license(raw["license"], owner)
            components = _strict_components(raw["third_party_components"], owner)
            engines.append(
                EngineDefinition(
                    engine_id=raw["engine_id"],
                    engine_name=raw["engine_name"],
                    model_id=raw["model_id"],
                    hub=raw["hub"],
                    revision=revision,
                    target_path=raw["target_path"],
                    model_repo=model_repo,
                    sub_models=sub_models,
                    license=license_info,
                    third_party_components=components,
                )
            )
        files: dict[str, dict[str, object]] = {}
        for rel_path, file_info in data["files"].items():
            if not isinstance(rel_path, str) or not rel_path:
                raise ValueError("Manifest files 包含无效路径")
            if not isinstance(file_info, dict) or set(file_info) != {"size", "sha256"}:
                raise ValueError(f"Manifest files[{rel_path}] 必须且只能包含 size/sha256")
            size = file_info["size"]
            sha256 = file_info["sha256"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError(f"Manifest files[{rel_path}].size 无效")
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise ValueError(f"Manifest files[{rel_path}].sha256 无效")
            files[rel_path] = dict(file_info)
        for field_name in (
            "engine_pack_version",
            "portable_release_version",
            "source_commit",
            "source_commit_short",
            "builder_commit",
        ):
            if not isinstance(data[field_name], str) or not data[field_name]:
                raise ValueError(f"Manifest {field_name} 必须是非空字符串")
        return cls(
            format_version=data["format_version"],
            engine_pack_version=data["engine_pack_version"],
            portable_release_version=data["portable_release_version"],
            source_commit=data["source_commit"],
            source_commit_short=data["source_commit_short"],
            builder_commit=data["builder_commit"],
            fixture=data["fixture"],
            engines=engines,
            total_files=data["total_files"],
            files=files,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化当前内容清单。"""
        return {
            "format_version": self.format_version,
            "engine_pack_version": self.engine_pack_version,
            "portable_release_version": self.portable_release_version,
            "source_commit": self.source_commit,
            "source_commit_short": self.source_commit_short,
            "builder_commit": self.builder_commit,
            "fixture": self.fixture,
            "engines": [
                {
                    "engine_id": engine.engine_id,
                    "engine_name": engine.engine_name,
                    "model_id": engine.model_id,
                    "hub": engine.hub,
                    "revision": engine.revision,
                    "target_path": engine.target_path,
                    "model_repo": engine.model_repo,
                    "sub_models": engine.sub_models,
                    "license": engine.license,
                    "third_party_components": engine.third_party_components,
                }
                for engine in self.engines
            ],
            "total_files": self.total_files,
            "files": self.files,
        }

    def get_engine_ids(self) -> list[str]:
        """返回清单中的引擎 ID。"""
        return [engine.engine_id for engine in self.engines]

    def get_target_paths(self) -> list[str]:
        """返回清单中的模型目标路径。"""
        return [engine.target_path for engine in self.engines]


def create_manifest(
    source_commit: str,
    builder_commit: str,
    file_list: dict[str, dict[str, object]],
    *,
    fixture: bool,
) -> EnginePackManifest:
    """根据当前模型锁创建内容清单。"""
    engines = [EngineDefinition(**raw) for raw in _get_engines_for_manifest()]
    return EnginePackManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        engine_pack_version=ENGINE_PACK_VERSION,
        portable_release_version=RELEASE_VERSION,
        source_commit=source_commit,
        source_commit_short=source_commit[:7],
        builder_commit=builder_commit,
        fixture=fixture,
        engines=engines,
        total_files=len(file_list),
        files=file_list,
    )


def validate_manifest(manifest: EnginePackManifest) -> list[str]:
    """严格校验当前内容清单。"""
    errors: list[str] = []
    if manifest.format_version != MANIFEST_FORMAT_VERSION:
        errors.append(f"format_version 不匹配: manifest={manifest.format_version} expected={MANIFEST_FORMAT_VERSION}")
    if manifest.engine_pack_version != ENGINE_PACK_VERSION:
        errors.append(
            f"engine_pack_version 不匹配: manifest={manifest.engine_pack_version} expected={ENGINE_PACK_VERSION}"
        )
    if manifest.portable_release_version != RELEASE_VERSION:
        errors.append(
            f"portable_release_version 不匹配: manifest={manifest.portable_release_version} expected={RELEASE_VERSION}"
        )
    if manifest.source_commit != SOURCE_COMMIT_FULL:
        errors.append(f"source_commit 不匹配: manifest={manifest.source_commit} expected={SOURCE_COMMIT_FULL}")
    if manifest.source_commit_short != SOURCE_COMMIT_SHORT:
        errors.append(
            f"source_commit_short 不匹配: manifest={manifest.source_commit_short} expected={SOURCE_COMMIT_SHORT}"
        )
    if len(manifest.source_commit) != 40 or manifest.source_commit_short != manifest.source_commit[:7]:
        errors.append("source_commit/source_commit_short 无效")
    if len(manifest.builder_commit) != 40:
        errors.append("builder_commit 无效")
    if manifest.total_files != len(manifest.files):
        errors.append(f"文件数不一致: declared={manifest.total_files} actual={len(manifest.files)}")
    expected_ids = set(get_all_engine_ids())
    actual_ids = set(manifest.get_engine_ids())
    if actual_ids != expected_ids:
        errors.append(f"引擎集合不匹配: actual={sorted(actual_ids)} expected={sorted(expected_ids)}")
    for engine in manifest.engines:
        if not engine.engine_id or not engine.model_id or not engine.target_path:
            errors.append(f"引擎定义不完整: {engine.engine_id or '?'}")
        if ".." in Path(engine.target_path).parts:
            errors.append(f"引擎 {engine.engine_id} target_path 包含 ..")
    return errors


def validate_installable_manifest(manifest: EnginePackManifest) -> list[str]:
    """Validate archive structure without coupling content to an app release.

    An Engine Pack produced by an older BiliLiveCut release remains usable when
    every engine has the same immutable content identity.  Version, artifact
    filename, build time and source commit are therefore deliberately not part
    of this install-time validation; :mod:`blc_portable.engine_pack.installer`
    compares per-engine fingerprints against the current model catalog.
    """
    errors: list[str] = []
    if manifest.format_version != MANIFEST_FORMAT_VERSION:
        errors.append(f"format_version 不支持: {manifest.format_version}")
    if len(manifest.source_commit) != 40 or manifest.source_commit_short != manifest.source_commit[:7]:
        errors.append("source_commit/source_commit_short 无效")
    if len(manifest.builder_commit) != 40:
        errors.append("builder_commit 无效")
    if manifest.total_files != len(manifest.files):
        errors.append(f"文件数不一致: declared={manifest.total_files} actual={len(manifest.files)}")
    engine_ids = manifest.get_engine_ids()
    if len(engine_ids) != len(set(engine_ids)):
        errors.append("引擎 ID 重复")
    for engine in manifest.engines:
        if not engine.engine_id or not engine.model_id or not engine.target_path:
            errors.append(f"引擎定义不完整: {engine.engine_id or '?'}")
        target = Path(engine.target_path)
        if target.is_absolute() or ".." in target.parts:
            errors.append(f"引擎 {engine.engine_id} target_path 无效")
    return errors


def load_manifest(path: Path) -> EnginePackManifest:
    """加载且严格校验当前内容清单。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Manifest 根节点必须是对象")
    manifest = EnginePackManifest.from_dict(data)
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Manifest 校验失败:\n" + "\n".join(f"  - {error}" for error in errors))
    return manifest


def load_manifest_for_install(path: Path) -> EnginePackManifest:
    """Load a structurally valid pack for content-identity installation."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Manifest 根节点必须是对象")
    manifest = EnginePackManifest.from_dict(data)
    errors = validate_installable_manifest(manifest)
    if errors:
        raise ValueError("Manifest 校验失败:\n" + "\n".join(f"  - {error}" for error in errors))
    return manifest


def get_engine_pack_info() -> dict[str, object]:
    """生成当前外部元数据占位。"""
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "artifact_class": "fixture",
        "engine_pack_version": ENGINE_PACK_VERSION,
        "portable_release_version": RELEASE_VERSION,
        "engine_pack_api_version": MANIFEST_FORMAT_VERSION,
        "model_set_version": MANIFEST_FORMAT_VERSION,
        "filename": ARCHIVE_FILENAME,
        "size_bytes": 0,
        "crc32": "",
        "sha256": "",
        "content_manifest_sha256": "",
        "model_lock_sha256": "",
        "source_commit": "",
        "builder_commit": "",
        "build_timestamp": "",
        "expected_engine_ids": get_all_engine_ids(),
    }
