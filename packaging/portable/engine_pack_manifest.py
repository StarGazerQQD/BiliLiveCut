"""Engine Pack Manifest 数据结构与校验。

定义四引擎模型包的 Manifest 格式，包含引擎列表、模型版本、文件清单和校验信息。
与 ``731a31c`` 中的四引擎真实配置严格对应：

============  ========  ===========================  ==========  ==========================
引擎          引擎 ID   模型 ID                       Revision   下载源
============  ========  ===========================  ==========  ==========================
Whisper       whisper   large-v3-turbo               N/A         HuggingFace
Paraformer    paraformer paraformer-zh               v2.0.4      ModelScope (hub="ms")
SenseVoice    sensevoice iic/SenseVoiceSmall          v2.0.4      ModelScope (hub="ms")
FunASR-Nano   funasr_nano iic/Fun-ASR-Nano            v2.0.4      ModelScope (hub="ms")
============  ========  ===========================  ==========  ==========================

Paraformer 额外需要 ``fsmn-vad`` / ``ct-punc`` / ``cam++`` 三个子模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════

ENGINE_PACK_VERSION = "0.1.14.6-alpha"
RELEASE_VERSION = "0.1.14.6-alpha"
SOURCE_COMMIT_SHORT = "731a31c"
MANIFEST_FORMAT_VERSION = 1
ARCHIVE_FILENAME = f"BiliLiveCut-EnginePack-{ENGINE_PACK_VERSION}.zip"

# ── 四引擎定义 (与 731a31c 源码严格对应) ──

ENGINES: list[dict[str, object]] = [
    {
        "engine_id": "whisper",
        "engine_name": "Whisper (兜底引擎)",
        "model_id": "large-v3-turbo",
        "model_repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "hub": "huggingface",
        "revision": None,  # Whisper 使用 model_size 参数，无 Revision
        "target_path": "models/whisper",
    },
    {
        "engine_id": "paraformer",
        "engine_name": "Paraformer-zh (主引擎)",
        "model_id": "paraformer-zh",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/paraformer",
        # Paraformer 的 AutoModel 额外需要三个子模型
        "sub_models": [
            {"model_id": "fsmn-vad", "hub": "modelscope", "revision": "v2.0.4"},
            {"model_id": "ct-punc", "hub": "modelscope", "revision": "v2.0.4"},
            {"model_id": "cam++", "hub": "modelscope", "revision": "v2.0.4"},
        ],
    },
    {
        "engine_id": "sensevoice",
        "engine_name": "SenseVoice-Small (辅助特征)",
        "model_id": "iic/SenseVoiceSmall",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/sensevoice",
    },
    {
        "engine_id": "funasr_nano",
        "engine_name": "Fun-ASR-Nano (低置信复核)",
        "model_id": "iic/Fun-ASR-Nano",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/funasr_nano",
    },
]

# ── ModelScope 国内镜像 ──
MODELSCOPE_MIRRORS = [
    "https://www.modelscope.cn",
]

# ── HuggingFace 国内镜像 ──
HF_MIRRORS = [
    "https://hf-mirror.com",
    "https://huggingface.co",
]


# ═══════════════════════════════════════════════════════════
# Dataclass
# ═══════════════════════════════════════════════════════════


@dataclass(slots=True)
class EngineDefinition:
    """单个引擎定义。"""

    engine_id: str
    engine_name: str
    model_id: str
    hub: str  # "huggingface" | "modelscope"
    revision: str | None
    target_path: str
    model_repo: str | None = None
    sub_models: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class EnginePackManifest:
    """Engine Pack Manifest 完整结构。"""

    format_version: int
    engine_pack_version: str
    portable_release_version: str
    source_commit: str
    source_commit_short: str
    archive_filename: str
    archive_crc32: str  # 8 位大写十六进制
    archive_sha256: str
    engines: list[EngineDefinition]
    total_files: int = 0
    files: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnginePackManifest:
        """从字典解析。

        :param data: Manifest 字典。
        :returns: EnginePackManifest 实例。
        :raises ValueError: 必需字段缺失时。
        """
        required = [
            "format_version",
            "engine_pack_version",
            "portable_release_version",
            "source_commit",
            "archive_filename",
            "archive_crc32",
            "archive_sha256",
            "engines",
        ]
        for key in required:
            if key not in data:
                raise ValueError(f"Manifest 缺少必需字段: {key}")

        engines: list[EngineDefinition] = []
        for e in data["engines"]:
            engines.append(
                EngineDefinition(
                    engine_id=str(e["engine_id"]),
                    engine_name=str(e.get("engine_name", e["engine_id"])),
                    model_id=str(e["model_id"]),
                    hub=str(e.get("hub", "modelscope")),
                    revision=e.get("revision") if e.get("revision") else None,
                    target_path=str(e["target_path"]),
                    model_repo=e.get("model_repo") if e.get("model_repo") else None,
                    sub_models=e.get("sub_models", []),
                )
            )

        return cls(
            format_version=int(data["format_version"]),
            engine_pack_version=str(data["engine_pack_version"]),
            portable_release_version=str(data["portable_release_version"]),
            source_commit=str(data["source_commit"]),
            source_commit_short=str(data.get("source_commit_short", "")),
            archive_filename=str(data["archive_filename"]),
            archive_crc32=str(data["archive_crc32"]),
            archive_sha256=str(data["archive_sha256"]),
            engines=engines,
            total_files=int(data.get("total_files", 0)),
            files=data.get("files", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        :returns: Manifest 字典。
        """
        return {
            "format_version": self.format_version,
            "engine_pack_version": self.engine_pack_version,
            "portable_release_version": self.portable_release_version,
            "source_commit": self.source_commit,
            "source_commit_short": self.source_commit_short,
            "archive_filename": self.archive_filename,
            "archive_crc32": self.archive_crc32,
            "archive_sha256": self.archive_sha256,
            "total_files": self.total_files,
            "engines": [
                {
                    "engine_id": e.engine_id,
                    "engine_name": e.engine_name,
                    "model_id": e.model_id,
                    "hub": e.hub,
                    "revision": e.revision,
                    "target_path": e.target_path,
                    "model_repo": e.model_repo,
                    "sub_models": e.sub_models,
                }
                for e in self.engines
            ],
            "files": self.files,
        }

    def get_engine_ids(self) -> list[str]:
        """获取所有引擎 ID 列表。

        :returns: 引擎 ID 列表。
        """
        return [e.engine_id for e in self.engines]

    def get_target_paths(self) -> list[str]:
        """获取所有目标路径。

        :returns: 目标路径列表。
        """
        return [e.target_path for e in self.engines]


def create_manifest(
    source_commit: str,
    archive_crc32: str,
    archive_sha256: str,
    file_list: dict[str, dict[str, object]],
) -> EnginePackManifest:
    """根据四引擎定义创建 Manifest。

    :param source_commit: 731a31c 对应的完整 Commit Hash。
    :param archive_crc32: ZIP 文件的 CRC32 (8 位大写十六进制)。
    :param archive_sha256: ZIP 文件的 SHA-256。
    :param file_list: 逐文件信息 {path: {size, sha256}}。
    :returns: EnginePackManifest 实例。
    """
    engines = [
        EngineDefinition(
            engine_id=str(e["engine_id"]),
            engine_name=str(e["engine_name"]),
            model_id=str(e["model_id"]),
            hub=str(e["hub"]),
            revision=e["revision"] if isinstance(e["revision"], str) else None,
            target_path=str(e["target_path"]),
            model_repo=e.get("model_repo") if isinstance(e.get("model_repo"), str) else None,
            sub_models=e.get("sub_models", []),
        )
        for e in ENGINES
    ]

    return EnginePackManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        engine_pack_version=ENGINE_PACK_VERSION,
        portable_release_version=RELEASE_VERSION,
        source_commit=source_commit,
        source_commit_short=SOURCE_COMMIT_SHORT,
        archive_filename=ARCHIVE_FILENAME,
        archive_crc32=archive_crc32,
        archive_sha256=archive_sha256,
        engines=engines,
        total_files=len(file_list),
        files=file_list,
    )


def validate_manifest(manifest: EnginePackManifest) -> list[str]:
    """校验 Manifest 完整性。

    :param manifest: EnginePackManifest 实例。
    :returns: 错误列表，空表示通过。
    """
    errors: list[str] = []

    if manifest.format_version < 1:
        errors.append(f"format_version 无效: {manifest.format_version}")

    if not manifest.engine_pack_version:
        errors.append("engine_pack_version 为空")

    if not manifest.source_commit or len(manifest.source_commit) < 7:
        errors.append("source_commit 无效")

    if not manifest.archive_filename:
        errors.append("archive_filename 为空")

    if not manifest.archive_crc32 or len(manifest.archive_crc32) != 8:
        errors.append(f"archive_crc32 格式无效: {manifest.archive_crc32} (应为 8 位十六进制)")

    if not manifest.archive_sha256 or len(manifest.archive_sha256) != 64:
        errors.append("archive_sha256 无效")

    if not manifest.engines:
        errors.append("engines 列表为空")

    expected_ids = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
    actual_ids = set(manifest.get_engine_ids())
    missing = expected_ids - actual_ids
    extra = actual_ids - expected_ids
    if missing:
        errors.append(f"缺少引擎: {missing}")
    if extra:
        errors.append(f"未知引擎: {extra}")

    for engine in manifest.engines:
        if not engine.engine_id:
            errors.append("引擎 engine_id 为空")
        if not engine.model_id:
            errors.append(f"引擎 {engine.engine_id} model_id 为空")
        if not engine.target_path:
            errors.append(f"引擎 {engine.engine_id} target_path 为空")
        if engine.target_path and ".." in engine.target_path:
            errors.append(f"引擎 {engine.engine_id} target_path 包含 ..")

    return errors


def load_manifest(path: Path) -> EnginePackManifest:
    """从 JSON 文件加载 Manifest。

    :param path: Manifest JSON 文件路径。
    :returns: EnginePackManifest 实例。
    :raises ValueError: 解析或校验失败时。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    manifest = EnginePackManifest.from_dict(data)
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Manifest 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
    return manifest


def get_engine_pack_info() -> dict[str, str]:
    """生成 engine_pack_info.json 内容（供 PyInstaller 嵌入）。

    :returns: engine_pack_info 字典。
    """
    return {
        "engine_pack_version": ENGINE_PACK_VERSION,
        "filename": ARCHIVE_FILENAME,
        "crc32": "",  # 由 build_engine_pack.py 填入真实 CRC32
        "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
    }
