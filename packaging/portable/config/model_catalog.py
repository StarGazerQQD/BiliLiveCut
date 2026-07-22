"""模型目录加载器 — 所有模型定义的唯一权威来源。

用法:
    from blc_portable.config.model_catalog import load_engines, get_engine_by_id

    engines = load_engines()
    for e in engines:
        print(e.engine_id, e.repository, e.resolved_revision)

其他模块禁止再次定义 ENGINES、MODEL_SOURCES、ENGINES_TO_DOWNLOAD 等常量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parent / "model_sources.lock.json"
_cache: dict | None = None


@dataclass
class LicenseDef:
    """模型或随附组件的再分发许可证信息。"""

    name: str = ""
    spdx: str = ""
    source: str = ""
    evidence_url: str = ""
    license_file: str = ""
    verified_at: str = ""
    redistribution_verified: bool = False


@dataclass
class SubModelDef:
    """子模型定义（如 Paraformer 的 VAD/标点/声纹）。"""

    engine_id: str
    display_name: str
    hub: str
    repository: str
    requested_revision: str
    resolved_revision: str
    target_subdir: str
    license: LicenseDef = field(default_factory=LicenseDef)


@dataclass
class ThirdPartyComponentDef:
    """模型快照内随附、但由另一上游维护的组件。"""

    component_id: str
    display_name: str
    repository: str
    revision: str
    target_subdir: str
    license: LicenseDef = field(default_factory=LicenseDef)


@dataclass
class EngineDef:
    """ASR 引擎定义。"""

    engine_id: str
    display_name: str
    hub: str
    repository: str
    requested_revision: str
    resolved_revision: str
    target_path: str
    required_files: list[str]
    sub_models: list[SubModelDef] = field(default_factory=list)
    third_party_components: list[ThirdPartyComponentDef] = field(default_factory=list)
    repo_id: str = ""  # huggingface specific
    license: LicenseDef = field(default_factory=LicenseDef)

    @property
    def license_name(self) -> str:
        """兼容旧调用方的许可证名称属性。"""
        return self.license.name

    @property
    def license_source(self) -> str:
        """兼容旧调用方的许可证来源属性。"""
        return self.license.source

    @property
    def redistribution_verified(self) -> bool:
        """兼容旧调用方的再分发验证属性。"""
        return self.license.redistribution_verified


def _parse_license(raw: dict) -> LicenseDef:
    """解析许可证元数据。"""
    return LicenseDef(
        name=raw.get("name", ""),
        spdx=raw.get("spdx", raw.get("name", "")),
        source=raw.get("source", ""),
        evidence_url=raw.get("evidence_url", raw.get("source", "")),
        license_file=raw.get("license_file", ""),
        verified_at=raw.get("verified_at", ""),
        redistribution_verified=raw.get("redistribution_verified", False),
    )


def _load_raw_catalog() -> dict:
    """加载原始 JSON 模型配置（带缓存）。

    :returns: 模型配置字典。
    :raises FileNotFoundError: 配置文件不存在时。
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载模型目录 {_CATALOG_PATH}: {exc}") from exc
    return _cache


def _parse_engine(raw: dict) -> EngineDef:
    """解析单个引擎定义。

    :param raw: 原始 JSON 字典。
    :returns: EngineDef 实例。
    """
    sub_models = []
    for sub in raw.get("sub_models", []):
        sub_models.append(
            SubModelDef(
                engine_id=sub["engine_id"],
                display_name=sub["display_name"],
                hub=sub["hub"],
                repository=sub["repository"],
                requested_revision=sub.get("requested_revision", ""),
                resolved_revision=sub.get("resolved_revision", ""),
                target_subdir=sub["target_subdir"],
                license=_parse_license(sub.get("license", {})),
            )
        )

    third_party_components = []
    for component in raw.get("third_party_components", []):
        third_party_components.append(
            ThirdPartyComponentDef(
                component_id=component["component_id"],
                display_name=component["display_name"],
                repository=component["repository"],
                revision=component.get("revision", ""),
                target_subdir=component["target_subdir"],
                license=_parse_license(component.get("license", {})),
            )
        )

    return EngineDef(
        engine_id=raw["engine_id"],
        display_name=raw["display_name"],
        hub=raw["hub"],
        repository=raw["repository"],
        requested_revision=raw.get("requested_revision", ""),
        resolved_revision=raw.get("resolved_revision", ""),
        target_path=raw["target_path"],
        required_files=raw.get("required_files", []),
        sub_models=sub_models,
        third_party_components=third_party_components,
        repo_id=raw.get("repository", "") if raw["hub"] == "huggingface" else "",
        license=_parse_license(raw.get("license", {})),
    )


def load_engines() -> list[EngineDef]:
    """加载所有引擎定义。

    :returns: EngineDef 列表。
    """
    catalog = _load_raw_catalog()
    return [_parse_engine(e) for e in catalog["engines"]]


def get_engine_by_id(engine_id: str) -> EngineDef | None:
    """按 ID 查找引擎。

    :param engine_id: 引擎 ID。
    :returns: EngineDef 或 None。
    """
    for e in load_engines():
        if e.engine_id == engine_id:
            return e
    return None


def get_all_engine_ids() -> list[str]:
    """获取所有引擎 ID 列表。

    :returns: 引擎 ID 列表。
    """
    return [e.engine_id for e in load_engines()]


def get_engine_pack_version() -> str:
    """获取 engine pack 版本。

    :returns: 版本字符串。
    """
    return _load_raw_catalog()["engine_pack_version"]


def get_compatible_app_range() -> dict[str, str]:
    """获取兼容 App 版本范围。

    :returns: {"min": "...", "max_exclusive": "..."}
    """
    return _load_raw_catalog()["compatible_app"]


def validate_catalog() -> list[str]:
    """验证模型目录完整性。

    检查:
    - engine ID 唯一
    - target path 唯一
    - resolved_revision 不为空
    - required_files 非空
    - repository 格式有效

    :returns: 错误信息列表。空列表表示通过。
    """
    errors: list[str] = []
    catalog = _load_raw_catalog()

    def validate_license(owner: str, license_info: dict) -> None:
        required = ("name", "spdx", "source", "evidence_url", "license_file", "verified_at")
        for key in required:
            if not license_info.get(key):
                errors.append(f"{owner}: license.{key} 为空")
        if license_info.get("redistribution_verified") is not True:
            errors.append(f"{owner}: redistribution_verified 未通过")

        relative_file = Path(str(license_info.get("license_file", "")))
        if relative_file.is_absolute() or ".." in relative_file.parts:
            errors.append(f"{owner}: license_file 路径无效")
        elif relative_file.parts and not (_CATALOG_PATH.parent.parent / relative_file).is_file():
            errors.append(f"{owner}: license_file 不存在 '{relative_file.as_posix()}'")

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for engine in catalog["engines"]:
        eid = engine["engine_id"]
        if eid in seen_ids:
            errors.append(f"重复 engine_id: {eid}")
        seen_ids.add(eid)

        tpath = engine["target_path"]
        if tpath in seen_paths:
            errors.append(f"重复 target_path: {tpath}")
        seen_paths.add(tpath)

        if not engine.get("resolved_revision"):
            errors.append(f"引擎 {eid}: resolved_revision 为空")

        if not engine.get("required_files"):
            errors.append(f"引擎 {eid}: required_files 为空")

        validate_license(f"引擎 {eid}", engine.get("license", {}))

        repo = engine.get("repository", "")
        if not repo or "/" not in repo:
            errors.append(f"引擎 {eid}: repository 格式无效 '{repo}'")

        # 检查 FunASR-Nano 使用正确仓库
        if eid == "funasr_nano" and "iic/Fun-ASR-Nano" in repo and "FunAudioLLM" not in repo:
            errors.append(f"funasr_nano: 使用错误的仓库 ID '{repo}', 应使用 'FunAudioLLM/Fun-ASR-Nano-2512'")

        for sub in engine.get("sub_models", []):
            sub_repo = sub.get("repository", "")
            if not sub_repo or "/" not in sub_repo:
                errors.append(f"引擎 {eid} 子模型 {sub.get('engine_id')}: repository 格式无效")
            if not sub.get("resolved_revision"):
                errors.append(f"引擎 {eid} 子模型 {sub.get('engine_id')}: resolved_revision 为空")
            validate_license(
                f"引擎 {eid} 子模型 {sub.get('engine_id')}",
                sub.get("license", {}),
            )

        for component in engine.get("third_party_components", []):
            component_id = component.get("component_id")
            component_repo = component.get("repository", "")
            if not component_repo or "/" not in component_repo:
                errors.append(f"引擎 {eid} 随附组件 {component_id}: repository 格式无效")
            if not component.get("revision"):
                errors.append(f"引擎 {eid} 随附组件 {component_id}: revision 为空")
            validate_license(
                f"引擎 {eid} 随附组件 {component_id}",
                component.get("license", {}),
            )

    return errors
