# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for BiliLiveCut Portable Launcher.

内嵌资源:
- source_payload.zip (74c21b4 业务源码)
- payload_manifest.json
- app_icon.ico (如有)
"""

import sys
from pathlib import Path

_here = Path(SPECPATH)  # spec 文件所在目录

# Payload 资源
_payload_zip = str(_here / "dist" / "payload" / "source_payload.zip")
_manifest = str(_here / "dist" / "payload" / "payload_manifest.json")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[
        (_payload_zip, "."),
        (_manifest, "."),
    ],
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
    name="BiliLiveCut-Portable-Lite-v0.1.14.5-alpha-x64",
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
