"""四引擎模型在线下载与原子安装。

职责:
* 仅下载内容身份不匹配的 ASR 引擎模型
* 优先国内镜像 (hf-mirror / ModelScope)
* 固定模型 Revision (与 Engine Pack 一致)
* 下载到按内容指纹命名的逐引擎 staging 目录
* 每个引擎独立原子安装，已成功引擎不会因后续失败而回滚
* 跨启动断点续传支持
* 写入下载缓存信息
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
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
_RELEASE_SMOKE_PROVIDER_ENV = "BLC_RELEASE_SMOKE_TINY_MODELS"
_RELEASE_SMOKE_FAIL_ENGINE_ENV = "BLC_RELEASE_SMOKE_FAIL_ENGINE"


def _load_catalog_engines(config_dir: Path | None = None) -> list[Any]:
    """Load immutable engine definitions from the bundled model catalog."""
    import sys as _sys

    configured = config_dir or (
        Path(os.environ["BLC_MODEL_CONFIG_DIR"]) if "BLC_MODEL_CONFIG_DIR" in os.environ else None
    )
    resolved_config = configured or Path(__file__).resolve().parents[3] / "config"
    config_path = str(resolved_config.resolve())
    if config_path not in _sys.path:
        _sys.path.insert(0, config_path)

    from model_catalog import load_engines

    return list(load_engines())


def _load_launcher_engines(config_dir: Path | None = None) -> list[dict[str, Any]]:
    """从统一模型目录加载引擎定义。

    :returns: 引擎下载定义列表。
    """
    engines = []
    for e in _load_catalog_engines(config_dir):
        d: dict[str, Any] = {
            "engine_id": e.engine_id,
            "hub": e.hub,
            "model_id": e.repository,
            "repo_id": e.repository,
            "revision": e.resolved_revision if e.resolved_revision else None,
            "target_dir": e.engine_id,  # bare name: "whisper", "paraformer", etc.
            "description": e.display_name,
            "required_files": list(e.required_files),
        }
        if e.sub_models:
            d["sub_models"] = [
                {
                    "model_id": s.repository,
                    "revision": s.resolved_revision if s.resolved_revision else None,
                    "target_subdir": s.target_subdir if s.target_subdir else s.repository.rsplit("/", 1)[-1],
                }
                for s in e.sub_models
            ]
        engines.append(d)
    return engines


def _missing_required_files(target_dir: Path, engine_def: dict[str, Any]) -> list[str]:
    """Return catalog-required files missing from one staging directory."""
    required = [str(item) for item in engine_def.get("required_files", [])]
    return [relative for relative in required if not (target_dir / relative).is_file()]


def _staging_complete(
    target_dir: Path,
    marker: Path,
    fingerprint: str,
    engine_def: dict[str, Any],
) -> bool:
    """Validate a completed staging marker before avoiding network access."""
    if not marker.is_file() or _missing_required_files(target_dir, engine_def):
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return payload == {"content_fingerprint": fingerprint}


def _release_smoke_provider_enabled() -> bool:
    """Return whether the CI-only tiny model provider is explicitly enabled.

    The provider exists only to make the frozen release workflow exercise the
    real online-provisioning orchestration without downloading production-sized
    model repositories.  Refusing it outside CI prevents an end user from
    accidentally installing fixture bytes as real model assets.
    """
    requested = os.environ.get(_RELEASE_SMOKE_PROVIDER_ENV) == "1"
    ci_environment = os.environ.get("CI", "").strip().lower() in {"1", "true"}
    if requested and not ci_environment:
        raise RuntimeError(f"{_RELEASE_SMOKE_PROVIDER_ENV}=1 is restricted to CI release smoke tests")
    return requested


def _materialize_release_smoke_engine(target_dir: Path, engine_def: dict[str, Any]) -> None:
    """Write deterministic tiny files for one release-smoke engine."""
    engine_id = str(engine_def["engine_id"])
    for relative in sorted(str(item) for item in engine_def.get("required_files", [])):
        target = target_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"BLC release smoke fixture: {engine_id}/{relative}\n".encode())


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
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is unavailable in the provisioning interpreter") from exc

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
    except ImportError as exc:
        raise RuntimeError("modelscope is unavailable in the provisioning interpreter") from exc

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


def download_all_engines(app_root: Path, *, config_dir: Path | None = None) -> dict[str, Any]:
    """按内容身份下载并逐引擎提交当前模型集合。

    :param app_root: 应用根目录。
    :returns: 安-装信息字典。
    :raises RuntimeError: 任何引擎下载或安装失败时。
    """
    print("=" * 60)
    print("  在线供给 ASR 引擎模型 (内容寻址 / 可续传)")
    print("=" * 60)

    engine_defs = _load_launcher_engines(config_dir)
    expected_ids = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
    actual_ids = {e["engine_id"] for e in engine_defs}

    if not engine_defs:
        raise RuntimeError("Model catalog is empty — cannot download models")
    if actual_ids != expected_ids:
        raise RuntimeError(f"Model catalog mismatch: expected {expected_ids}, got {actual_ids}")

    from ..engine_pack.identity import desired_engine_records, model_set_fingerprint
    from ..engine_pack.installer import check_installed_models, install_engine_from_staging, reusable_engine_ids

    catalog_engines = _load_catalog_engines(config_dir)
    desired = desired_engine_records(catalog_engines)
    reusable, _ = reusable_engine_ids(app_root / "models")
    if reusable == expected_ids:
        return {
            "source": "already_installed",
            "method": "content_fingerprint",
            "network_requests": 0,
            "engines": sorted(reusable),
            "model_set_fingerprint": model_set_fingerprint(desired),
        }

    staging_root = app_root / ".model-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    installed_now: list[str] = []
    resumed: list[str] = []
    network_requests = 0
    total = len(engine_defs)
    release_smoke_provider = _release_smoke_provider_enabled()
    injected_failure = os.environ.get(_RELEASE_SMOKE_FAIL_ENGINE_ENV, "").strip()
    if injected_failure and not release_smoke_provider:
        raise RuntimeError(f"{_RELEASE_SMOKE_FAIL_ENGINE_ENV} requires the CI-only tiny model provider")

    for idx, engine_def in enumerate(engine_defs):
        engine_id = str(engine_def["engine_id"])
        if engine_id in reusable:
            print(f"  [{idx + 1}/{total}] reuse engine by content fingerprint: {engine_id}")
            continue
        fingerprint = str(desired[engine_id]["content_fingerprint"])
        target_dir = staging_root / f"{engine_id}-{fingerprint[:16]}"
        marker = target_dir / ".provision-complete.json"
        desc = str(engine_def.get("description", engine_id))
        _print_progress(idx, total, desc)
        target_dir.mkdir(parents=True, exist_ok=True)

        complete = _staging_complete(target_dir, marker, fingerprint, engine_def)
        if complete:
            resumed.append(engine_id)
            print(f"    reuse completed staging: {target_dir.name}")
        else:
            if injected_failure == engine_id:
                raise RuntimeError(f"Injected release-smoke hub unavailable for engine {engine_id}")
            if release_smoke_provider:
                _materialize_release_smoke_engine(target_dir, engine_def)
                network_requests += 1
            else:
                hub = str(engine_def["hub"])
                if hub == "huggingface":
                    revision = engine_def.get("revision") if engine_def.get("revision") else None
                    _download_hf_model(str(engine_def["repo_id"]), target_dir, revision, HF_MIRRORS[0])
                    network_requests += 1
                elif hub == "modelscope":
                    revision = str(engine_def.get("revision", "v2.0.4"))
                    _download_ms_model(str(engine_def["model_id"]), target_dir, revision)
                    network_requests += 1
                    for sub in engine_def.get("sub_models", []):
                        sub_id = str(sub["model_id"])
                        sub_rev = str(sub.get("revision", revision))
                        sub_name = str(sub.get("target_subdir", sub_id.rsplit("/", 1)[-1]))
                        sub_dir = target_dir / sub_name
                        sub_dir.mkdir(parents=True, exist_ok=True)
                        print(f"    下载子模型: {sub_id}")
                        _download_ms_model(sub_id, sub_dir, sub_rev)
                        network_requests += 1
                else:
                    raise RuntimeError(f"Unsupported model hub for {engine_id}: {hub}")
            missing = _missing_required_files(target_dir, engine_def)
            if missing:
                raise RuntimeError(f"Engine {engine_id} download incomplete; missing required files: {missing}")
            marker.write_text(
                json.dumps({"content_fingerprint": fingerprint}, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        marker.unlink(missing_ok=True)
        install_engine_from_staging(app_root, engine_id, target_dir)
        installed_now.append(engine_id)

    complete, errors = check_installed_models(app_root / "models", full_rehash=True)
    if not complete:
        raise RuntimeError("Online model provisioning incomplete: " + "; ".join(errors[:5]))
    print("  四引擎模型供给完成")
    return {
        "source": "online_download",
        "method": "per_engine_content_addressed",
        "network_requests": network_requests,
        "engines": sorted(expected_ids),
        "installed_engines": installed_now,
        "reused_engines": sorted(reusable),
        "resumed_staging": resumed,
        "model_set_fingerprint": model_set_fingerprint(desired),
        "provider": "release_smoke_tiny" if release_smoke_provider else "production_hubs",
    }


def provision_models(
    app_root: Path,
    *,
    config_dir: Path,
    expected_filename: str,
    expected_crc32: str,
    expected_sha256: str,
    user_engine_pack_path: str | None,
    offline: bool,
    fallback_online: bool,
) -> dict[str, Any]:
    """Provision installed models, a local pack, or locked online sources.

    This function is the production helper boundary and is expected to run
    under ``<app_root>/.venv`` rather than the frozen launcher interpreter.
    """
    from ..engine_pack.installer import check_installed_models, find_local_engine_packs, install_from_engine_pack

    models_dir = app_root / "models"
    ok, _ = check_installed_models(models_dir)
    if ok:
        print("  4-engine models installed (content fingerprint match), skip model prep")
        return {"source": "already_installed", "method": "content_fingerprint", "network_requests": 0}

    pack_paths = find_local_engine_packs(app_root, expected_filename, user_engine_pack_path)
    pack_errors: list[str] = []
    for pack_path in pack_paths:
        print(f"\n  found local Engine Pack candidate: {pack_path.name}")
        try:
            return install_from_engine_pack(
                app_root,
                pack_path,
                expected_crc32 if pack_path.name == expected_filename else "",
                expected_sha256 if pack_path.name == expected_filename else "",
            )
        except RuntimeError as exc:
            pack_errors.append(f"{pack_path}: {exc}")
            if user_engine_pack_path and not fallback_online:
                raise
            print(f"  local pack is not content-compatible: {exc}")
    if pack_paths:
        print("  no local Engine Pack candidate matched the desired content fingerprints")
    else:
        print("\n  no local Engine Pack found")

    if offline:
        detail = "\n".join(f"  - {item}" for item in pack_errors)
        raise RuntimeError(
            "Offline mode: no valid local Engine Pack was found and online download is blocked.\n"
            "Provide a content-compatible Engine Pack ZIP or remove --offline."
            + (f"\nCandidate failures:\n{detail}" if detail else "")
        )

    print("  downloading locked engine models online...")
    return download_all_engines(app_root, config_dir=config_dir)


def _build_parser() -> argparse.ArgumentParser:
    """Build the external provisioning helper argument parser."""
    parser = argparse.ArgumentParser(description="BiliLiveCut model provisioning helper")
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--expected-filename", required=True)
    parser.add_argument("--expected-crc32", default="")
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--engine-pack")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--fallback-online", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run provisioning and emit one machine-readable result record."""
    args = _build_parser().parse_args(argv)
    try:
        result = provision_models(
            args.app_root.resolve(),
            config_dir=args.config_dir.resolve(),
            expected_filename=args.expected_filename,
            expected_crc32=args.expected_crc32,
            expected_sha256=args.expected_sha256,
            user_engine_pack_path=args.engine_pack,
            offline=args.offline,
            fallback_online=args.fallback_online,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary must preserve third-party root failures
        print(f"BLC_PROVISION_ROOT_EXCEPTION={type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    result["provisioning_interpreter"] = str(Path(sys.executable).resolve())
    print("BLC_PROVISION_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
