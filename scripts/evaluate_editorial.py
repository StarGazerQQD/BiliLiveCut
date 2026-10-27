# ruff: noqa: D100
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path


def main() -> int:
    """只读导出真实判断快照或计算离线评估，不启动模型、服务或数据库迁移。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.analysis.editorial_evaluation import (
        EvaluationLabels,
        EvaluationSnapshot,
        evaluate,
        label_template,
        snapshot_digest,
    )
    from app.analysis.editorial_evaluation_io import export_snapshot, read_contract, write_result

    parser = argparse.ArgumentParser(description="冻结已有编辑判断并进行独立人工标注评估")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="只读导出已有业务结果，不调用模型")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--dataset-id", required=True)
    export.add_argument("--session", type=int, action="append", required=True)
    export.add_argument("--output", type=Path, required=True)
    template = commands.add_parser("label-template", help="生成未知标注模板并预先划分完整场次")
    template.add_argument("--snapshot", type=Path, required=True)
    template.add_argument("--holdout-session", type=int, action="append", required=True)
    template.add_argument("--annotator", required=True)
    template.add_argument("--annotation-version", required=True)
    template.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("report", help="根据明确的人工标注生成离线报告")
    report.add_argument("--snapshot", type=Path, required=True)
    report.add_argument("--labels", type=Path, required=True)
    report.add_argument("--mode", choices=["fixed_candidates", "full_session"], required=True)
    report.add_argument("--iou-threshold", type=float, default=0.5)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        snapshot = export_snapshot(args.database, dataset_id=args.dataset_id, session_ids=args.session)
        write_result(args.output, snapshot.model_dump(mode="json"))
        print(f"已导出 {len(snapshot.records)} 个对象；snapshot_sha256={snapshot_digest(snapshot)}")
    elif args.command == "label-template":
        snapshot = read_contract(args.snapshot, EvaluationSnapshot)
        labels = label_template(
            snapshot,
            annotator=args.annotator,
            annotation_version=args.annotation_version,
            holdout_sessions=args.holdout_session,
        )
        write_result(args.output, labels.model_dump(mode="json"))
        print("标注模板已保存；所有判断保持未知，请独立观看素材后填写。")
    else:
        snapshot = read_contract(args.snapshot, EvaluationSnapshot)
        labels = read_contract(args.labels, EvaluationLabels)
        write_result(args.output, evaluate(snapshot, labels, mode=args.mode, iou_threshold=args.iou_threshold))
        print("离线评估报告已保存；真实质量取决于独立人工标注，工程用例不代表模型效果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
