"""独立引擎模型缓存下载脚本。

每个引擎独立下载到持久缓存目录 (build/model_cache/)。
支持断点续传，引擎间互不影响。完成后由 build_engine_pack.py 读取缓存构建 ZIP。

用法:
    python download_engines.py              # 下载全部四引擎
    python download_engines.py whisper       # 只下载 Whisper
    python download_engines.py paraformer    # 只下载 Paraformer
    python download_engines.py sensevoice    # 只下载 SenseVoice
    python download_engines.py funasr_nano   # 只下载 FunASR-Nano
    python download_engines.py --status      # 查看进度
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

PORTABLE_DIR = Path(__file__).resolve().parent.parent.parent.parent
CACHE_DIR = PORTABLE_DIR / ".model_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 模型定义统一来源: packaging/portable/config/model_sources.lock.json ──
# 通过 model_catalog 加载，不再维护独立 ENGINES 列表。


def _load_engine_defs() -> list[dict[str, Any]]:
    """从统一模型目录加载引擎定义，适配下载器格式。

    :returns: 引擎定义列表。
    """
    import sys as _sys

    _CONFIG_DIR = str(PORTABLE_DIR / "config")
    if _CONFIG_DIR not in _sys.path:
        _sys.path.insert(0, _CONFIG_DIR)

    from model_catalog import load_engines

    engines: list[dict[str, Any]] = []
    for e in load_engines():
        d: dict[str, Any] = {
            "engine_id": e.engine_id,
            "engine_name": e.display_name,
            "model_id": e.repository,
            "hub": "modelscope",  # downloader 统一走 ModelScope 国内加速
            "revision": e.resolved_revision if e.resolved_revision else None,
            "cache_dir": _engine_id_to_cache_dir(e.engine_id),
            "target_path": e.target_path,
            "size_hint": "N/A",
        }
        if e.sub_models:
            d["sub_models"] = [
                {
                    "model_id": s.repository,
                    "revision": s.resolved_revision if s.resolved_revision else None,
                    "name": s.target_subdir if s.target_subdir else s.repository.rsplit("/", 1)[-1],
                }
                for s in e.sub_models
            ]
        engines.append(d)
    return engines


def _engine_id_to_cache_dir(engine_id: str) -> str:
    """引擎 ID → 缓存子目录名映射。"""
    return engine_id


STATE_FILE = CACHE_DIR / "download_state.json"


def load_state() -> dict[str, Any]:
    """加载下载状态。"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"downloaded": [], "progress": {}}


def save_state(state: dict[str, Any]) -> None:
    """保存下载状态。"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_engine_dir(engine: dict[str, Any]) -> Path:
    """获取引擎缓存目录。"""
    return CACHE_DIR / str(engine["cache_dir"])


def get_engine_size(engine_dir: Path) -> tuple[int, float]:
    """获取引擎缓存目录的文件数和总大小。

    :returns: (文件数, 大小GB)
    """
    if not engine_dir.exists():
        return 0, 0.0
    fc = sum(1 for _ in engine_dir.rglob("*") if _.is_file())
    ts = sum(f.stat().st_size for f in engine_dir.rglob("*") if f.is_file())
    return fc, ts / (1024**3)


def show_status() -> None:
    """显示所有引擎下载状态。"""
    print("=" * 60)
    print("  模型缓存下载状态")
    print("=" * 60)
    total_files = 0
    total_gb = 0.0
    for engine in _load_engine_defs():
        engine_dir = get_engine_dir(engine)
        fc, gb = get_engine_size(engine_dir)
        total_files += fc
        total_gb += gb
        status = "[OK]" if fc > 0 and gb > 0.01 else "[--]"
        extra = engine.get("size_hint", "")
        print(f"  [{status}] {engine['engine_name']}")
        print(f"         目录: {engine_dir}")
        print(f"         文件: {fc}, 大小: {gb:.2f} GB" + (f" (预计 {extra})" if extra else ""))
    print(f"\n  总计: {total_files} 文件, {total_gb:.2f} GB")


def download_engine(engine: dict[str, Any]) -> bool:
    """下载单个引擎到缓存目录。支持断点续传。

    :param engine: 引擎定义。
    :returns: True 成功。
    """
    from modelscope.hub.snapshot_download import snapshot_download

    engine_dir = get_engine_dir(engine)
    engine_dir.mkdir(parents=True, exist_ok=True)

    model_id = str(engine["model_id"])
    revision = engine.get("revision")
    hub = engine.get("hub", "modelscope")

    print(f"\n  [{engine['engine_id']}] {engine['engine_name']}")
    print(f"  ModelScope ID: {model_id}")
    if revision:
        print(f"  Revision: {revision}")
    print(f"  目标目录: {engine_dir}")

    start = time.time()
    try:
        # 主模型下载
        kwargs: dict[str, Any] = {"model_id": model_id, "local_dir": str(engine_dir)}
        if revision:
            kwargs["revision"] = str(revision)

        if hub == "modelscope":
            snapshot_download(**kwargs)

        # 子模型下载 (Paraformer)
        for sub in engine.get("sub_models", []):
            sub_id = str(sub["model_id"])
            sub_name = str(sub.get("name", sub_id.rsplit("/", 1)[-1]))
            sub_rev = sub.get("revision")
            sub_dir = engine_dir / sub_name
            sub_dir.mkdir(parents=True, exist_ok=True)
            print(f"    子模型: {sub_name} ({sub_id})")
            sub_kwargs: dict[str, Any] = {"model_id": sub_id, "local_dir": str(sub_dir)}
            if sub_rev:
                sub_kwargs["revision"] = str(sub_rev)
            snapshot_download(**sub_kwargs)

    except Exception as e:
        elapsed = time.time() - start
        print(f"\n  [FAIL] 下载失败 ({elapsed:.0f}s): {e}")
        return False

    elapsed = time.time() - start
    fc, gb = get_engine_size(engine_dir)
    print(f"  [OK] 完成 ({elapsed:.0f}s): {fc} 文件, {gb:.2f} GB")

    # 更新状态
    state = load_state()
    if engine["engine_id"] not in state["downloaded"]:
        state["downloaded"].append(engine["engine_id"])
    state["progress"][engine["engine_id"]] = {"files": fc, "size_gb": round(gb, 3), "elapsed_s": int(elapsed)}
    save_state(state)

    return True


def main() -> None:
    """主入口。"""
    if "--status" in sys.argv or "-s" in sys.argv:
        show_status()
        return

    # 确定要下载的引擎
    engine_defs = _load_engine_defs()
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not targets:
        targets = [e["engine_id"] for e in engine_defs]

    print("=" * 60)
    print("  BiliLiveCut Engine Pack — 模型缓存下载")
    print(f"  目标引擎: {', '.join(targets)}")
    print(f"  缓存目录: {CACHE_DIR}")
    print("  下载源: ModelScope (国内加速)")
    print("=" * 60)

    failed: list[str] = []
    for engine in engine_defs:
        if engine["engine_id"] not in targets:
            continue

        # 检查是否已下载
        eid = engine["engine_id"]
        state = load_state()
        if eid in state.get("downloaded", []):
            fc, gb = get_engine_size(get_engine_dir(engine))
            if fc > 0 and gb > 0.01:
                print(f"\n  [{eid}] 已缓存，跳过 ({fc} 文件, {gb:.2f} GB)")
                continue
            else:
                # 状态文件显示已下载但实际文件不存在，重置状态
                state["downloaded"].remove(eid)
                save_state(state)

        if not download_engine(engine):
            failed.append(eid)

    # 最终报告
    print("\n" + "=" * 60)
    if failed:
        print(f"  失败: {', '.join(failed)}")
        print(f"  已完成: {len(targets) - len(failed)}/{len(targets)}")
        print("  请重试: python download_engines.py " + " ".join(failed))
        sys.exit(1)
    else:
        show_status()
        print("\n  全部下载完成！可执行构建:")
        print("    python build_engine_pack.py --from-cache")
        sys.exit(0)


if __name__ == "__main__":
    main()
