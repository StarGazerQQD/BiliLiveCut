@echo off
REM 即插即用版:全自动「一键即用」= 自动修复(下载缺失/纠正平台)-> 离线安装 -> 启动。
setlocal
cd /d "%~dp0"

REM 让包内所有联网 pip 操作走清华(主)+ 阿里云(备选)镜像,国内更快。
set "PIP_CONFIG_FILE=%~dp0pip.ini"

echo [setup] 1/3 自动修复(按需下载模型/依赖/ffmpeg,纠正平台不一致)...
python build_bundle.py --repair
if errorlevel 1 (
  echo [setup] 自动修复未完成:请检查网络后重试 setup.bat
  exit /b 1
)

echo [setup] 2/3 离线安装依赖到 .venv ...
call install.bat
if errorlevel 1 (
  echo [setup] 安装失败。
  exit /b 1
)

echo [setup] 3/3 启动服务 ...
call run.bat
endlocal
