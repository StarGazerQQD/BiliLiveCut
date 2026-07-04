"""模型版本注册表 (v0.1.8.2.1-HL-alpha)。

管理多个模型版本：存储、列出版本、激活、回滚、删除旧版本。
支持 Shadow/Champion 双轨模式下的版本对比。
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_BASE = Path("storage/models")
_REGISTRY_PATH = _MODEL_BASE / "model_registry.json"


@dataclass(slots=True)
class ModelVersion:
    """一个模型版本的元数据。"""
    version: int
    path: str
    metrics: dict[str, float]
    n_samples: int
    n_positive: int
    feature_names: list[str] = field(default_factory=list)
    created_at: str = ""
    is_active: bool = False
    is_champion: bool = False  # Champion = 当前生产中使用的版本
    is_shadow: bool = False    # Shadow = 双轨评估中的候选版本


class ModelRegistry:
    """模型版本注册表。

    - 每次训练自动保存版本号递增的模型文件
    - 支持回滚到任意历史版本
    - 自动清理超过 max_versions 的旧版本
    - 维护 champion（生产）与 shadow（评估）两个角色

    :param max_versions: 最多保留的版本数，超出自动删除最旧的。
    """

    def __init__(self, max_versions: int = 10) -> None:
        self.max_versions = max_versions
        self._versions: list[ModelVersion] = []
        self._load()

    # ------------------------------------------------------------------ #
    # 注册新版本
    # ------------------------------------------------------------------ #
    def register(self, model_path: str, metrics: dict[str, float],
                 n_samples: int, n_positive: int,
                 feature_names: list[str] | None = None) -> int:
        """注册一个训练好的模型为新版本。

        :param model_path: 模型文件路径。
        :param metrics: 评估指标字典。
        :param n_samples: 训练样本数。
        :param n_positive: 正样本数。
        :param feature_names: 特征名称列表。
        :returns: 新版本号。
        """
        version = max((v.version for v in self._versions), default=0) + 1

        # 复制模型文件为版本化名称
        src = Path(model_path)
        versioned_name = f"highlight_model_v{version}{src.suffix}"
        versioned_path = _MODEL_BASE / versioned_name
        _MODEL_BASE.mkdir(parents=True, exist_ok=True)
        if src.exists() and src != versioned_path:
            shutil.copy2(src, versioned_path)

        # 保存元数据
        meta = {
            "metrics": metrics,
            "n_samples": n_samples,
            "n_positive": n_positive,
            "feature_names": feature_names or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": version,
        }
        meta_path = versioned_path.with_suffix(".meta.json")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        mv = ModelVersion(
            version=version,
            path=str(versioned_path),
            metrics=metrics,
            n_samples=n_samples,
            n_positive=n_positive,
            feature_names=feature_names or [],
            created_at=meta["created_at"],
        )
        self._versions.append(mv)
        # 首个版本自动设为 champion
        if len(self._versions) == 1:
            mv.is_champion = True
            mv.is_active = True

        self._save()
        self._cleanup_old()
        logger.info("模型 v%d 已注册 champion=%s AUC=%.3f", version, mv.is_champion, metrics.get("auc", 0))
        return version

    # ------------------------------------------------------------------ #
    # Shadow / Champion 管理
    # ------------------------------------------------------------------ #
    def promote_shadow(self, version: int) -> bool:
        """将指定版本提升为 Champion，原 Champion 降级存档。

        :param version: 要提升的版本号。
        :returns: 成功返回 True。
        """
        target = self._find(version)
        if target is None:
            return False
        for v in self._versions:
            if v.is_champion:
                v.is_champion = False
                v.is_active = False
                logger.info("Champion v%d 已降级", v.version)
        target.is_champion = True
        target.is_active = True
        target.is_shadow = False
        self._save()
        logger.info("Shadow v%d → Champion", version)
        return True

    def set_shadow(self, version: int) -> bool:
        """将指定版本设为 Shadow（双轨评估模式候选）。

        :param version: 版本号。
        :returns: 成功返回 True。
        """
        target = self._find(version)
        if target is None:
            return False
        for v in self._versions:
            if v.is_shadow:
                v.is_shadow = False
        target.is_shadow = True
        self._save()
        logger.info("v%d 已设为 Shadow", version)
        return True

    def rollback(self, version: int) -> bool:
        """回滚到指定版本（设为 Champion）。

        :param version: 目标版本号。
        :returns: 成功返回 True。
        """
        return self.promote_shadow(version)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def champion(self) -> ModelVersion | None:
        """返回当前 Champion 版本。"""
        for v in self._versions:
            if v.is_champion:
                return v
        return None

    @property
    def shadow(self) -> ModelVersion | None:
        """返回当前 Shadow 版本。"""
        for v in self._versions:
            if v.is_shadow:
                return v
        return None

    @property
    def has_shadow(self) -> bool:
        """是否有正在双轨评估的版本。"""
        return self.shadow is not None

    @property
    def versions(self) -> list[ModelVersion]:
        """返回所有版本列表（按版本号降序）。"""
        return sorted(self._versions, key=lambda v: v.version, reverse=True)

    @property
    def champion_path(self) -> str:
        c = self.champion
        return c.path if c else ""

    @property
    def shadow_path(self) -> str:
        s = self.shadow
        return s.path if s else ""

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _find(self, version: int) -> ModelVersion | None:
        for v in self._versions:
            if v.version == version:
                return v
        return None

    def _load(self) -> None:
        if not _REGISTRY_PATH.exists():
            return
        try:
            data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            for item in data.get("versions", []):
                self._versions.append(ModelVersion(**item))
        except Exception as exc:
            logger.warning("模型注册表加载失败: %s", exc)

    def _save(self) -> None:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(
            json.dumps(
                {"versions": [
                    {
                        "version": v.version, "path": v.path,
                        "metrics": v.metrics, "n_samples": v.n_samples,
                        "n_positive": v.n_positive,
                        "feature_names": v.feature_names,
                        "created_at": v.created_at,
                        "is_active": v.is_active,
                        "is_champion": v.is_champion,
                        "is_shadow": v.is_shadow,
                    }
                    for v in self._versions
                ]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def _cleanup_old(self) -> None:
        """删除超出 max_versions 的最旧且非活跃版本。"""
        deletable = [v for v in self._versions if not v.is_champion and not v.is_shadow]
        deletable.sort(key=lambda v: v.version)
        while len([v for v in self._versions if not v.is_champion]) > self.max_versions and deletable:
            old = deletable.pop(0)
            path = Path(old.path)
            if path.exists():
                path.unlink()
                logger.info("已清理旧模型 v%d", old.version)
            meta = path.with_suffix(".meta.json")
            if meta.exists():
                meta.unlink()
            self._versions.remove(old)
        self._save()
