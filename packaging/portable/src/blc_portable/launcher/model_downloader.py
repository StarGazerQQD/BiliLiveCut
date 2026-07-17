"""四引擎模型在线下载与原子安装。

职责:
* 全量下载四个 ASR 引擎模型 (Whisper + Paraformer + SenseVoice + FunASR-Nano)
* 优先国内镜像 (hf-mirror / ModelScope)
* 固定模型 Revision (与 Engine Pack 一致)
* 下载到独立 staging 目录
* 四引擎整体原子安装 (任一失败则整体回滚)
* 断点续传支持
* 写入下载缓存信息
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

# ── 镜像配置 ──────────────────────────────────────────────

HF_MIRRORS = [
    "https://hf-mirror.com",
    "https://huggingface.co",
]

MODELSCOPE_MIRRORS = [
    "https://www.modelscope.cn",
]

# ── 四引擎下载定义 ────────────────────────────────────────

ENGINES_TO_DOWNLOAD: list[dict[str, Any]] = []


def _load_launcher_engines() -> list[dict[str, Any]]:
    """从统一模型目录加载引擎定义。

    :returns: 引擎下载定义列表。
    """
    import sys as _sys

    _CONFIG_DIR = str(Path(__file__).resolve().parent.parent.parent.parent.parent / "config")
    if _CONFIG_DIR not in _sys.path:
        _sys.path.insert(0, _CONFIG_DIR)

    from model_catalog import load_engines

    engines = []
    for e in load_engines():
        d: dict[str, Any] = {
            "engine_id": e.engine_id,
            "hub": e.hub,
            "model_id": e.repository,
            "repo_id": e.repository,
            "revision": e.resolved_revision if e.resolved_revision else None,
            "target_dir": e.engine_id,  # bare name: "whisper", "paraformer", etc.
            "description": e.display_name,
        }
        if e.sub_models:
            d["sub_models"] = [
                {"model_id": s.repository, "revision": s.resolved_revision if s.resolved_revision else None}
                for s in e.sub_models
            ]
        engines.append(d)
    return engines


# ── HuggingFace 下载 ──────────────────────────────────────


def _download_hf_model(
    repo_id: str,
    target_dir: Path,
    revision: str | None = None,
    mirror: str | None = None,
) -> None:
    """从 HuggingFace (或镜像) 下载模型。

    使用 huggingface_hub 的 snapshot_download 下载全部文件。

    :param repo_id: 仓库 ID。
    :param target_dir: 目标目录。
    :param revision: 分支/标签/commit。
    :param mirror: 镜像地址 (如 https://hf-mirror.com)。
    :raises ImportError: huggingface_hub 未安装时。
    :raises RuntimeError: 下载失败时。
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise ImportError(
            "需要安装 huggingface_hub 来下载 Whisper 模型。\n请执行: pip install huggingface_hub"
        ) from None

    if mirror:
        os.environ["HF_ENDPOINT"] = mirror

    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(target_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
    )


# ── ModelScope 下载 ───────────────────────────────────────


def _download_ms_model(
    model_id: str,
    target_dir: Path,
    revision: str = "v2.0.4",
) -> None:
    """从 ModelScope 下载单个模型。

    使用 modelscope 的 snapshot_download。

    :param model_id: 模型 ID。
    :param target_dir: 目标目录。
    :param revision: 版本。
    :raises ImportError: modelscope 未安装时。
    :raises RuntimeError: 下载失败时。
    """
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError:
        raise ImportError("需要安装 modelscope 来下载 FunASR 模型。\n请执行: pip install modelscope") from None

    snapshot_download(
        model_id=model_id,
        revision=revision,
        local_dir=str(target_dir),
    )


# ── 进度显示 ──────────────────────────────────────────────


def _print_progress(current: int, total: int, name: str) -> None:
    """输出下载进度。

    :param current: 当前索引 (0-based)。
    :param total: 总数。
    :param name: 当前名称。
    """
    pct = (current + 1) * 100 // total if total > 0 else 100
    bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
    print(f"  [{current + 1}/{total}] [{bar}] {pct}% {name}")


# ── 在线下载主入口 ────────────────────────────────────────


def download_all_engines(app_root: Path) -> dict[str, Any]:
    """全量在线下载四个引擎模型到 staging，然后原子安装。

    任何一个引擎下载失败 → 整体安装失败 → 不覆盖现有 models/。

    :param app_root: 应用根目录。
    :returns: 安-装信息字典。
    :raises RuntimeError: 任何引擎下载或安装失败时。
    """
    print("=" * 60)
    print("  在线下载四引擎模型 (全量)")
    print("=" * 60)

    staging_dir = app_root / f"models-staging-{uuid.uuid4().hex[:12]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    total = len(ENGINES_TO_DOWNLOAD)
    installed_engines: list[str] = []
    files_info: dict[str, dict[str, object]] = {}
    failed_engines: list[str] = []

    try:
        for idx, engine_def in enumerate(ENGINES_TO_DOWNLOAD):
            engine_id = str(engine_def["engine_id"])
            target_dir = staging_dir / str(engine_def["target_dir"])
            target_dir.mkdir(parents=True, exist_ok=True)
            desc = str(engine_def.get("description", engine_id))

            _print_progress(idx, total, desc)

            try:
                hub = str(engine_def["hub"])
                if hub == "huggingface":
                    repo_id = str(engine_def["repo_id"])
                    revision = engine_def.get("revision") if engine_def.get("revision") else None
                    _download_hf_model(repo_id, target_dir, revision, HF_MIRRORS[0])
                elif hub == "modelscope":
                    model_id = str(engine_def["model_id"])
                    revision = str(engine_def.get("revision", "v2.0.4"))
                    _download_ms_model(model_id, target_dir, revision)

                    # 下载子模型 (Paraformer)
                    for sub in engine_def.get("sub_models", []):
                        sub_id = str(sub["model_id"])
                        sub_rev = str(sub.get("revision", revision))
                        sub_dir = target_dir / sub_id
                        sub_dir.mkdir(parents=True, exist_ok=True)
                        print(f"    下载子模型: {sub_id}")
                        _download_ms_model(sub_id, sub_dir, sub_rev)

            except Exception as exc:
                print(f"    [失败] {desc}: {exc}")
                failed_engines.append(f"{engine_id}: {exc}")
                continue

            installed_engines.append(engine_id)
            fc = sum(1 for _ in target_dir.rglob("*") if _.is_file())
            ts = sum(f.stat().st_size for f in target_dir.rglob("*") if f.is_file())
            files_info[engine_id] = {
                "target_path": f"models/{engine_id}",
                "file_count": fc,
                "total_size": ts,
            }

        # 检查是否全部成功
        if failed_engines:
            failures = "; ".join(failed_engines)
            raise RuntimeError(f"以下引擎下载失败: {failures}")

        # 原子安装
        print(f"\n  全部 {total} 个引擎下载完成，正在安装...")
        from ..engine_pack.installer import install_models_dir_from_staging

        ok = install_models_dir_from_staging(
            app_root,
            staging_dir,
            "0.1.14.11-alpha",
            installed_engines,
            files_info,
        )

        if not ok:
            raise RuntimeError("模型原子安装失败，已回滚原 models/")

        print("  四引擎模型安装完成")
        return {
            "source": "online_download",
            "method": "full_download",
            "network_requests": total,
            "engines": installed_engines,
            "files": files_info,
        }

    except Exception:
        # 清理 staging
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
        # 不删除现有 models/
        raise
