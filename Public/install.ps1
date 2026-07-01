# 即插即用版:离线安装依赖到本地 .venv(全部使用 vendor/wheels 中已封装的包)。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 让联网 pip(如需)走清华+阿里云镜像;离线安装用 --no-index 时不受影响。
$env:PIP_CONFIG_FILE = Join-Path $root "pip.ini"

$wheels = Join-Path $root "vendor\wheels"
if (-not (Test-Path $wheels) -or -not (Get-ChildItem $wheels -Filter *.whl -ErrorAction SilentlyContinue)) {
    Write-Host "[install] 未找到已封装的依赖 wheel。请先在联网机器执行: python build_bundle.py" -ForegroundColor Yellow
    exit 1
}

Write-Host "[install] 创建虚拟环境 .venv ..."
python -m venv .venv

$py = Join-Path $root ".venv\Scripts\python.exe"
Write-Host "[install] 离线安装依赖(--no-index) ..."
& $py -m pip install --no-index --find-links $wheels -r (Join-Path $root "requirements-bundle.txt")

Write-Host "[install] 完成。运行 run.ps1 或 run.bat 启动。" -ForegroundColor Green
