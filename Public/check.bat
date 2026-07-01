@echo off
REM 即插即用版:分发前一键体检(核对模型/依赖/源码是否齐全,并刷新 manifest.json)。
setlocal
cd /d "%~dp0"

REM 优先用包内 .venv 的 python;没有则用系统 python。
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" build_bundle.py --check
endlocal
