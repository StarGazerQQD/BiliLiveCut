# 即插即用版:全自动「一键即用」= 自动修复(下载缺失/纠正平台)-> 离线安装 -> 启动。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 让包内所有联网 pip 操作走清华(主)+ 阿里云(备选)镜像,国内更快。
$env:PIP_CONFIG_FILE = Join-Path $root "pip.ini"

Write-Host "[setup] 1/3 自动修复(按需下载模型/依赖/ffmpeg,纠正平台不一致)..." -ForegroundColor Cyan
python build_bundle.py --repair
if ($LASTEXITCODE -ne 0) {
    Write-Host "[setup] 自动修复未完成:请检查网络后重试 setup.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host "[setup] 2/3 离线安装依赖到 .venv ..." -ForegroundColor Cyan
& (Join-Path $root "install.ps1")
if ($LASTEXITCODE -ne 0) { Write-Host "[setup] 安装失败。" -ForegroundColor Yellow; exit 1 }

Write-Host "[setup] 3/3 启动服务 ..." -ForegroundColor Cyan
& (Join-Path $root "run.ps1")
