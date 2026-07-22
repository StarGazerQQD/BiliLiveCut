# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for BiliLiveCut Portable Launcher.

内嵌资源:
- source_payload.zip (1b47a09 业务源码)
- payload_manifest.json
- engine_pack_info.json (四引擎模型包信息, 含 CRC32)
- app_icon.ico (如有)
"""

import os
import sys
from pathlib import Path

_here = Path(SPECPATH).parent  # spec 在 specs/ 下，上溯一级到 packaging/portable/

# 入口脚本
_entry = str(_here / "src" / "blc_portable" / "launcher" / "main.py")
# 模块搜索路径
_config_dir = _here / "config"
_pathex = [str(_here / "src"), str(_config_dir)]

# Payload 资源
_payload_zip = str(_here / "dist" / "payload" / "source_payload.zip")
_manifest = str(_here / "dist" / "payload" / "payload_manifest.json")
_version_config = str(_config_dir / "version.json")
_model_sources_lock = str(_config_dir / "model_sources.lock.json")

# Engine Pack 信息
_engine_pack_info = str(_here / "resources" / "engine_pack_info.json")

# 构建 datas 列表
_lock_dir = str(_here / "locks")
_lock_files = []
for _lf in sorted(Path(_lock_dir).glob("requirements-runtime-*-win-x64.lock")):
    _lock_files.append((str(_lf), "locks"))

_datas = [
    (_payload_zip, "."),
    (_manifest, "."),
    (_version_config, "."),
    (_model_sources_lock, "."),
] + _lock_files

# engine_pack_info.json 存在则嵌入, 不存在则不嵌入 (此-时 CRC32 为空)
if os.environ.get("BLC_OMIT_ENGINE_PACK_INFO") != "1" and Path(_engine_pack_info).exists():
    _datas.append((_engine_pack_info, "."))

a = Analysis(
    [_entry],
    pathex=_pathex,
    binaries=[],
    datas=_datas,
    hiddenimports=[
        "sqlmodel",
        "sqlalchemy",
        "pydantic",
        "uvicorn",
        "fastapi",
        "model_catalog",
        "version_loader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BiliLiveCut-Portable-Lite-v0.1.15.2-alpha-x64",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
