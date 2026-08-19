Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot "runtime\python\python.exe"
$env:PATH = "$(Join-Path $PSScriptRoot 'runtime\python');$(Join-Path $PSScriptRoot 'runtime\python\Scripts');$env:PATH"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot "runtime\ms-playwright"
$env:HF_HOME = Join-Path $PSScriptRoot "runtime\huggingface"
$env:PYTHONUTF8 = "1"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Private Python runtime is missing."
}

& $python app.py
