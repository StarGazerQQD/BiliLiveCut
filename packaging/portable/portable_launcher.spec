# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for BiliLiveCut Portable Launcher.

内嵌资源:
- source_payload.zip (731a31c 业务源码)
- payload_manifest.json
- engine_pack_info.json (四引擎模型包信息, 含 CRC32)
- app_icon.ico (如有)
"""

import sys
from pathlib import Path

_here = Path(SPECPATH)  # spec 文件所在目录

# Payload 资源
_payload_zip = str(_here / "dist" / "payload" / "source_payload.zip")
_manifest = str(_here / "dist" / "payload" / "payload_manifest.json")

# Engine Pack 信息
_engine_pack_info = str(_here / "resources" / "engine_pack_info.json")

# 构建 datas 列表
_datas = [
    (_payload_zip, "."),
    (_manifest, "."),
]

# engine_pack_info.json 存在则嵌入, 不存在则不嵌入 (此-时 CRC32 为空)
if Path(_engine_pack_info).exists():
    _datas.append((_engine_pack_info, "."))

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=["sqlmodel", "sqlalchemy", "pydantic", "uvicorn", "fastapi"],
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
    name="BiliLiveCut-Portable-Lite-v0.1.14.6-alpha-x64",
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
