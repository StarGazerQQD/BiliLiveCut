# 即插即用版:分发前一键体检(核对模型/依赖/源码是否齐全,并刷新 manifest.json)。
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

& $py build_bundle.py --check
exit $LASTEXITCODE
