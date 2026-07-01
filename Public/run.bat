@echo off
REM 即插即用版:启动 Web 管理后台(固定使用包内 Whisper large-v3-turbo)。
setlocal
cd /d "%~dp0"

if not exist "models\whisper-large-v3-turbo\model.bin" (
  echo [run] 未找到包内模型。请先在联网机器执行: python build_bundle.py
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo [run] 未安装依赖。请先运行 install.bat
  exit /b 1
)

REM 以绝对路径强制指向包内 large-v3-turbo(覆盖 .env)。
set "WHISPER_MODEL=%~dp0models\whisper-large-v3-turbo"

REM 优先使用包内 ffmpeg(bin\),无需系统另装。
if exist "%~dp0bin\ffmpeg.exe" (
  set "FFMPEG_PATH=%~dp0bin\ffmpeg.exe"
  set "FFPROBE_PATH=%~dp0bin\ffprobe.exe"
  set "PATH=%~dp0bin;%PATH%"
)

echo [run] 启动中 -^> http://127.0.0.1:8000  (Whisper: 包内 large-v3-turbo)
".venv\Scripts\python.exe" -m app.cli serve --host 127.0.0.1 --port 8000
endlocal
