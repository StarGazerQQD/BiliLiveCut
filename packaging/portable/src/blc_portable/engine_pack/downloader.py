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

# ── 四引擎定义 (全部走 ModelScope 国内加速) ──────────────────

ENGINES: list[dict[str, Any]] = [
    {
        "engine_id": "whisper",
        "engine_name": "Whisper (兜底引擎) — faster-whisper-large-v3-turbo",
        "model_id": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "hub": "modelscope",
        "revision": None,
        "cache_dir": "whisper",
        "target_path": "models/whisper",
        "size_hint": "1.6 GB",
    },
    {
        "engine_id": "paraformer",
        "engine_name": "Paraformer-zh (主引擎) + 3 子模型",
        "model_id": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "cache_dir": "paraformer",
        "target_path": "models/paraformer",
        "size_hint": "~900 MB",
        "sub_models": [
            {
                "model_id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                "revision": "v2.0.4",
                "name": "fsmn-vad",
            },
            {
                "model_id": "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                "revision": "v2.0.4",
                "name": "ct-punc",
            },
            {
                "model_id": "iic/speech_campplus_sv_zh-cn_16k-common",
                "revision": None,
                "name": "cam++",
            },
        ],
    },
    {
        "engine_id": "sensevoice",
        "engine_name": "SenseVoice-Small (辅助特征)",
        "model_id": "iic/SenseVoiceSmall",
        "hub": "modelscope",
        "revision": None,
        "cache_dir": "sensevoice",
        "target_path": "models/sensevoice",
        "size_hint": "~900 MB",
    },
    {
        "engine_id": "funasr_nano",
        "engine_name": "Fun-ASR-Nano-2512 (低置信复核)",
        "model_id": "FunAudioLLM/Fun-ASR-Nano-2512",
        "hub": "modelscope",
        "revision": None,
        "cache_dir": "funasr_nano",
        "target_path": "models/funasr_nano",
        "size_hint": "~2.1 GB",
    },
]

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
    for engine in ENGINES:
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
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not targets:
        targets = [e["engine_id"] for e in ENGINES]

    print("=" * 60)
    print("  BiliLiveCut Engine Pack — 模型缓存下载")
    print(f"  目标引擎: {', '.join(targets)}")
    print(f"  缓存目录: {CACHE_DIR}")
    print("  下载源: ModelScope (国内加速)")
    print("=" * 60)

    failed: list[str] = []
    for engine in ENGINES:
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
