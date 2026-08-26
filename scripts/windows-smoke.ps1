[CmdletBinding()]
param(
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$ConfigPath = Join-Path $PSScriptRoot "..\tests\integration.json"

if (-not (Test-Path $ConfigPath)) {
    throw "Missing $ConfigPath. Copy tests\integration.example.json to tests\integration.json, then fill it in."
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
if (-not $Config.enabled) {
    throw "tests\integration.json has enabled=false. Review the target device and selector, then set enabled=true explicitly."
}

if ($Install) {
    py -m pip install --user --upgrade .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

py -m unittest discover -s tests -p test_integration.py -v
exit $LASTEXITCODE
