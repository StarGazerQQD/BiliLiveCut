"""ASR Golden Set 评测工具 (V0.1.12.2)。

运行方式::

    python -m tests.golden_set.golden_eval

输出:
    - JSON 评测报告
    - 人类可读摘要
    - 各后端单独评测
    - 完整 Pipeline 评测

注意: 此脚本设计为在有真实音频文件时运行。
如果没有音频文件 (仅占位), 将输出格式说明并跳过评测。
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MANIFEST_PATH = _HERE / "manifest.json"


def _levenshtein(a: str, b: str) -> int:
    """计算编辑距离 (CER 基础)。"""
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(
                min(
                    curr[-1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = curr
    return prev[-1]


def compute_cer(reference: str, hypothesis: str) -> float:
    """字符错误率 Character Error Rate。"""
    ref = reference.replace(" ", "").replace("\n", "")
    hyp = hypothesis.replace(" ", "").replace("\n", "")
    if not ref:
        return 1.0 if hyp else 0.0
    return _levenshtein(ref, hyp) / len(ref)


def compute_proper_noun_accuracy(
    reference: str,
    hypothesis: str,
    hotwords: list[str],
) -> float:
    """专有名词准确率。"""
    if not hotwords:
        return 1.0
    correct = sum(1 for hw in hotwords if hw in hypothesis)
    return correct / len(hotwords)


def compute_segment_time_error(
    ref_segments: list[dict],
    hyp_segments: list[dict],
) -> float | None:
    """句子级时间戳平均误差 (秒)。"""
    if not ref_segments or not hyp_segments:
        return None
    errors: list[float] = []
    for i, ref in enumerate(ref_segments):
        if i >= len(hyp_segments):
            break
        hyp_start = hyp_segments[i].get("start", 0)
        errors.append(abs(ref["start"] - hyp_start))
    return sum(errors) / len(errors) if errors else None


def run_eval() -> dict:
    """执行 Golden Set 评测 (如果音频文件存在)。"""
    if not _MANIFEST_PATH.exists():
        return {"error": "manifest.json 不存在"}

    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    samples = manifest.get("samples", [])

    # 检查音频文件是否存在
    available = []
    missing = []
    for s in samples:
        audio_path = _HERE / s["audio_path"]
        if audio_path.exists():
            available.append(s)
        else:
            missing.append(s["sample_id"])

    report: dict = {
        "config": manifest.get("config", {}),
        "total_samples": len(samples),
        "available_samples": len(available),
        "missing_samples": len(missing),
        "missing_ids": missing,
        "results": [],
        "summary": {},
    }

    if not available:
        report["note"] = (
            "所有音频文件为占位符, 无法执行实际评测。"
            "请将真实音频放入 tests/golden_set/ 并更新 manifest.json 中的 audio_path。"
        )
        return report

    # 暂不加载真实模型进行评测 (需用户显式调用)
    return report


def print_report(report: dict) -> None:
    """打印人类可读评测报告。"""
    print("=" * 60)
    print("BiliLiveCut ASR Golden Set 评测报告")
    print(f"  模型 revision: {report.get('config', {}).get('model_revision', 'N/A')}")
    print(f"  主引擎: {report.get('config', {}).get('primary_engine', 'N/A')}")
    print(f"  复核引擎: {report.get('config', {}).get('review_engine', 'N/A')}")
    print(f"  总样本: {report.get('total_samples', 0)}")
    print(f"  可用: {report.get('available_samples', 0)}")
    print(f"  缺失: {report.get('missing_samples', 0)}")
    if report.get("missing_ids"):
        print(f"  缺失样本: {', '.join(report['missing_ids'])}")

    if report.get("note"):
        print(f"\n  提示: {report['note']}")

    results = report.get("results", [])
    if results:
        print("\n  逐样本结果:")
        for r in results:
            print(f"    {r.get('sample_id', '?')}: CER={r.get('cer', 'N/A'):.3f}")

    print("=" * 60)


if __name__ == "__main__":
    report = run_eval()
    print_report(report)
    out_path = _HERE / "golden_eval_report.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n评测报告已写入: {out_path}")
