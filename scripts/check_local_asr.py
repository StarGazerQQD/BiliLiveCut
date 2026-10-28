#!/usr/bin/env python3
"""Explicit local-model ASR integration check; never downloads models or calls an LLM."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.analysis.transcription.models import ASRTranscriptResult

REPO_ROOT = Path(__file__).resolve().parent.parent


def validate_result(result: ASRTranscriptResult, *, engine: str, expect: str) -> bool:
    """Validate the requested engine and distinguish VAD completion from model inference."""
    from app.analysis.transcription.speech_evidence import require_final_result

    require_final_result(result)
    text = (result.final_text or result.text).strip()
    if result.result_type != expect or bool(text) != (expect == "speech"):
        raise AssertionError(f"Expected {expect}, got {result.result_type}; text_chars={len(text)}")
    if any(
        not (
            math.isfinite(segment.start)
            and math.isfinite(segment.end)
            and 0 <= segment.start <= segment.end <= result.audio_duration + 0.1
        )
        for segment in result.segments
    ):
        raise AssertionError("ASR segment timestamps are invalid")
    if expect == "no_speech":
        return False
    expected_backend = "funasr-nano" if engine == "funasr_nano" else engine
    if (
        result.backend != expected_backend
        or result.primary_backend not in {"", expected_backend}
        or result.primary_status == "failed"
        or result.primary_error_type
        or result.fallback_backend
        or result.final_text_source == "fallback"
    ):
        raise AssertionError(
            f"Requested {engine} did not complete primary inference: backend={result.backend} "
            f"primary={result.primary_backend}/{result.primary_status} fallback={result.fallback_backend}"
        )
    return True


def main(argv: list[str] | None = None) -> int:
    """Validate installed models, then run the production isolated ASR pipeline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path, help="Existing speech or silence recording")
    parser.add_argument(
        "--models-dir", required=True, type=Path, help="Verified installed Engine Pack models directory"
    )
    parser.add_argument("--engine", choices=("whisper", "paraformer", "funasr_nano"), default="whisper")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--expect", choices=("speech", "no_speech"), default="speech")
    parser.add_argument("--contains", default="", help="Optional expected text fragment; not included in the report")
    parser.add_argument("--output", type=Path, help="Optional JSON report (does not include transcript text)")
    args = parser.parse_args(argv)
    audio = args.audio.resolve()
    models = args.models_dir.resolve()
    output = args.output.resolve() if args.output else None
    if not audio.is_file() or audio.stat().st_size == 0:
        parser.error("--audio must be an existing nonempty local file")
    if not (models / "engine-pack-installed.json").is_file():
        parser.error("--models-dir must contain engine-pack-installed.json; prepare the Engine Pack first")
    if args.expect == "no_speech" and args.contains:
        parser.error("--contains cannot be used with --expect no_speech")

    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "packaging/portable/src"))
    from blc_portable.engine_pack.installer import check_installed_models

    valid, errors = check_installed_models(models, full_rehash=True)
    if not valid:
        raise RuntimeError("Installed model verification failed: " + "; ".join(errors))

    # 独立 CLI 使用专用临时目录，避免读取仓库 .env 或写入原数据库和模型目录。
    original_cwd = Path.cwd()
    original_tempdir = tempfile.tempdir
    work_root = REPO_ROOT / ".local" / "asr-check"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=work_root) as temporary:
        tempfile.tempdir = temporary
        os.chdir(temporary)
        os.environ.update(
            DATABASE_URL="sqlite:///:memory:",
            STORAGE_ROOT=str(Path(temporary) / "storage"),
            PLUGIN_DIR=str(Path(temporary) / "plugins"),
            BLC_MODELS_DIR=str(models),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            MODELSCOPE_CACHE=str(Path(temporary) / "modelscope"),
            HF_HOME=str(Path(temporary) / "huggingface"),
            TORCH_HOME=str(Path(temporary) / "torch"),
            XDG_CACHE_HOME=str(Path(temporary) / "cache"),
            NUMBA_CACHE_DIR=str(Path(temporary) / "numba"),
            TEMP=temporary,
            TMP=temporary,
            ASR_PRIMARY=args.engine,
            ASR_PRIMARY_DEVICE=args.device,
            ASR_AUXILIARY_DEVICE=args.device,
            ASR_FALLBACK_DEVICE=args.device,
            ASR_REVIEW_DEVICE=args.device,
            WHISPER_DEVICE=args.device,
            WHISPER_COMPUTE_TYPE="int8",
            ASR_PRELOAD_ON_START="false",
        )
        try:
            from app.analysis.transcription.isolated import IsolatedPipeline
            from app.analysis.transcription.process_pool import ASRProcessPool

            pool = ASRProcessPool()
            started = time.monotonic()
            try:
                result = IsolatedPipeline(pool=pool, context={"operation": "local_asr_check"}).transcribe(str(audio))
                model_states = pool.infos()
            finally:
                pool.shutdown(0)
            text = (result.final_text or result.text).strip()
            target_engine_verified = validate_result(result, engine=args.engine, expect=args.expect)
            if args.contains and args.contains not in text:
                raise AssertionError("ASR text does not contain the requested fragment")
            report = {
                "engine": args.engine,
                "device": args.device,
                "backend": result.backend,
                "primary_backend": result.primary_backend,
                "primary_status": result.primary_status,
                "fallback_backend": result.fallback_backend,
                "review_backend": result.review_backend,
                "target_engine_verified": target_engine_verified,
                "model_states": model_states,
                "cuda_libraries": result.metadata.get("cuda_libraries"),
                "result_type": result.result_type,
                "text_chars": len(text),
                "segments": len(result.segments),
                "elapsed_s": round(time.monotonic() - started, 3),
                "audio_duration_s": result.audio_duration,
                "models_full_rehash": True,
            }
        finally:
            os.chdir(original_cwd)
            tempfile.tempdir = original_tempdir
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
