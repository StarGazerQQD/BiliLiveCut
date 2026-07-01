# 即插即用版:启动 Web 管理后台(固定使用包内 Whisper large-v3-turbo)。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$model = Join-Path $root "models\whisper-large-v3-turbo"
if (-not (Test-Path (Join-Path $model "model.bin"))) {
    Write-Host "[run] 未找到包内模型 $model。请先在联网机器执行: python build_bundle.py" -ForegroundColor Yellow
    exit 1
}
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[run] 未安装依赖。请先运行 install.ps1 / install.bat" -ForegroundColor Yellow
    exit 1
}

# 以绝对路径强制指向包内 large-v3-turbo(覆盖 .env),确保即插即用版始终用包内模型。
$env:WHISPER_MODEL = $model

# 优先使用包内 ffmpeg(bin/),无需系统另装。
$bin = Join-Path $root "bin"
if (Test-Path (Join-Path $bin "ffmpeg.exe")) {
    $env:FFMPEG_PATH = Join-Path $bin "ffmpeg.exe"
    $env:FFPROBE_PATH = Join-Path $bin "ffprobe.exe"
    $env:PATH = "$bin;$env:PATH"
}

Write-Host "[run] 启动中 -> http://127.0.0.1:8000  (Whisper: 包内 large-v3-turbo)" -ForegroundColor Green
& $py -m app.cli serve --host 127.0.0.1 --port 8000
