"""CLI 子命令 — 系统诊断。"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def cmd_doctor(
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
) -> None:
    """自检命令 — 检查系统环境与依赖是否满足运行要求 (V0.1.13)。

    检查项:
    - Python 版本、FFmpeg/FFprobe
    - 数据库状态 (Schema version/fingerprint/integrity)
    - 磁盘空间、数据目录权限
    - 配置安全性 (ADMIN_PASSWORD)
    - Bilibili API 连通性
    - CPU/GPU 资源

    输出等级: PASS / WARN / FAIL。
    存在 FAIL 时退出码非 0。

    :param yes: 跳过交互确认。
    """
    import os as _os
    import shutil
    import sys as _sys
    from pathlib import Path as _Path

    results: list[dict] = []

    def report(level: str, item: str, detail: str = "") -> None:
        color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(level, "white")
        symbol = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(level, "?")
        console.print(f"[{color}]  {symbol}[/{color}] {item}" + (f"  — {detail}" if detail else ""))
        results.append({"level": level, "item": item, "detail": detail})

    console.print("[bold]BiliLiveCut Doctor[/bold]\n")

    # ── Python 版本
    ver = _sys.version_info
    if (ver.major, ver.minor) >= (3, 11):
        report("PASS", "Python 版本", f"{ver.major}.{ver.minor}.{ver.micro}")
    else:
        report("FAIL", "Python 版本", f"{ver.major}.{ver.minor}.{ver.micro} (需 ≥ 3.11)")

    # ── FFmpeg / FFprobe
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg:
        report("PASS", "FFmpeg", ffmpeg)
    else:
        report("FAIL", "FFmpeg", "未找到, 录制/渲染不可用")
    if ffprobe:
        report("PASS", "FFprobe", ffprobe)
    else:
        report("WARN", "FFprobe", "未找到, 部分媒体分析功能不可用")

    # ── 数据库
    from app.core.config import settings as _cfg

    db_path = _Path(_cfg.database_url.replace("sqlite:///", "."))
    if db_path.exists():
        try:
            from app.db.schema import (
                CURRENT_SCHEMA_VERSION,
                validate_schema,
            )

            ok = validate_schema()
            if ok:
                report("PASS", "数据库 Schema", f"v{CURRENT_SCHEMA_VERSION}")
            else:
                report("FAIL", "数据库 Schema", "不兼容, 需重建")
            # Integrity
            from app.db.session import engine

            with engine.connect() as conn:
                r = conn.exec_driver_sql("PRAGMA integrity_check").fetchone()
                if r and r[0] == "ok":
                    report("PASS", "数据库完整性")
                else:
                    report("FAIL", "数据库完整性", str(r[0]) if r else "unknown")
        except Exception as exc:
            report("FAIL", "数据库检查", str(exc)[:80])
    else:
        report("WARN", "数据库", "尚未创建 (首次启动时自动创建)")

    # ── 磁盘空间
    data_dir = _Path(_cfg.data_dir)
    if data_dir.exists():
        try:
            if hasattr(shutil, "disk_usage"):
                usage = shutil.disk_usage(data_dir)
                free_gb = usage.free / (1024**3)
                if free_gb >= 5:
                    report("PASS", "磁盘空间", f"{free_gb:.1f} GB 可用")
                elif free_gb >= 2:
                    report("WARN", "磁盘空间", f"仅 {free_gb:.1f} GB 可用")
                else:
                    report("FAIL", "磁盘空间", f"严重不足: {free_gb:.1f} GB")
        except Exception:
            report("WARN", "磁盘空间", "无法检测")

    # ── 数据目录权限
    try:
        test_file = data_dir / ".doctor_write_test"
        test_file.write_text("test")
        test_file.unlink()
        report("PASS", "数据目录权限", str(data_dir))
    except Exception:
        report("FAIL", "数据目录权限", f"无法写入 {data_dir}")

    # ── ADMIN_PASSWORD 安全
    if not _cfg.admin_password:
        import socket

        hostname = socket.gethostname()
        has_nonloop = False
        try:
            for info in socket.getaddrinfo(hostname, None):
                addr = info[4][0]
                if not addr.startswith("127.") and addr != "::1":
                    has_nonloop = True
                    break
        except Exception:
            has_nonloop = True
        if has_nonloop:
            report("WARN", "Web 安全", "ADMIN_PASSWORD 为空, 非本机访问将被拒绝")
        else:
            report("WARN", "Web 安全", "ADMIN_PASSWORD 为空 (仅本机可访问)")

    # ── Bilibili API 连通性 (可选)
    try:
        import httpx

        resp = httpx.get("https://api.live.bilibili.com/", timeout=5)
        if resp.status_code < 500:
            report("PASS", "Bilibili API", "连通正常")
        else:
            report("WARN", "Bilibili API", f"HTTP {resp.status_code}")
    except Exception as exc:
        report("WARN", "Bilibili API", f"无法连通: {type(exc).__name__}")

    # ── CPU/Memory
    try:
        cpu_count = _os.cpu_count() or 0
        report("PASS", "CPU", f"{cpu_count} 核")
    except Exception:
        report("WARN", "CPU", "无法检测")

    # ── GPU
    try:
        from app.core.asr_detection import detect_resources

        res = detect_resources()
        if res.get("gpu_available"):
            vram = res.get("total_vram_mb", 0) / 1024
            report("PASS", "GPU", f"可用 ({vram:.1f} GB VRAM)")
        else:
            report("WARN", "GPU", "未检测到 NVIDIA GPU (将使用 CPU 模式)")
    except Exception:
        report("WARN", "GPU", "无法检测")

    # ── ASR 配置
    asr_backend = _cfg.asr_backend or "none"
    report("PASS" if asr_backend != "none" else "WARN", "ASR 后端", asr_backend or "未配置")

    # ── 模型目录
    model_dir = _Path(_cfg.model_dir) if _cfg.model_dir else None
    if model_dir and model_dir.exists():
        report("PASS", "模型目录", str(model_dir))
    elif model_dir:
        report("WARN", "模型目录", "不存在")
    else:
        report("WARN", "模型目录", "未配置")

    # ── Uploader
    uploader = _cfg.uploader_type or "manual"
    report("PASS", "上传方式", uploader)

    # ── 汇总
    console.print("")
    fail_count = sum(1 for r in results if r["level"] == "FAIL")
    warn_count = sum(1 for r in results if r["level"] == "WARN")
    pass_count = sum(1 for r in results if r["level"] == "PASS")

    if fail_count == 0:
        console.print(f"[green]{pass_count} PASS, {warn_count} WARN[/green] — 环境就绪")
    else:
        console.print(f"[red]{fail_count} FAIL, {warn_count} WARN, {pass_count} PASS[/red] — 存在关键问题需修复")
        raise typer.Exit(code=1)


# 注册列表
DOCTOR_COMMANDS = [
    ("doctor", cmd_doctor, None),
]
