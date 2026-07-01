@echo off
REM 即插即用版:离线安装依赖到本地 .venv(使用 vendor\wheels 中已封装的包)。
setlocal
cd /d "%~dp0"

REM 让联网 pip(如需)走清华+阿里云镜像;离线安装用 --no-index 时不受影响。
set "PIP_CONFIG_FILE=%~dp0pip.ini"

if not exist "vendor\wheels\*.whl" (
  echo [install] 未找到已封装的依赖 wheel。请先在联网机器执行: python build_bundle.py
  exit /b 1
)

echo [install] 创建虚拟环境 .venv ...
python -m venv .venv

echo [install] 离线安装依赖(--no-index) ...
".venv\Scripts\python.exe" -m pip install --no-index --find-links "vendor\wheels" -r "requirements-bundle.txt"

echo [install] 完成。运行 run.bat 启动。
endlocal
