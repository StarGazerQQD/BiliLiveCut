"""原子模型注册表、Champion/Shadow 与真实回滚。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from app.analysis.highlight_ml.models import ModelArtifact


def _canonical_json(payload: dict[str, object]) -> bytes:
    """返回稳定 UTF-8 JSON 字节。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    """在同目录落盘、fsync 后原子替换目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _RegistryLock(AbstractContextManager[None]):
    """基于独占创建的短时跨进程注册表锁。"""

    def __init__(self, path: Path, timeout_s: float = 5.0) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._fd: int | None = None

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"pid={os.getpid()} time={datetime.now(UTC).isoformat()}".encode())
                return None
            except FileExistsError:
                try:
                    lock_age = time.time() - self.path.stat().st_mtime
                    if lock_age > 30.0:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"模型注册表锁超时: {self.path}") from None
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self.path.exists():
            self.path.unlink()


@dataclass(frozen=True, slots=True)
class RegisteredVersion:
    """注册表中的不可变模型版本。"""

    version: int
    relative_path: str
    artifact_sha256: str
    model_type: str
    schema_fingerprint: str
    metrics: dict[str, float]
    n_samples: int
    n_positive: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        """转换为注册表 JSON。"""
        return {
            "version": self.version,
            "relative_path": self.relative_path,
            "artifact_sha256": self.artifact_sha256,
            "model_type": self.model_type,
            "schema_fingerprint": self.schema_fingerprint,
            "metrics": self.metrics,
            "n_samples": self.n_samples,
            "n_positive": self.n_positive,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> RegisteredVersion:
        """从注册表 JSON 恢复版本。"""
        try:
            return cls(
                version=int(payload["version"]),
                relative_path=str(payload["relative_path"]),
                artifact_sha256=str(payload["artifact_sha256"]),
                model_type=str(payload["model_type"]),
                schema_fingerprint=str(payload["schema_fingerprint"]),
                metrics={str(key): float(value) for key, value in payload["metrics"].items()},  # type: ignore[union-attr]
                n_samples=int(payload["n_samples"]),
                n_positive=int(payload["n_positive"]),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("模型注册表版本格式无效") from exc


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """供热加载器比较的注册表快照。"""

    generation: int
    champion_version: int | None
    shadow_version: int | None
    versions: tuple[RegisteredVersion, ...]


class ModelRegistry:
    """以完成模型目录为前提、原子更新角色指针的注册表。"""

    def __init__(self, root: str | Path, *, schema_fingerprint: str, max_versions: int = 10) -> None:
        self.root = Path(root)
        self.schema_fingerprint = schema_fingerprint
        self.max_versions = max(2, max_versions)
        self._registry_path = self.root / "registry.json"
        self._versions_path = self.root / "versions"
        self._lock_path = self.root / ".registry.lock"

    def _empty_state(self) -> dict[str, object]:
        return {"format_version": 1, "generation": 0, "champion": None, "shadow": None, "versions": []}

    def _read_state(self) -> dict[str, object]:
        if not self._registry_path.exists():
            return self._empty_state()
        try:
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"模型注册表损坏: {self._registry_path}") from exc
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            raise RuntimeError("模型注册表版本无效")
        return payload

    def snapshot(self) -> RegistrySnapshot:
        """读取最新 generation 和角色指针。"""
        state = self._read_state()
        raw_versions = state.get("versions", [])
        if not isinstance(raw_versions, list):
            raise RuntimeError("模型注册表 versions 无效")
        versions = tuple(RegisteredVersion.from_dict(item) for item in raw_versions if isinstance(item, dict))
        champion = state.get("champion")
        shadow = state.get("shadow")
        return RegistrySnapshot(
            generation=int(state.get("generation", 0)),
            champion_version=int(champion) if champion is not None else None,
            shadow_version=int(shadow) if shadow is not None else None,
            versions=versions,
        )

    def _write_state(self, state: dict[str, object]) -> None:
        _atomic_write(self._registry_path, _canonical_json(state))

    def _next_version(self, state: dict[str, object]) -> int:
        registered = [
            int(item["version"])
            for item in state.get("versions", [])  # type: ignore[union-attr]
            if isinstance(item, dict) and "version" in item
        ]
        on_disk = []
        if self._versions_path.exists():
            for path in self._versions_path.glob("v[0-9][0-9][0-9][0-9][0-9][0-9]"):
                try:
                    on_disk.append(int(path.name[1:]))
                except ValueError:
                    continue
        return max([0, *registered, *on_disk]) + 1

    def register(
        self,
        artifact: ModelArtifact,
        *,
        as_shadow: bool = False,
        attachments: Mapping[str, bytes] | None = None,
    ) -> RegisteredVersion:
        """完整写入新版本后，最后原子发布注册表指针。"""
        if artifact.schema_fingerprint != self.schema_fingerprint:
            raise ValueError("模型产物 Schema 与注册表不一致")
        attachment_payloads = dict(attachments or {})
        for name, content in attachment_payloads.items():
            if not name or Path(name).name != name or name.startswith("."):
                raise ValueError(f"模型附件名无效: {name}")
            if not isinstance(content, bytes):
                raise TypeError(f"模型附件必须是 bytes: {name}")
        artifact_bytes = _canonical_json(artifact.to_dict())
        checksum = hashlib.sha256(artifact_bytes).hexdigest()
        with _RegistryLock(self._lock_path):
            state = self._read_state()
            version = self._next_version(state)
            version_name = f"v{version:06d}"
            staging = self._versions_path / f".{version_name}.{uuid.uuid4().hex}.staging"
            final = self._versions_path / version_name
            staging.mkdir(parents=True, exist_ok=False)
            try:
                _atomic_write(staging / "artifact.json", artifact_bytes)
                attachment_checksums: dict[str, str] = {}
                for name, content in sorted(attachment_payloads.items()):
                    _atomic_write(staging / name, content)
                    attachment_checksums[name] = hashlib.sha256(content).hexdigest()
                manifest = {
                    "format_version": 1,
                    "artifact_sha256": checksum,
                    "schema_fingerprint": artifact.schema_fingerprint,
                    "created_at": artifact.created_at,
                    "attachments_sha256": attachment_checksums,
                }
                _atomic_write(staging / "manifest.json", _canonical_json(manifest))
                os.replace(staging, final)
            finally:
                if staging.exists():
                    for child in staging.iterdir():
                        child.unlink()
                    staging.rmdir()

            summary = artifact.training_summary
            entry = RegisteredVersion(
                version=version,
                relative_path=f"versions/{version_name}",
                artifact_sha256=checksum,
                model_type=artifact.model_type,
                schema_fingerprint=artifact.schema_fingerprint,
                metrics=dict(artifact.report.metrics),
                n_samples=int(summary.get("n_samples", 0)),
                n_positive=int(summary.get("n_positive", 0)),
                created_at=artifact.created_at,
            )
            raw_versions = state.setdefault("versions", [])
            if not isinstance(raw_versions, list):
                raise RuntimeError("模型注册表 versions 无效")
            raw_versions.append(entry.to_dict())
            if state.get("champion") is None:
                state["champion"] = version
            elif as_shadow:
                state["shadow"] = version
            state["generation"] = int(state.get("generation", 0)) + 1
            pruned_paths = self._prune_state(state)
            self._write_state(state)
            for directory in pruned_paths:
                if directory.is_dir():
                    for child in directory.iterdir():
                        child.unlink()
                    directory.rmdir()
            return entry

    def _prune_state(self, state: dict[str, object]) -> list[Path]:
        raw_versions = state.get("versions", [])
        if not isinstance(raw_versions, list) or len(raw_versions) <= self.max_versions:
            return []
        protected = {state.get("champion"), state.get("shadow")}
        ordered = sorted(
            (item for item in raw_versions if isinstance(item, dict)), key=lambda item: int(item["version"])
        )
        pruned_paths: list[Path] = []
        while len(ordered) > self.max_versions:
            removable = next((item for item in ordered if item.get("version") not in protected), None)
            if removable is None:
                break
            ordered.remove(removable)
            directory = (self.root / str(removable["relative_path"])).resolve()
            root = self.root.resolve()
            if root not in directory.parents:
                raise RuntimeError("待清理模型版本路径越界")
            pruned_paths.append(directory)
        state["versions"] = ordered
        return pruned_paths

    def _set_roles(self, *, champion: int | None = None, shadow: int | None | object = ...) -> None:
        with _RegistryLock(self._lock_path):
            state = self._read_state()
            known = {
                int(item["version"])
                for item in state.get("versions", [])  # type: ignore[union-attr]
                if isinstance(item, dict)
            }
            if champion is not None:
                if champion not in known:
                    raise KeyError(f"模型版本不存在: {champion}")
                state["champion"] = champion
            if shadow is not ...:
                if shadow is not None and int(shadow) not in known:
                    raise KeyError(f"模型版本不存在: {shadow}")
                state["shadow"] = int(shadow) if shadow is not None else None
            state["generation"] = int(state.get("generation", 0)) + 1
            self._write_state(state)

    def set_shadow(self, version: int | None) -> None:
        """设置或清空 Shadow 版本。"""
        self._set_roles(shadow=version)

    def promote_shadow(self) -> int:
        """把当前 Shadow 原子提升为 Champion，并清空 Shadow。"""
        with _RegistryLock(self._lock_path):
            state = self._read_state()
            shadow = state.get("shadow")
            if shadow is None:
                raise RuntimeError("当前没有 Shadow 模型")
            promoted = int(shadow)
            state["champion"] = promoted
            state["shadow"] = None
            state["generation"] = int(state.get("generation", 0)) + 1
            self._write_state(state)
            return promoted

    def rollback(self, version: int) -> None:
        """真实切换 Champion 到指定历史产物。"""
        self._set_roles(champion=version)

    def load_artifact(self, version: int) -> ModelArtifact:
        """校验注册表、Manifest 和 SHA-256 后加载模型产物。"""
        snapshot = self.snapshot()
        entry = next((item for item in snapshot.versions if item.version == version), None)
        if entry is None:
            raise KeyError(f"模型版本不存在: {version}")
        directory = (self.root / entry.relative_path).resolve()
        root = self.root.resolve()
        if root not in directory.parents:
            raise RuntimeError("模型版本路径越界")
        artifact_path = directory / "artifact.json"
        manifest_path = directory / "manifest.json"
        try:
            artifact_bytes = artifact_path.read_bytes()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"模型版本文件损坏: v{version}") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError(f"模型版本 Manifest 格式无效: v{version}")
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if actual != entry.artifact_sha256 or actual != manifest.get("artifact_sha256"):
            raise RuntimeError(f"模型版本校验和不匹配: v{version}")
        try:
            payload = json.loads(artifact_bytes)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"模型产物 JSON 损坏: v{version}") from exc
        artifact = ModelArtifact.from_dict(payload)
        if artifact.schema_fingerprint != self.schema_fingerprint:
            raise RuntimeError(f"模型版本 Schema 不兼容: v{version}")
        return artifact

    def load_attachment(self, version: int, name: str) -> bytes:
        """校验 Manifest 后读取某个原子发布的模型附件。"""
        if not name or Path(name).name != name or name.startswith("."):
            raise ValueError(f"模型附件名无效: {name}")
        snapshot = self.snapshot()
        entry = next((item for item in snapshot.versions if item.version == version), None)
        if entry is None:
            raise KeyError(f"模型版本不存在: {version}")
        directory = (self.root / entry.relative_path).resolve()
        root = self.root.resolve()
        if root not in directory.parents:
            raise RuntimeError("模型版本路径越界")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            content = (directory / name).read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"模型附件损坏: v{version}/{name}") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError(f"模型版本 Manifest 格式无效: v{version}")
        checksums = manifest.get("attachments_sha256", {})
        if not isinstance(checksums, dict) or not isinstance(checksums.get(name), str):
            raise RuntimeError(f"模型附件未登记: v{version}/{name}")
        if hashlib.sha256(content).hexdigest() != checksums[name]:
            raise RuntimeError(f"模型附件校验和不匹配: v{version}/{name}")
        return content
