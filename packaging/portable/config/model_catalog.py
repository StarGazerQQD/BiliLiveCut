"""当前模型锁加载器；未知字段、缺失字段和非当前版本均拒绝。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(__file__).resolve().parent
_CATALOG_PATH = _CONFIG_DIR / "model_sources.lock.json"
_VERSION_PATH = _CONFIG_DIR / "version.json"
_LICENSE_FIELDS = {
    "name",
    "spdx",
    "source",
    "evidence_url",
    "license_file",
    "verified_at",
    "redistribution_verified",
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "engine_pack_version",
    "portable_release_version",
    "source_commit",
    "generated_at",
    "engines",
}
_ENGINE_FIELDS = {
    "engine_id",
    "display_name",
    "hub",
    "repository",
    "requested_revision",
    "resolved_revision",
    "target_path",
    "required_files",
    "files",
    "license",
    "sub_models",
    "third_party_components",
}
_SUB_MODEL_FIELDS = {
    "engine_id",
    "display_name",
    "hub",
    "repository",
    "requested_revision",
    "resolved_revision",
    "target_subdir",
    "license",
}
_THIRD_PARTY_FIELDS = {
    "component_id",
    "display_name",
    "repository",
    "revision",
    "target_subdir",
    "license",
}
_cache: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LicenseDef:
    """模型或随附组件的再分发许可证信息。"""

    name: str
    spdx: str
    source: str
    evidence_url: str
    license_file: str
    verified_at: str
    redistribution_verified: bool


@dataclass(frozen=True, slots=True)
class SubModelDef:
    """Paraformer 等引擎的子模型定义。"""

    engine_id: str
    display_name: str
    hub: str
    repository: str
    requested_revision: str
    resolved_revision: str
    target_subdir: str
    license: LicenseDef


@dataclass(frozen=True, slots=True)
class ThirdPartyComponentDef:
    """模型快照内由另一上游维护的组件。"""

    component_id: str
    display_name: str
    repository: str
    revision: str
    target_subdir: str
    license: LicenseDef


@dataclass(frozen=True, slots=True)
class EngineDef:
    """当前 Engine Pack 中的一个 ASR 引擎。"""

    engine_id: str
    display_name: str
    hub: str
    repository: str
    requested_revision: str
    resolved_revision: str
    target_path: str
    required_files: list[str]
    files: dict[str, dict[str, object]]
    license: LicenseDef
    sub_models: list[SubModelDef] = field(default_factory=list)
    third_party_components: list[ThirdPartyComponentDef] = field(default_factory=list)
    repo_id: str = ""


def _require_exact_fields(raw: object, expected: set[str], owner: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{owner} 必须是对象")
    actual = set(raw)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{owner} 缺少字段: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{owner} 包含未知字段: {sorted(unknown)}")
    return raw


def _require_nonempty_string(raw: dict[str, Any], field_name: str, owner: str) -> str:
    value = raw[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}.{field_name} 必须是非空字符串")
    return value


def _parse_license(raw: object, owner: str) -> LicenseDef:
    item = _require_exact_fields(raw, _LICENSE_FIELDS, f"{owner}.license")
    redistribution = item["redistribution_verified"]
    if not isinstance(redistribution, bool):
        raise ValueError(f"{owner}.license.redistribution_verified 必须是布尔值")
    return LicenseDef(
        name=_require_nonempty_string(item, "name", f"{owner}.license"),
        spdx=_require_nonempty_string(item, "spdx", f"{owner}.license"),
        source=_require_nonempty_string(item, "source", f"{owner}.license"),
        evidence_url=_require_nonempty_string(item, "evidence_url", f"{owner}.license"),
        license_file=_require_nonempty_string(item, "license_file", f"{owner}.license"),
        verified_at=_require_nonempty_string(item, "verified_at", f"{owner}.license"),
        redistribution_verified=redistribution,
    )


def _parse_string_list(value: object, owner: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{owner} 必须是{'可为空的' if allow_empty else '非空'}字符串数组")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{owner} 只能包含非空字符串")
    return list(value)


def _parse_files(value: object, owner: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError(f"{owner} 必须是对象")
    result: dict[str, dict[str, object]] = {}
    for path, info in value.items():
        if not isinstance(path, str) or not path or not isinstance(info, dict):
            raise ValueError(f"{owner} 的路径和文件信息类型无效")
        if set(info) != {"size", "sha256"}:
            raise ValueError(f"{owner}.{path} 必须且只能包含 size/sha256")
        if not isinstance(info["size"], int) or info["size"] < 0:
            raise ValueError(f"{owner}.{path}.size 无效")
        if not isinstance(info["sha256"], str) or len(info["sha256"]) != 64:
            raise ValueError(f"{owner}.{path}.sha256 无效")
        result[path] = dict(info)
    return result


def _parse_sub_model(raw: object, owner: str) -> SubModelDef:
    item = _require_exact_fields(raw, _SUB_MODEL_FIELDS, owner)
    return SubModelDef(
        engine_id=_require_nonempty_string(item, "engine_id", owner),
        display_name=_require_nonempty_string(item, "display_name", owner),
        hub=_require_nonempty_string(item, "hub", owner),
        repository=_require_nonempty_string(item, "repository", owner),
        requested_revision=_require_nonempty_string(item, "requested_revision", owner),
        resolved_revision=_require_nonempty_string(item, "resolved_revision", owner),
        target_subdir=_require_nonempty_string(item, "target_subdir", owner),
        license=_parse_license(item["license"], owner),
    )


def _parse_component(raw: object, owner: str) -> ThirdPartyComponentDef:
    item = _require_exact_fields(raw, _THIRD_PARTY_FIELDS, owner)
    return ThirdPartyComponentDef(
        component_id=_require_nonempty_string(item, "component_id", owner),
        display_name=_require_nonempty_string(item, "display_name", owner),
        repository=_require_nonempty_string(item, "repository", owner),
        revision=_require_nonempty_string(item, "revision", owner),
        target_subdir=_require_nonempty_string(item, "target_subdir", owner),
        license=_parse_license(item["license"], owner),
    )


def _parse_engine(raw: object) -> EngineDef:
    raw = _require_exact_fields(raw, _ENGINE_FIELDS, "engine")
    engine_id = _require_nonempty_string(raw, "engine_id", "engine")
    owner = f"engine[{engine_id}]"
    sub_raw = raw["sub_models"]
    component_raw = raw["third_party_components"]
    if not isinstance(sub_raw, list) or not isinstance(component_raw, list):
        raise ValueError(f"{owner} 的 sub_models/third_party_components 必须是数组")
    hub = _require_nonempty_string(raw, "hub", owner)
    repository = _require_nonempty_string(raw, "repository", owner)
    requested_revision = raw["requested_revision"]
    if not isinstance(requested_revision, str):
        raise ValueError(f"{owner}.requested_revision 必须是字符串")
    return EngineDef(
        engine_id=engine_id,
        display_name=_require_nonempty_string(raw, "display_name", owner),
        hub=hub,
        repository=repository,
        requested_revision=requested_revision,
        resolved_revision=_require_nonempty_string(raw, "resolved_revision", owner),
        target_path=_require_nonempty_string(raw, "target_path", owner),
        required_files=_parse_string_list(raw["required_files"], f"{owner}.required_files", allow_empty=False),
        files=_parse_files(raw["files"], f"{owner}.files"),
        license=_parse_license(raw["license"], owner),
        sub_models=[_parse_sub_model(item, f"{owner}.sub_models[{index}]") for index, item in enumerate(sub_raw)],
        third_party_components=[
            _parse_component(item, f"{owner}.third_party_components[{index}]")
            for index, item in enumerate(component_raw)
        ],
        repo_id=repository if hub == "huggingface" else "",
    )


def _load_version_config() -> dict[str, Any]:
    data = json.loads(_VERSION_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("version.json 根节点必须是对象")
    return data


def _load_raw_catalog() -> dict[str, Any]:
    """加载并严格验证唯一模型锁。"""
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载模型目录 {_CATALOG_PATH}: {exc}") from exc
    try:
        catalog = _require_exact_fields(raw, _TOP_LEVEL_FIELDS, "model_sources.lock.json")
        version = _load_version_config()
        if catalog["schema_version"] != version["model_lock_schema"]:
            raise ValueError("模型锁 schema_version 与当前版本不一致")
        if catalog["engine_pack_version"] != version["engine_pack_version"]:
            raise ValueError("模型锁 engine_pack_version 与当前版本不一致")
        if catalog["portable_release_version"] != version["release_version"]:
            raise ValueError("模型锁 portable_release_version 与当前版本不一致")
        if catalog["source_commit"] != version["source_commit_full"]:
            raise ValueError("模型锁 source_commit 与当前源码基线不一致")
        if not isinstance(catalog["generated_at"], str) or not catalog["generated_at"]:
            raise ValueError("模型锁 generated_at 必须是非空字符串")
        if not isinstance(catalog["engines"], list) or not catalog["engines"]:
            raise ValueError("模型锁 engines 必须是非空数组")
        # 解析一次即可验证每个嵌套结构，拒绝旧字段和缺省字段。
        [_parse_engine(engine) for engine in catalog["engines"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"模型目录格式无效: {exc}") from exc
    _cache = catalog
    return catalog


def load_engines() -> list[EngineDef]:
    """加载当前版本的所有引擎定义。"""
    return [_parse_engine(engine) for engine in _load_raw_catalog()["engines"]]


def get_engine_by_id(engine_id: str) -> EngineDef | None:
    """按 ID 查找当前引擎。"""
    return next((engine for engine in load_engines() if engine.engine_id == engine_id), None)


def get_all_engine_ids() -> list[str]:
    """返回当前 Engine Pack 的引擎 ID。"""
    return [engine.engine_id for engine in load_engines()]


def get_engine_pack_version() -> str:
    """返回当前 Engine Pack 版本。"""
    return str(_load_raw_catalog()["engine_pack_version"])


def validate_catalog() -> list[str]:
    """验证模型锁语义和本地许可证文件。"""
    try:
        engines = load_engines()
    except RuntimeError as exc:
        return [str(exc)]
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for engine in engines:
        if engine.engine_id in seen_ids:
            errors.append(f"重复 engine_id: {engine.engine_id}")
        seen_ids.add(engine.engine_id)
        if engine.target_path in seen_paths:
            errors.append(f"重复 target_path: {engine.target_path}")
        seen_paths.add(engine.target_path)
        if "/" not in engine.repository:
            errors.append(f"引擎 {engine.engine_id}: repository 格式无效")
        licenses = [engine.license, *(sub.license for sub in engine.sub_models)]
        licenses.extend(component.license for component in engine.third_party_components)
        for license_info in licenses:
            license_path = Path(license_info.license_file)
            if license_path.is_absolute() or ".." in license_path.parts:
                errors.append(f"引擎 {engine.engine_id}: license_file 路径无效")
            elif not (_CATALOG_PATH.parent.parent / license_path).is_file():
                errors.append(f"引擎 {engine.engine_id}: license_file 不存在 '{license_path.as_posix()}'")
            if not license_info.redistribution_verified:
                errors.append(f"引擎 {engine.engine_id}: redistribution_verified 未通过")
    return errors
